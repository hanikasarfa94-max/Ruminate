"""P0 storage, migration and command-line acceptance checks (no network required)."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ruminate.application import Application
from ruminate.config import Config, default_data_dir
from ruminate.errors import RuminateError
from ruminate.persistence.database import (
    Migration,
    apply_migrations,
    bundled_migrations,
    connect,
)


@pytest.fixture
def app(tmp_path: Path) -> Application:
    return Application(Config(tmp_path / "private data", network_mode="off"))


def run_cli(
    directory: Path, command: str, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["RUMINATE_NETWORK_MODE"] = "off"
    environment.pop("RUMINATE_ALLOWED_ROOTS", None)
    environment.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "ruminate", "--data-dir", str(directory), command],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )


def test_health_before_init_has_no_persistent_effect(app: Application) -> None:
    result = app.health_check()
    assert result["status"] == "uninitialized"
    assert result["initialized"] is False
    assert result["model"] == {"provider": "none", "credentials_required": False}
    assert not app.config.data_dir.exists()


def test_initialize_is_idempotent_and_survives_application_restart(app: Application) -> None:
    initial = app.initialize()
    assert initial["status"] == "ok"
    assert initial["fts5"] is True
    assert initial["journal_mode"] == "wal"
    with connect(app.config.database_path) as connection:
        connection.execute(
            "INSERT INTO jobs(id, kind, input_ref, status) VALUES (?, ?, ?, ?)",
            ("job_persist", "test", "fixture", "queued"),
        )
        history = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")]

    restarted = Application(app.config)
    assert restarted.initialize()["status"] == "ok"
    assert restarted.health_check()["schema_version"] == len(bundled_migrations())
    with connect(app.config.database_path, readonly=True) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")
        ] == history
        assert (
            connection.execute("SELECT status FROM jobs WHERE id='job_persist'").fetchone()[0]
            == "queued"
        )
    if os.name == "posix":
        assert app.config.database_path.stat().st_mode & 0o777 == 0o600
        assert app.config.data_dir.stat().st_mode & 0o777 == 0o700


def test_failed_migration_rolls_back_schema_data_and_history(app: Application) -> None:
    app.initialize()
    migrations = bundled_migrations()
    broken = Migration(
        len(migrations) + 1,
        "002_broken.sql",
        "CREATE TABLE partial_write(id INTEGER);"
        "INSERT INTO partial_write VALUES (1);"
        "INSERT INTO table_that_does_not_exist VALUES (2);",
    )
    with connect(app.config.database_path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, (*migrations, broken))
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='partial_write'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
            migrations
        )
    assert app.health_check()["status"] == "ok"


def test_incomplete_sql_rolls_back_pending_batch(app: Application) -> None:
    app.initialize()
    migrations = bundled_migrations()
    valid_pending = Migration(len(migrations) + 1, "002_pending.sql", "CREATE TABLE pending(id);")
    incomplete = Migration(len(migrations) + 2, "003_incomplete.sql", "CREATE TABLE incomplete(id)")
    with connect(app.config.database_path) as connection:
        with pytest.raises(RuminateError) as error:
            apply_migrations(connection, (*migrations, valid_pending, incomplete))
        assert error.value.code == "invalid_migration_sql"
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='pending'").fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
            migrations
        )


@pytest.mark.parametrize("field", ["checksum", "name"])
def test_changed_migration_is_rejected_without_replacing_history(
    app: Application, field: str
) -> None:
    app.initialize()
    with connect(app.config.database_path) as connection:
        # The column is from the fixed parameterization, never from user input.
        connection.execute(
            f"UPDATE schema_migrations SET {field} = ? WHERE version = 1", ("changed",)
        )
    health = app.health_check()
    assert health["status"] == "needs_attention"
    assert health["error"]["code"] == "migration_mismatch"
    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "migration_mismatch"
    with connect(app.config.database_path, readonly=True) as connection:
        assert (
            connection.execute(f"SELECT {field} FROM schema_migrations WHERE version=1").fetchone()[
                0
            ]
            == "changed"
        )


def test_newer_database_requires_newer_application(app: Application) -> None:
    app.initialize()
    newer_version = len(bundled_migrations()) + 1
    with connect(app.config.database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
            (newer_version, "future.sql", "future-checksum"),
        )
    result = app.health_check()
    assert result["status"] == "needs_attention"
    assert result["error"]["code"] == "schema_too_new"
    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "schema_too_new"


def test_corrupt_database_is_reported_and_preserved(app: Application) -> None:
    app.config.data_dir.mkdir()
    corrupt = b"not a SQLite database\x00do not overwrite"
    app.config.database_path.write_bytes(corrupt)
    result = app.health_check()
    assert result["status"] == "needs_attention"
    assert result["error"]["code"] == "database_unreadable"
    assert app.config.database_path.read_bytes() == corrupt


def test_cli_doctor_init_and_restart_return_json(tmp_path: Path) -> None:
    directory = tmp_path / "cli workspace"
    before = run_cli(directory, "doctor")
    assert before.returncode == 1
    assert json.loads(before.stdout)["status"] == "uninitialized"
    assert before.stderr == ""
    assert not directory.exists()

    initialized = run_cli(directory, "init")
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["status"] == "ok"
    assert initialized.stderr == ""

    after = run_cli(directory, "doctor")
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout)["schema_version"] == len(bundled_migrations())
    assert after.stderr == ""


def test_cli_storage_error_is_json_without_private_path(tmp_path: Path) -> None:
    blocked = tmp_path / "private-secret-name"
    blocked.write_text("existing user content")
    result = run_cli(blocked, "init")
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "storage_error"
    assert "private-secret-name" not in result.stderr
    assert blocked.read_text() == "existing user content"


def test_invalid_config_fails_before_creating_data(tmp_path: Path) -> None:
    directory = tmp_path / "invalid config"
    result = run_cli(directory, "init", RUMINATE_NETWORK_MODE="unrestricted")
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_config"
    assert result.stdout == ""
    assert not directory.exists()


def test_config_env_and_explicit_path_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMINATE_DATA_DIR", str(tmp_path / "from env"))
    monkeypatch.setenv("RUMINATE_NETWORK_MODE", "off")
    roots = [tmp_path / "notes", tmp_path / "papers"]
    monkeypatch.setenv("RUMINATE_ALLOWED_ROOTS", os.pathsep.join(map(str, roots)))
    config = Config.from_env()
    assert config.data_dir == (tmp_path / "from env").resolve()
    assert config.allowed_roots == tuple(path.resolve() for path in roots)
    assert config.network_mode == "off"
    override = Config.from_env(tmp_path / "explicit")
    assert override.data_dir == (tmp_path / "explicit").resolve()
    assert not config.data_dir.exists()
    assert not override.data_dir.exists()


def test_platform_data_paths_are_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert default_data_dir({}, "darwin") == tmp_path / "home/Library/Application Support/Ruminate"
    assert default_data_dir({}, "linux") == tmp_path / "home/.local/share/ruminate"
    assert (
        default_data_dir({"XDG_DATA_HOME": str(tmp_path / "xdg")}, "linux")
        == tmp_path / "xdg/ruminate"
    )
    assert (
        default_data_dir({"LOCALAPPDATA": str(tmp_path / "local")}, "win32")
        == tmp_path / "local/Ruminate"
    )


def test_init_rejects_database_symlink_without_touching_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.sqlite3"
    with sqlite3.connect(outside) as connection:
        connection.execute("CREATE TABLE unrelated_secret(id INTEGER)")
    original = outside.read_bytes()
    original_permissions = outside.stat().st_mode & 0o777
    data = tmp_path / "workspace"
    data.mkdir()
    (data / "ruminate.sqlite3").symlink_to(outside)
    app = Application(Config(data))

    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "unsafe_storage_path"
    assert outside.read_bytes() == original
    assert outside.stat().st_mode & 0o777 == original_permissions
    with sqlite3.connect(outside) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("unrelated_secret",)]
    assert app.health_check()["status"] == "needs_attention"


def test_init_rejects_foreign_database_without_changing_it(app: Application) -> None:
    app.config.data_dir.mkdir()
    with sqlite3.connect(app.config.database_path) as connection:
        connection.execute("CREATE TABLE unrelated_secret(id INTEGER)")
        connection.execute("INSERT INTO unrelated_secret VALUES (42)")
    original = app.config.database_path.read_bytes()
    original_permissions = app.config.database_path.stat().st_mode & 0o777

    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "foreign_database"
    assert app.config.database_path.read_bytes() == original
    assert app.config.database_path.stat().st_mode & 0o777 == original_permissions
    with sqlite3.connect(app.config.database_path) as connection:
        assert connection.execute("SELECT * FROM unrelated_secret").fetchall() == [(42,)]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("unrelated_secret",)]
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("component", ["blobs", "outputs"])
def test_init_rejects_derived_directory_symlink(tmp_path: Path, component: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "existing.txt"
    sentinel.write_text("unchanged external content")
    original_permissions = outside.stat().st_mode & 0o777
    data = tmp_path / "workspace"
    data.mkdir()
    (data / component).symlink_to(outside, target_is_directory=True)
    app = Application(Config(data))

    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "unsafe_storage_path"
    assert sentinel.read_text() == "unchanged external content"
    assert list(outside.iterdir()) == [sentinel]
    assert outside.stat().st_mode & 0o777 == original_permissions
    assert not app.config.database_path.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_init_rejects_sqlite_sidecar_symlink(tmp_path: Path, suffix: str) -> None:
    outside = tmp_path / "outside.bin"
    original = b"must never become SQLite shared memory or a WAL"
    outside.write_bytes(original)
    original_permissions = outside.stat().st_mode & 0o777
    data = tmp_path / "workspace"
    data.mkdir()
    (data / f"ruminate.sqlite3{suffix}").symlink_to(outside)
    app = Application(Config(data))

    with pytest.raises(RuminateError) as error:
        app.initialize()
    assert error.value.code == "unsafe_storage_path"
    assert outside.read_bytes() == original
    assert outside.stat().st_mode & 0o777 == original_permissions
    assert not app.config.database_path.exists()
    assert app.health_check()["status"] == "needs_attention"


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE jobs",
        "UPDATE app_metadata SET value='another-product' WHERE key='product'",
    ],
)
def test_health_detects_incomplete_or_foreign_schema(app: Application, damage: str) -> None:
    app.initialize()
    with connect(app.config.database_path) as connection:
        connection.execute(damage)
    health = app.health_check()
    assert health["status"] == "needs_attention"


def test_zero_byte_database_can_be_initialized(app: Application) -> None:
    app.config.data_dir.mkdir()
    app.config.database_path.touch()
    assert app.initialize()["status"] == "ok"

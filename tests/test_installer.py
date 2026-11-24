"""Installation preserves research and unrelated host configuration, including on failure."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import install_codex as installer

ROOT = Path(__file__).resolve().parents[1]


def source(tmp_path: Path) -> Path:
    checkout = tmp_path / "source with spaces"
    for name in installer.RUNTIME_ITEMS:
        original, target = ROOT / name, checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_dir():
            shutil.copytree(original, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(original, target)
    return checkout


def test_release_files_exclude_developer_settings_and_research(tmp_path: Path) -> None:
    checkout = source(tmp_path)
    for name in (".codex/config.toml", ".local/store/ruminate.sqlite3", ".env"):
        private = checkout / name
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("private")
    files = installer.runtime_files(checkout)
    assert Path("ruminate/ui/app.js") in files
    assert Path("docs/THINKING_WORKSPACE.md") in files
    assert Path("docs/auto-reflection-prompt.md") in files
    assert not any(str(file).startswith((".local", ".codex", ".env")) for file in files)
    (checkout / "ruminate/unexpected").symlink_to(checkout / ".env")
    with pytest.raises(ValueError, match="symlink"):
        installer.runtime_files(checkout)


def test_runtime_is_stable_and_venv_is_not_relocated(tmp_path: Path, monkeypatch) -> None:
    checkout = source(tmp_path)
    root = tmp_path / "installation"
    root.mkdir()
    calls = []

    def sync(command, **kwargs):
        runtime = Path(command[-1])
        marker = runtime / ".venv/bin/python"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text(str(runtime))
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", sync)
    first = installer.prepare_runtime(checkout, root, "uv")
    assert installer.prepare_runtime(checkout, root, "uv") == first
    assert (first / ".venv/bin/python").read_text() == str(first)
    (checkout / "ruminate/__init__.py").write_text("# new runtime\n")
    updated = installer.prepare_runtime(checkout, root, "uv")
    assert updated != first and first.exists()
    assert all("--locked" in command and "--no-dev" in command for command in calls)


def test_update_keeps_data_path_and_rejects_overlap(tmp_path: Path) -> None:
    root, data = tmp_path / "plugin", tmp_path / "research"
    assert installer.choose_data(root, None, {"data_dir": str(data)}) == data
    with pytest.raises(ValueError, match="already uses"):
        installer.choose_data(root, tmp_path / "empty", {"data_dir": str(data)})
    for invalid in (root, root / "data", tmp_path):
        with pytest.raises(ValueError, match="separate"):
            installer.choose_data(root, invalid, {})


def test_marketplace_runs_locally_and_keeps_custom_permissions(tmp_path: Path) -> None:
    checkout = source(tmp_path)
    data = tmp_path / "Research notes"
    first = tmp_path / "first"
    installer.build_marketplace(checkout, first, data, tmp_path / "absent")
    plugin = first / "plugins/ruminate"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    wiring = json.loads((plugin / ".mcp.json").read_text())
    transport = wiring["mcpServers"]["ruminate"]
    assert manifest["mcpServers"] == "./.mcp.json"
    assert transport["command"] == str(checkout / ".venv/bin/python")
    assert str(data) in transport["args"]
    assert "uv" not in transport["command"]  # Opening the plugin does not resolve dependencies.
    assert transport["env"] == {"RUMINATE_MCP_WRITES": "1"}
    assert not (plugin / "plugin.json").exists()
    transport["env"]["RUMINATE_NETWORK_MODE"] = "off"
    transport["startup_timeout_sec"] = 30
    (plugin / ".mcp.json").write_text(json.dumps(wiring))
    second = tmp_path / "next"
    installer.build_marketplace(checkout, second, data, first)
    new_transport = json.loads((second / "plugins/ruminate/.mcp.json").read_text())
    assert new_transport["mcpServers"]["ruminate"] == transport


@pytest.mark.parametrize("failure", ["dependencies", "register", "verify"])
def test_failed_update_restores_registration_and_keeps_research(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    checkout = source(tmp_path)
    root, data = tmp_path / "installed", tmp_path / "research"
    data.mkdir()
    notes = data / "notes.md"
    notes.write_text("Previously saved research")
    old = root / "marketplace"
    installer.build_marketplace(checkout, old, data, tmp_path / "absent")
    sentinel = old / "installed-marker"
    sentinel.write_text("previous version")
    state = {"plugin_id": installer.PLUGIN_ID, "data_dir": str(data), "version": "old"}
    installer.write_json(root / "installation.json", state)
    commands = []
    monkeypatch.setattr(installer, "check_conflicts", lambda *args: None)

    def runtime(*args):
        if failure == "dependencies":
            raise RuntimeError("dependency failure")
        return checkout

    def run(command, **kwargs):
        commands.append(command)
        if command[1:4] == ["plugin", "marketplace", "add"]:
            return {"alreadyAdded": True}
        if command[1:3] == ["plugin", "add"]:
            if failure == "register" and not sentinel.exists():
                raise RuntimeError("registration failure")
            return {"version": "new"}
        if "--installed-plugin" in command:
            raise RuntimeError("verification failure")
        return {"status": "ok"}

    monkeypatch.setattr(installer, "prepare_runtime", runtime)
    monkeypatch.setattr(installer, "run", run)
    with pytest.raises(RuntimeError, match="failure"):
        installer.install(checkout, root, None, "codex", "uv")
    assert notes.read_text() == "Previously saved research"
    assert sentinel.read_text() == "previous version"
    assert json.loads((root / "installation.json").read_text()) == state
    if failure != "dependencies":
        assert commands[-1][1:3] == ["plugin", "add"]


def test_failed_fresh_install_removes_only_its_registration(tmp_path: Path, monkeypatch) -> None:
    checkout = source(tmp_path)
    root = tmp_path / "installed"
    commands = []
    monkeypatch.setattr(installer, "check_conflicts", lambda *args: None)
    monkeypatch.setattr(installer, "prepare_runtime", lambda *args: checkout)

    def run(command, **kwargs):
        commands.append(command)
        if "--installed-plugin" in command:
            raise RuntimeError("verification failed")
        return {"status": "ok", "alreadyAdded": False, "version": "new"}

    monkeypatch.setattr(installer, "run", run)
    with pytest.raises(RuntimeError, match="verification failed"):
        installer.install(checkout, root, tmp_path / "data", "codex", "uv")
    assert commands[-2][1:] == ["plugin", "remove", installer.PLUGIN_ID, "--json"]
    assert commands[-1][1:] == ["plugin", "marketplace", "remove", "ruminate", "--json"]
    assert not (root / "marketplace").exists()
    assert not (root / "installation.json").exists()


@pytest.mark.parametrize("config", [
    '[mcp_servers.ruminate]\ncommand="ruminate"\nenabled=false\n',
    '[plugins."ruminate@ruminate-local"]\nenabled=true\n',
])
def test_conflicting_server_is_not_overwritten(tmp_path: Path, monkeypatch, config: str) -> None:
    profile = tmp_path / "codex-profile"
    profile.mkdir()
    (profile / "config.toml").write_text(config)
    monkeypatch.setenv("CODEX_HOME", str(profile))
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: {"marketplaces": []})
    with pytest.raises(ValueError, match="Ruminate"):
        installer.check_conflicts("codex", tmp_path / "plugin")
    assert (profile / "config.toml").read_text() == config


@pytest.mark.parametrize("with_data", [True, False])
@pytest.mark.parametrize("uv_exit", [0, 23])
def test_bootstrap_forwards_paths_and_cleans_temporary_download(
    tmp_path: Path, with_data: bool, uv_exit: int,
) -> None:
    helpers = tmp_path / "helper tools"
    helpers.mkdir()
    codex, uv = helpers / "codex", helpers / "uv"
    log = tmp_path / "arguments"
    codex.write_text("#!/bin/sh\nexit 0\n")
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUMINATE_TEST_LOG"\n'
                  f"exit {uv_exit}\n")
    codex.chmod(0o700)
    uv.chmod(0o700)
    temp = tmp_path / "downloads"
    temp.mkdir()
    env = dict(os.environ, TMPDIR=str(temp), RUMINATE_TEST_LOG=str(log))
    env.pop("RUMINATE_DATA_DIR", None)
    extra = ["--data-dir", str(tmp_path / "my research")] if with_data else []
    result = subprocess.run([
        "bash", str(ROOT / "install.sh"), "--source", str(ROOT),
        "--install-dir", str(tmp_path / "plugin files"), "--codex", str(codex), "--uv", str(uv),
        *extra,
    ], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == uv_exit, result.stderr
    arguments = log.read_text().splitlines()
    assert (str(tmp_path / "my research") in arguments) is with_data
    assert str(codex) in arguments
    assert not list(temp.iterdir())


def test_old_codex_fails_before_installing_dependencies(tmp_path: Path) -> None:
    codex = tmp_path / "old-codex"
    codex.write_text("#!/bin/sh\nexit 2\n")
    codex.chmod(0o700)
    root = tmp_path / "not-created"
    result = subprocess.run([
        "bash", str(ROOT / "install.sh"), "--source", str(ROOT),
        "--install-dir", str(root), "--codex", str(codex),
    ], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "Update Codex" in result.stderr
    assert not root.exists()

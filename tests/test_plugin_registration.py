"""Project registration preserves unrelated settings and reviews exact proposed bytes."""

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from scripts.register_project_plugin import apply_plan, prepare

ROOT = Path(__file__).resolve().parents[1]


def project(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    shutil.copytree(ROOT / "plugins/ruminate", repo / "plugins/ruminate")
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    return repo, python


def test_registration_only_enables_project_plugin_and_preserves_existing_settings(
    tmp_path: Path,
) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = '# Existing policy\nmodel = "chosen-model"\n\n[features]\nexisting = true\n'
    config.write_text(original)
    marketplace = repo / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(json.dumps({"name": "team", "plugins": [{"name": "other"}]}))
    plan = prepare(repo, repo / "data", python)
    assert config.read_text() == original  # Preparation changes neither registration target.
    assert json.loads(marketplace.read_text())["plugins"] == [{"name": "other"}]
    assert plan["permissions_changed"] is False
    result = apply_plan(repo / ".local/codex-registration/plan.json")
    assert result["user_config_changed"] is False
    assert config.read_text().startswith(original.rstrip())
    assert '[plugins."ruminate@team"]\nenabled = true' in config.read_text()
    assert tomllib.loads(config.read_text())["marketplaces"]["team"] == {
        "source_type": "local",
        "source": str(repo),
    }
    assert json.loads(marketplace.read_text())["plugins"][0] == {"name": "other"}
    prepared = prepare(repo, repo / "data", python)
    assert not any(edit["changed"] for edit in prepared["edits"])
    assert apply_plan(repo / ".local/codex-registration/plan.json")["changed_files"] == []
    wiring = json.loads((repo / ".local/codex-plugin/ruminate/mcp.json").read_text())
    assert wiring["mcpServers"]["ruminate"]["command"] == str(python)
    assert wiring["mcpServers"]["ruminate"]["env"] == {"RUMINATE_MCP_WRITES": "1"}
    mirror = repo / ".local/codex-plugin/ruminate"
    manifest = json.loads((mirror / ".codex-plugin/plugin.json").read_text())
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Ruminate"
    legacy = json.loads((mirror / ".mcp.json").read_text())
    assert legacy["mcpServers"]["ruminate"]["command"] == str(python)
    assert "type" not in legacy["mcpServers"]["ruminate"]
    assert not (mirror / "plugin.json").exists()
    assert (repo / "plugins/ruminate/plugin.json").exists()


def test_registration_rejects_changes_after_review(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    prepare(repo, repo / "data", python)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("# A user edit\n")
    with pytest.raises(ValueError, match="changed after"):
        apply_plan(repo / ".local/codex-registration/plan.json")
    assert config.read_text() == "# A user edit\n"
    assert not (repo / ".agents/plugins/marketplace.json").exists()


def test_registration_rejects_symlinked_config_directory(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    other = tmp_path / "outside"
    other.mkdir()
    (repo / ".codex").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare(repo, repo / "data", python)
    assert list(other.iterdir()) == []


def test_registration_updates_only_existing_plugin_enabled_value(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        '# Keep this\n[plugins."ruminate@ruminate-local"]\nenabled = false # choice\n'
        'other = "preserve"\n\n[plugins."other@elsewhere"]\nenabled = false\n'
    )
    prepare(repo, repo / "data", python)
    apply_plan(repo / ".local/codex-registration/plan.json")
    updated = config.read_text()
    assert 'enabled = true # choice\nother = "preserve"' in updated
    assert '[plugins."other@elsewhere"]\nenabled = false' in updated


def test_registration_preserves_unrelated_marketplaces_and_rejects_name_reuse(
    tmp_path: Path,
) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = '[marketplaces.team]\nsource_type = "git"\nsource = "https://example.org/team"\n'
    config.write_text(original)
    prepare(repo, repo / "data", python)
    apply_plan(repo / ".local/codex-registration/plan.json")
    assert config.read_text().startswith(original.rstrip())
    unrelated = tomllib.loads(config.read_text())["marketplaces"]["team"]
    assert unrelated == {"source_type": "git", "source": "https://example.org/team"}
    config.write_text(
        '[marketplaces.ruminate-local]\nsource_type = "local"\nsource = "/elsewhere"\n'
    )
    before = config.read_bytes()
    with pytest.raises(ValueError, match="another source"):
        prepare(repo, repo / "data", python)
    assert config.read_bytes() == before


def test_direct_registration_preserves_other_servers_and_disables_duplicate_plugin(
    tmp_path: Path,
) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = (
        '# Project settings\n[mcp_servers.other]\ncommand = "keep-me"\n'
        '\n[plugins."ruminate@ruminate-local"]\nenabled = true # local choice\n'
        'custom = "keep-this"\n'
    )
    config.write_text(original)
    data = repo / "资料 with spaces"
    plan = prepare(repo, data, python, direct_mcp=True)
    assert config.read_text() == original
    assert plan["mode"] == "direct_mcp"
    result = apply_plan(repo / ".local/codex-registration/plan.json")
    assert result["mode"] == "direct_mcp"
    updated = tomllib.loads(config.read_text())
    assert updated["mcp_servers"]["other"] == {"command": "keep-me"}
    assert updated["plugins"]["ruminate@ruminate-local"] == {
        "enabled": False, "custom": "keep-this",
    }
    assert 'enabled = false # local choice' in config.read_text()
    assert updated["mcp_servers"]["ruminate"] == {
        "command": str(python),
        "args": ["-m", "ruminate", "--data-dir", str(data), "mcp", "--transport", "stdio"],
        "cwd": str(repo),
        "env": {"RUMINATE_MCP_WRITES": "1"},
    }
    assert updated["marketplaces"]["ruminate-local"]["source"] == str(repo)
    again = prepare(repo, data, python, direct_mcp=True)
    assert not any(edit["changed"] for edit in again["edits"])
    assert apply_plan(repo / ".local/codex-registration/plan.json")["changed_files"] == []
    with pytest.raises(ValueError, match="--direct-mcp"):
        prepare(repo, data, python)


def test_direct_registration_refuses_conflicting_existing_server(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = '[mcp_servers.ruminate]\ncommand = "my-existing-ruminate"\n'
    config.write_text(original)
    with pytest.raises(ValueError, match="MCP config differs"):
        prepare(repo, repo / "data", python, direct_mcp=True)
    assert config.read_text() == original
    assert not (repo / ".agents/plugins/marketplace.json").exists()


def test_direct_registration_preserves_matching_server_options(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    prepare(repo, repo / "data", python, direct_mcp=True)
    apply_plan(repo / ".local/codex-registration/plan.json")
    config = repo / ".codex/config.toml"
    content = config.read_text().replace(
        'env = { "RUMINATE_MCP_WRITES" = "1" }',
        'env = { RUMINATE_MCP_WRITES = "1", RUMINATE_NETWORK_MODE = "off" }\n'
        'startup_timeout_sec = 30 # keep startup behavior',
    )
    config.write_text(content)
    plan = prepare(repo, repo / "data", python, direct_mcp=True)
    assert not any(edit["changed"] for edit in plan["edits"])
    assert config.read_text() == content


def test_switch_to_plugin_owns_mcp_and_preserves_data_network_scope_and_other_settings(
    tmp_path: Path,
) -> None:
    repo, python = project(tmp_path)
    data = repo / "private research"
    prepare(repo, data, python, direct_mcp=True)
    apply_plan(repo / ".local/codex-registration/plan.json")
    config = repo / ".codex/config.toml"
    content = config.read_text().replace(
        'env = { "RUMINATE_MCP_WRITES" = "1" }',
        'env = { RUMINATE_MCP_WRITES = "1", RUMINATE_NETWORK_MODE = "off" }\n'
        'startup_timeout_sec = 30 # retain\n'
        '\n[mcp_servers.other]\ncommand = "unchanged"\n',
    )
    config.write_text(content)
    plan = prepare(repo, data, python, plugin_mcp=True)
    assert plan["mode"] == "plugin_mcp"
    assert config.read_text() == content
    apply_plan(repo / ".local/codex-registration/plan.json")
    parsed = tomllib.loads(config.read_text())
    assert parsed["plugins"]["ruminate@ruminate-local"]["enabled"] is True
    assert "ruminate" not in parsed["mcp_servers"]
    assert parsed["mcp_servers"]["other"] == {"command": "unchanged"}
    assert 'startup_timeout_sec = 30 # retain' in (
        repo / '.local/codex-registration/config.toml.before'
    ).read_text()
    transport = json.loads((repo / ".local/codex-plugin/ruminate/mcp.json").read_text())
    transport = transport["mcpServers"]["ruminate"]
    assert transport["args"][3] == str(data)
    assert transport["env"]["RUMINATE_NETWORK_MODE"] == "off"
    assert transport["startup_timeout_sec"] == 30
    assert "enabled" not in transport
    again = prepare(repo, data, python, plugin_mcp=True)
    assert not any(edit["changed"] for edit in again["edits"])
    # The backup and private mirror retain settings for a reversible switch back.
    prepare(repo, data, python, direct_mcp=True)
    apply_plan(repo / ".local/codex-registration/plan.json")
    parsed = tomllib.loads(config.read_text())
    assert parsed["plugins"]["ruminate@ruminate-local"]["enabled"] is False
    assert parsed["mcp_servers"]["ruminate"].get("enabled", True) is True
    assert parsed["mcp_servers"]["ruminate"]["env"]["RUMINATE_NETWORK_MODE"] == "off"


def test_plugin_switch_preserves_unknown_tool_restrictions_by_refusing_implicit_migration(
    tmp_path: Path,
) -> None:
    repo, python = project(tmp_path)
    prepare(repo, repo / "data", python, direct_mcp=True)
    apply_plan(repo / ".local/codex-registration/plan.json")
    config = repo / ".codex/config.toml"
    original = config.read_text() + 'disabled_tools = ["ui_write"]\n'
    config.write_text(original)
    with pytest.raises(ValueError, match="plugin-scoped equivalent"):
        prepare(repo, repo / "data", python, plugin_mcp=True)
    assert config.read_text() == original


def test_plugin_switch_rejects_foreign_server_and_conflicting_mode(tmp_path: Path) -> None:
    repo, python = project(tmp_path)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = '[mcp_servers.ruminate]\ncommand = "another-server"\n'
    config.write_text(original)
    with pytest.raises(ValueError, match="MCP config differs"):
        prepare(repo, repo / "data", python, plugin_mcp=True)
    assert config.read_text() == original
    with pytest.raises(ValueError, match="either"):
        prepare(repo, repo / "data", python, plugin_mcp=True, direct_mcp=True)

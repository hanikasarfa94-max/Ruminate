"""Prepare or apply project-only Codex plugin registration without changing user settings.

Run with the project's Python. The default prepares reviewable files below .local;
--apply writes only this project's marketplace and Codex configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _digest(body: bytes | None) -> str | None:
    return None if body is None else hashlib.sha256(body).hexdigest()


def _read(path: Path) -> bytes | None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"Registration path must not be a symlink: {path}")
    return path.read_bytes() if path.exists() else None


def _set_enabled(text: str, section: str, key: str, enabled_value: bool) -> str:
    parsed = tomllib.loads(text)
    entries = parsed.get(section, {})
    if not isinstance(entries, dict):
        raise ValueError(f"Existing {section} config must be a TOML table")
    current = entries.get(key)
    if current is None:
        value = str(enabled_value).lower()
        return text.rstrip() + f"\n\n[{section}.{json.dumps(key)}]\nenabled = {value}\n"
    if not isinstance(current, dict):
        raise ValueError(f"Existing {section}.{key} config must be a TOML table")
    if current.get("enabled") is enabled_value:
        return text
    # Preserve all unrelated settings and comments. Unusual inline/dotted TOML is left for
    # the caller to inspect rather than serializing and rewriting the entire user document.
    quoted = r'(?:"' + re.escape(key) + r'"|\'' + re.escape(key) + r"')"
    keys = f"(?:{quoted}|{re.escape(key)})" if re.fullmatch(r"[\w-]+", key) else quoted
    pattern = re.compile(r"(?m)^\[" + re.escape(section) + r"\." + keys + r"\][ \t]*(?:#.*)?$")
    match = pattern.search(text)
    if match is None:
        raise ValueError(
            f"Existing {section}.{key} uses inline/dotted TOML; edit its enabled field"
        )
    next_table = re.search(r"(?m)^\[", text[match.end() :])
    end = match.end() + next_table.start() if next_table else len(text)
    body = text[match.end() : end]
    enabled = re.compile(r"(?m)^(\s*enabled\s*=\s*)(?:true|false)(\s*(?:#.*)?)$")
    if "enabled" in current:
        body, count = enabled.subn(r"\g<1>" + str(enabled_value).lower() + r"\2", body)
        if count != 1:
            raise ValueError("Cannot safely update the existing Ruminate enabled value")
    else:
        body = f"\nenabled = {str(enabled_value).lower()}\n" + body.lstrip("\n")
    return text[: match.end()] + body + text[end:]


def _set_plugin_enabled(text: str, key: str, enabled_value: bool) -> str:
    return _set_enabled(text, "plugins", key, enabled_value)


def _remove_direct_server(text: str) -> str:
    """A disabled top-level server still overrides the bundled server with the same name."""
    headers = list(re.finditer(r"(?m)^[ \t]*\[.*$", text))
    owned = re.compile(r'''^\s*\[mcp_servers\.(?:ruminate|"ruminate"|'ruminate')(?:\.|\])''')
    for index in reversed(range(len(headers))):
        header = headers[index]
        if owned.match(header.group()):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            text = text[:header.start()] + text[end:]
    if "ruminate" in tomllib.loads(text).get("mcp_servers", {}):
        raise ValueError("Cannot safely remove the existing inline/dotted MCP table")
    return text


def _validate_direct_server(current: Any, repo: Path, data: Path, python: Path) -> None:
    expected = {
        "command": str(python),
        "args": ["-m", "ruminate", "--data-dir", str(data), "mcp", "--transport", "stdio"],
        "cwd": str(repo),
    }
    if (
        not isinstance(current, dict)
        or any(current.get(key) != value for key, value in expected.items())
        or not isinstance(current.get("env"), dict)
        or current["env"].get("RUMINATE_MCP_WRITES") != "1"
    ):
        raise ValueError("Existing Ruminate MCP config differs; review it before registration")


def _configure_marketplace(text: str, name: str, repo: Path) -> str:
    configured = tomllib.loads(text).get("marketplaces", {})
    if not isinstance(configured, dict):
        raise ValueError("Existing marketplaces config must be a TOML table")
    current = configured.get(name)
    expected = {"source_type": "local", "source": str(repo)}
    if current is not None:
        if not isinstance(current, dict) or any(current.get(k) != v for k, v in expected.items()):
            raise ValueError("This marketplace name is already configured for another source")
        return text
    return (
        text.rstrip()
        + f'\n\n[marketplaces.{json.dumps(name)}]\nsource_type = "local"\n'
        + "source = "
        + json.dumps(str(repo), ensure_ascii=False)
        + "\n"
    )


def _configure_direct_mcp(
    text: str, repo: Path, data: Path, python: Path, transport: dict[str, Any]
) -> str:
    servers = tomllib.loads(text).get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("Existing mcp_servers config must be a TOML table")
    args = ["-m", "ruminate", "--data-dir", str(data), "mcp", "--transport", "stdio"]
    current = servers.get("ruminate")
    if current is not None:
        _validate_direct_server(current, repo, data, python)
        return text if current.get("enabled", True) else _set_enabled(
            text, "mcp_servers", "ruminate", True
        )
    env = ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in transport["env"].items())
    # Reuse scoped environment and timeouts when switching back from a plugin mirror.
    extra = "".join(
        f"{key} = {json.dumps(transport[key])}\n"
        for key in ("env_vars", "startup_timeout_sec", "tool_timeout_sec") if key in transport
    )
    return (
        text.rstrip()
        + "\n\n[mcp_servers.ruminate]\n"
        + "command = " + json.dumps(str(python), ensure_ascii=False) + "\n"
        + "args = " + json.dumps(args, ensure_ascii=False) + "\n"
        + "cwd = " + json.dumps(str(repo), ensure_ascii=False) + "\n"
        + "env = { " + env + " }\n" + extra
    )


def prepare(
    repo: Path, data: Path, python: Path, *, direct_mcp: bool = False, plugin_mcp: bool = False
) -> dict[str, Any]:
    if direct_mcp and plugin_mcp:
        raise ValueError("Choose either direct MCP or plugin MCP registration")
    repo, data = repo.resolve(), data.expanduser().resolve()
    python = python.expanduser().absolute()  # Keep the virtualenv launcher path.
    if not python.is_file():
        raise ValueError(f"Python launcher not found: {python}")
    source = repo / "plugins" / "ruminate"
    if not (source / "plugin.json").is_file():
        raise ValueError("Project plugin source is missing")
    local = repo / ".local" / "codex-registration"
    plugin = repo / ".local" / "codex-plugin" / "ruminate"
    for path in (local, plugin):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Local registration directories must not be symlinks")
    previous_wiring = _read(plugin / "mcp.json")
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    plugin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(source, plugin, dirs_exist_ok=True)
    wiring = json.loads((source / "mcp.json").read_text())
    wiring["mcpServers"]["ruminate"] = {
        "type": "stdio",
        "command": str(python),
        "args": ["-m", "ruminate", "--data-dir", str(data), "mcp", "--transport", "stdio"],
        "cwd": str(repo),
        "env": {"RUMINATE_MCP_WRITES": "1"},
    }
    marketplace_path = repo / ".agents" / "plugins" / "marketplace.json"
    original_marketplace = _read(marketplace_path)
    marketplace = (
        json.loads(original_marketplace)
        if original_marketplace is not None
        else {
            "name": "ruminate-local",
            "interface": {"displayName": "Ruminate 本地开发"},
            "plugins": [],
        }
    )
    if (
        not isinstance(marketplace, dict)
        or not isinstance(marketplace.get("name"), str)
        or not isinstance(marketplace.get("plugins"), list)
        or not all(isinstance(item, dict) for item in marketplace.get("plugins", []))
    ):
        raise ValueError("Existing project marketplace is not a supported catalog")
    name = marketplace["name"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ValueError("Marketplace name is not a safe local identifier")
    entry = {
        "name": "ruminate",
        "source": {"source": "local", "path": "./.local/codex-plugin/ruminate"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    existing = [
        i for i, item in enumerate(marketplace["plugins"]) if item.get("name") == "ruminate"
    ]
    if len(existing) > 1:
        raise ValueError("Existing marketplace has duplicate Ruminate entries")
    if existing:
        marketplace["plugins"][existing[0]] = entry
    else:
        marketplace["plugins"].append(entry)
    config_path = repo / ".codex" / "config.toml"
    original_config = _read(config_path)
    config = (original_config or b"").decode("utf-8")
    current = tomllib.loads(config).get("mcp_servers", {}).get("ruminate")
    if current is None and previous_wiring is not None:
        # The private mirror retains transport options after the standalone entry is removed.
        previous = json.loads(previous_wiring)["mcpServers"]["ruminate"]
        _validate_direct_server(previous, repo, data, python)
        wiring["mcpServers"]["ruminate"] = previous
    if not direct_mcp and current is not None:
        if not plugin_mcp:
            raise ValueError(
                "Ruminate MCP is configured directly; use --direct-mcp or --plugin-mcp"
            )
        _validate_direct_server(current, repo, data, python)
        portable_keys = {
            "command", "args", "cwd", "env", "env_vars",
            "startup_timeout_sec", "tool_timeout_sec", "enabled",
        }
        if set(current) - portable_keys:
            raise ValueError(
                "Existing MCP policy needs a plugin-scoped equivalent before switching"
            )
        # Preserve the current data path, network mode and file scopes when changing ownership.
        wiring["mcpServers"]["ruminate"] = {
            "type": "stdio", **{key: value for key, value in current.items() if key != "enabled"},
        }
        config = _remove_direct_server(config)
    config = _set_plugin_enabled(config, f"ruminate@{name}", not direct_mcp)
    config = _configure_marketplace(config, name, repo)
    if direct_mcp:
        config = _configure_direct_mcp(config, repo, data, python, wiring["mcpServers"]["ruminate"])
    tomllib.loads(config)
    (plugin / "mcp.json").write_text(json.dumps(wiring, ensure_ascii=False, indent=2) + "\n")
    # Codex 0.167 currently discovers bundled MCP through the compatibility manifest.
    # Keep the source package portable and generate this desktop mirror from it.
    manifest = json.loads((source / "plugin.json").read_text())
    compatibility = {key: manifest[key] for key in ("name", "version", "description")}
    compatibility.update(manifest.get("extensions", {}).get("com.openai", {}))
    compatibility.update({"mcpServers": "./.mcp.json", "skills": "./skills/"})
    manifest_dir = plugin / ".codex-plugin"
    if manifest_dir.is_symlink():
        raise ValueError("Plugin compatibility directory must not be a symlink")
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(compatibility, ensure_ascii=False, indent=2) + "\n"
    )
    legacy_mcp = {
        "mcpServers": {
            name: {key: value for key, value in transport.items() if key != "type"}
            for name, transport in wiring["mcpServers"].items()
        }
    }
    (plugin / ".mcp.json").write_text(json.dumps(legacy_mcp, ensure_ascii=False, indent=2) + "\n")
    (plugin / "plugin.json").unlink()  # Generated copy; its portable source stays in plugins/.
    contents = {
        "marketplace.json": (
            marketplace_path,
            original_marketplace,
            json.dumps(marketplace, ensure_ascii=False, indent=2).encode() + b"\n",
        ),
        "config.toml": (config_path, original_config, config.encode()),
    }
    edits = []
    for filename, (target, previous, proposed) in contents.items():
        proposed_path = local / filename
        proposed_path.write_bytes(proposed)
        proposed_path.chmod(0o600)
        if previous is not None:
            (local / (filename + ".before")).write_bytes(previous)
            (local / (filename + ".before")).chmod(0o600)
        edits.append(
            {
                "target": str(target),
                "proposed": str(proposed_path),
                "before_sha256": _digest(previous),
                "after_sha256": _digest(proposed),
                "changed": previous != proposed,
            }
        )
    plan = {
        "project": str(repo),
        "plugin": "ruminate",
        "marketplace": name,
        "data_dir": str(data),
        "runtime": str(python),
        "mode": "direct_mcp" if direct_mcp else "plugin_mcp" if plugin_mcp else "plugin",
        "edits": edits,
        "user_config_changed": False,
        "permissions_changed": False,
    }
    (local / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    (local / "plan.json").chmod(0o600)
    return plan


def apply_plan(plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text())
    repo = Path(plan["project"]).resolve()
    expected = {repo / ".agents/plugins/marketplace.json", repo / ".codex/config.toml"}
    if {Path(edit["target"]) for edit in plan["edits"]} != expected:
        raise ValueError("Plan contains targets outside project registration files")
    planned = []
    for edit in plan["edits"]:
        target = Path(edit["target"])
        before = _read(target)
        proposed = Path(edit["proposed"]).read_bytes()
        if _digest(before) != edit["before_sha256"] or _digest(proposed) != edit["after_sha256"]:
            raise ValueError("Files changed after preparation; prepare and review again")
        planned.append((target, before, proposed))
    applied = []
    try:
        for target, before, proposed in planned:
            if before == proposed:
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(proposed)
            temporary.chmod(0o600)
            temporary.replace(target)
            applied.append((target, before))
    except BaseException:
        for target, previous in reversed(applied):
            if previous is None:
                target.unlink()
            else:
                target.write_bytes(previous)
        raise
    return {
        "status": "registered",
        "project": str(repo),
        "marketplace": plan["marketplace"],
        "mode": plan.get("mode", "plugin"),
        "changed_files": [str(path) for path, _ in applied],
        "user_config_changed": False,
        "permissions_changed": False,
        "next_step": "Refresh the current project's MCP configuration and call open_workspace",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--python", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--direct-mcp", action="store_true")
    mode.add_argument("--plugin-mcp", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply:
            result = apply_plan(args.project / ".local/codex-registration/plan.json")
        else:
            if args.data_dir is None:
                parser.error("--data-dir is required when preparing registration")
            result = prepare(
                args.project, args.data_dir, args.python or args.project / ".venv/bin/python",
                direct_mcp=args.direct_mcp, plugin_mcp=args.plugin_mcp,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None

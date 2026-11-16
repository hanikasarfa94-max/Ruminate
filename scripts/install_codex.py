"""Install a private runtime and register its workspace through public Codex commands.

The shell bootstrap supplies uv/Python. This script requires only the standard library;
the installed application and the actual Codex app-server validate the result.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

MARKETPLACE = "ruminate"
PLUGIN_ID = "ruminate@ruminate"
RUNTIME_ITEMS = (
    "ruminate", "plugins/ruminate", "pyproject.toml", "uv.lock", "README.md",
    "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
)


def run(command: list[str], *, cwd: Path, timeout: int = 60) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"{Path(command[0]).name} failed: {message}")
    return json.loads(result.stdout)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    Path(stream.name).replace(path)


def runtime_files(source: Path) -> list[Path]:
    files = []
    for name in RUNTIME_ITEMS:
        item = source / name
        if not item.exists():
            raise ValueError(f"Source is missing {name}")
        if item.is_symlink():
            raise ValueError("Runtime source must not contain symlinks")
        for file in sorted(item.rglob("*") if item.is_dir() else [item]):
            if "__pycache__" in file.parts or file.name == ".DS_Store":
                continue
            if file.is_symlink():
                raise ValueError("Runtime source must not contain symlinks")
            if file.is_file():
                files.append(file.relative_to(source))
    return files


def prepare_runtime(source: Path, root: Path, uv: str) -> Path:
    files = runtime_files(source)
    digest = hashlib.sha256()
    for file in files:
        digest.update(str(file).encode() + b"\0")
        digest.update(hashlib.sha256((source / file).read_bytes()).digest())
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid release version")
    runtime = root / "runtimes" / f"{version}-{digest.hexdigest()[:16]}"
    for file in files:
        target = runtime / file
        if target.is_file() and target.read_bytes() == (source / file).read_bytes():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / file, target)
    # Paths are final before uv creates the environment: venv launchers cannot be relocated.
    result = subprocess.run(
        [uv, "sync", "--locked", "--no-dev", "--managed-python", "--python", "3.11",
         "--project", str(runtime)],
        cwd=root, timeout=600,
    )
    if result.returncode:
        raise RuntimeError("Dependency installation failed. Re-run the installer to retry.")
    return runtime


def choose_data(root: Path, requested: Path | None, previous: dict[str, Any]) -> Path:
    saved = previous.get("data_dir")
    if saved and requested and Path(saved).resolve() != requested.expanduser().resolve():
        raise ValueError(
            "This installation already uses another data directory; keep its data path."
        )
    if requested:
        data = requested.expanduser().resolve()
    elif saved:
        data = Path(saved).resolve()
    elif sys.platform == "darwin":
        data = Path.home() / "Library/Application Support/Ruminate"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ruminate"
    data = data.resolve()
    if data == root or root in data.parents or data in root.parents:
        raise ValueError("Keep the research data directory separate from the plugin installation.")
    return data


def check_conflicts(codex: str, root: Path) -> None:
    catalogs = run([codex, "plugin", "marketplace", "list", "--json"], cwd=root)
    for entry in catalogs["marketplaces"]:
        if entry["name"] == MARKETPLACE and Path(entry["root"]).resolve() != root / "marketplace":
            raise ValueError("A different Ruminate marketplace is registered; use its installer.")
    profile = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config = profile / "config.toml"
    current = tomllib.loads(config.read_text()) if config.exists() else {}
    if "ruminate" in current.get("mcp_servers", {}):
        raise ValueError(
            "A standalone Ruminate MCP is configured. Remove that entry before installing the "
            "plugin; its data stays in place. See docs/INSTALL.md."
        )
    for plugin, settings in current.get("plugins", {}).items():
        if plugin.startswith("ruminate@") and plugin != PLUGIN_ID and settings.get("enabled", True):
            raise ValueError(
                "Another Ruminate plugin is enabled. Keep using that installation, or disable "
                "it and install with --data-dir pointing to its existing store."
            )


def build_marketplace(runtime: Path, destination: Path, data: Path, old: Path) -> None:
    source = runtime / "plugins/ruminate"
    plugin = destination / "plugins/ruminate"
    shutil.copytree(source, plugin)
    manifest = json.loads((plugin / "plugin.json").read_text())
    compatibility = {key: manifest[key] for key in ("name", "version", "description")}
    compatibility.update(manifest.get("extensions", {}).get("com.openai", {}))
    compatibility.update({"mcpServers": "./.mcp.json", "skills": "./skills/"})
    transport = {"env": {"RUMINATE_MCP_WRITES": "1"}}
    old_mcp = old / "plugins/ruminate/.mcp.json"
    if old_mcp.exists():
        # Keep user-scoped network/file permissions and timeouts on updates.
        transport = json.loads(old_mcp.read_text())["mcpServers"]["ruminate"]
    transport.update({
        "command": str(runtime / ".venv/bin/python"),
        "args": ["-m", "ruminate", "--data-dir", str(data), "mcp", "--transport", "stdio"],
        "cwd": str(runtime),
    })
    write_json(plugin / ".codex-plugin/plugin.json", compatibility)
    write_json(plugin / ".mcp.json", {"mcpServers": {"ruminate": transport}})
    (plugin / "plugin.json").unlink()
    (plugin / "mcp.json").unlink()
    write_json(destination / ".agents/plugins/marketplace.json", {
        "name": MARKETPLACE,
        "interface": {"displayName": "Ruminate"},
        "plugins": [{
            "name": "ruminate",
            "source": {"source": "local", "path": "./plugins/ruminate"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }],
    })


def install(source: Path, root: Path, data: Path | None, codex: str, uv: str) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another Ruminate installation is running.") from None
        state = root / "installation.json"
        previous = json.loads(state.read_text()) if state.exists() else {}
        if previous and previous.get("plugin_id") != PLUGIN_ID:
            raise ValueError("Installation directory belongs to another package")
        data = choose_data(root, data, previous)
        marketplace = root / "marketplace"
        if marketplace.exists() and not previous:
            raise ValueError("Unrecognized marketplace directory; choose another --install-dir")
        # Run outside the source tree, avoiding developer-only project configuration.
        check_conflicts(codex, root)
        print("[2/4] Installing the local runtime…", flush=True)
        runtime = prepare_runtime(source, root, uv)
        python = str(runtime / ".venv/bin/python")
        health = run([python, "-m", "ruminate", "--data-dir", str(data), "init"], cwd=root)
        if health.get("status") != "ok":
            raise RuntimeError("The research store did not pass its health check")
        with tempfile.TemporaryDirectory(prefix=".registration-", dir=root) as directory:
            stage = Path(directory)
            prepared, backup = stage / "next", stage / "previous"
            build_marketplace(runtime, prepared, data, marketplace)
            if marketplace.exists():
                marketplace.rename(backup)
            prepared.rename(marketplace)
            added_marketplace = False
            try:
                print("[3/4] Adding Ruminate to the Codex plugin library…", flush=True)
                added = run([codex, "plugin", "marketplace", "add", str(marketplace), "--json"],
                            cwd=root)
                added_marketplace = not added.get("alreadyAdded", False)
                registered = run([codex, "plugin", "add", PLUGIN_ID, "--json"], cwd=root)
                print("[4/4] Checking the installed workspace through Codex…", flush=True)
                report = run([
                    python, str(source / "scripts/probe_codex_mcp_ui.py"), "--installed-plugin",
                    "--codex", codex, "--marketplace-path",
                    str(marketplace / ".agents/plugins/marketplace.json"),
                ], cwd=root, timeout=300)
                if report.get("status") != "SUPPORTED":
                    raise RuntimeError("Codex could not load the installed workspace")
                outcome = {
                    "plugin_id": PLUGIN_ID, "version": registered["version"],
                    "runtime": str(runtime), "data_dir": str(data), "codex": codex,
                    "marketplace": str(marketplace), "verification": report,
                }
                write_json(state, outcome)
            except BaseException:
                # Restore plugin wiring, never delete or roll back research data.
                shutil.rmtree(marketplace)
                if backup.exists():
                    backup.rename(marketplace)
                    recovery = [["plugin", "add", PLUGIN_ID, "--json"]]
                elif added_marketplace:
                    recovery = [
                        ["plugin", "remove", PLUGIN_ID, "--json"],
                        ["plugin", "marketplace", "remove", MARKETPLACE, "--json"],
                    ]
                else:
                    recovery = []
                for command in recovery:
                    try:
                        run([codex, *command], cwd=root)
                    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                        print("Codex could not finish restoring the previous registration; "
                              "re-run the installer after restarting Codex.", file=sys.stderr)
                raise
        return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--uv", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = install(args.source.resolve(), args.install_dir.expanduser().resolve(),
                         args.data_dir, str(Path(args.codex).absolute()),
                         str(Path(args.uv).absolute()))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1
    print(f"\nRuminate {result['version']} is installed. / Ruminate 已安装。")
    print("Reopen Codex → Plugins → Ruminate → Open plugin.")
    print("重新打开 Codex → 插件 → Ruminate → 打开插件。")
    print(f"Research data / 资料目录: {result['data_dir']}")
    print("Re-run this installer to update; your research data is preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

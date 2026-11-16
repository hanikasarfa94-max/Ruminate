#!/usr/bin/env bash
# Download this file, then run it with bash, or pipe it to bash.
# Keep execution inside main so an incomplete download cannot run half the installer.
main() {
  set -euo pipefail
  umask 077
  local install_dir="${RUMINATE_INSTALL_DIR:-}" source_dir="" data_dir="${RUMINATE_DATA_DIR:-}"
  local codex_bin="${RUMINATE_CODEX:-}" uv_bin="${RUMINATE_UV:-}" arg candidate stage
  local ref="${RUMINATE_REF:-v0.5.3}"
  while [ "$#" -gt 0 ]; do
    arg="$1"
    case "$arg" in
      --help|-h)
        cat <<'HELP'
Install Ruminate into your Codex plugin library (macOS / Linux).

  bash install.sh [--data-dir DIR] [--install-dir DIR] [--codex FILE] [--uv FILE]
  bash install.sh --source /path/to/Ruminate   # install a local checkout

Re-run to update. Research data is preserved. No sudo, API key or Git login needed.
RUMINATE_REF selects a GitHub tag or commit (default: v0.5.3).
HELP
        return 0 ;;
      --source|--data-dir|--install-dir|--codex|--uv)
        [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "Missing value for $arg" >&2; return 1; }
        case "$arg" in
          --source) source_dir="$2" ;;
          --data-dir) data_dir="$2" ;;
          --install-dir) install_dir="$2" ;;
          --codex) codex_bin="$2" ;;
          --uv) uv_bin="$2" ;;
        esac
        shift 2 ;;
      *) echo "Unknown option: $arg (use --help)" >&2; return 1 ;;
    esac
  done
  case "$(uname -s)" in
    Darwin) install_dir="${install_dir:-$HOME/Library/Application Support/RuminatePlugin}" ;;
    Linux) install_dir="${install_dir:-${XDG_DATA_HOME:-$HOME/.local/share}/ruminate-plugin}" ;;
    *) echo "Ruminate currently supports macOS and Linux." >&2; return 1 ;;
  esac
  if [ -z "$codex_bin" ]; then
    codex_bin="$(command -v codex || true)"
    if [ -z "$codex_bin" ]; then
      for candidate in \
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex" \
        "$HOME/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex" \
        "/Applications/Codex.app/Contents/Resources/codex" \
        "$HOME/Applications/Codex.app/Contents/Resources/codex"; do
        if [ -x "$candidate" ]; then codex_bin="$candidate"; break; fi
      done
    fi
  fi
  if [ -z "$codex_bin" ] || [ ! -x "$codex_bin" ]; then
    echo "Codex not found. Install the desktop app, or pass --codex /path/to/codex." >&2
    return 1
  fi
  if ! "$codex_bin" plugin marketplace add --help >/dev/null 2>&1 \
    || ! "$codex_bin" plugin add --help >/dev/null 2>&1; then
    echo "This Codex version does not support plugin installation. Update Codex first." >&2
    return 1
  fi
  command -v curl >/dev/null || { echo "curl is required." >&2; return 1; }
  stage="$(mktemp -d "${TMPDIR:-/tmp}/ruminate-install.XXXXXXXX")"
  ruminate_install_stage="$stage"
  trap 'ruminate_exit_code=$?; rm -rf -- "$ruminate_install_stage"; exit "$ruminate_exit_code"' EXIT
  if [ -z "$source_dir" ]; then
    case "$ref" in
      *[!a-zA-Z0-9._-]*|'') echo "RUMINATE_REF must be a tag or commit." >&2; return 1 ;;
    esac
    echo "[1/4] Downloading Ruminate $ref from GitHub…"
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 20 --max-time 180 \
      "https://codeload.github.com/hanikasarfa94-max/Ruminate/tar.gz/$ref" -o "$stage/source.tar.gz"
    mkdir "$stage/source"
    tar -xzf "$stage/source.tar.gz" -C "$stage/source" --strip-components=1
    source_dir="$stage/source"
  fi
  [ -f "$source_dir/scripts/install_codex.py" ] || { echo "Incomplete Ruminate source." >&2; return 1; }
  source_dir="$(cd "$source_dir" && pwd -P)"
  mkdir -p "$install_dir"
  install_dir="$(cd "$install_dir" && pwd -P)"
  if [ -z "$uv_bin" ]; then
    if [ -x "$install_dir/tools/uv" ]; then uv_bin="$install_dir/tools/uv";
    else uv_bin="$(command -v uv || true)"; fi
  fi
  if [ -z "$uv_bin" ]; then
    echo "[2/4] Preparing uv and Python…"
    curl --fail --silent --show-error --location --retry 2 --connect-timeout 20 --max-time 180 \
      https://astral.sh/uv/0.12.9/install.sh -o "$stage/uv-install.sh"
    UV_UNMANAGED_INSTALL="$install_dir/tools" sh "$stage/uv-install.sh"
    uv_bin="$install_dir/tools/uv"
  fi
  [ -x "$uv_bin" ] || { echo "uv executable not found: $uv_bin" >&2; return 1; }
  set -- "$source_dir/scripts/install_codex.py" --source "$source_dir" \
    --install-dir "$install_dir" --codex "$codex_bin" --uv "$uv_bin"
  if [ -n "$data_dir" ]; then set -- "$@" --data-dir "$data_dir"; fi
  # Python and caches belong to this installation; no shell profile edits are needed.
  UV_PYTHON_INSTALL_DIR="$install_dir/python" UV_CACHE_DIR="$install_dir/cache" \
    "$uv_bin" run --no-project --managed-python --python 3.11 python "$@"
}

main "$@"

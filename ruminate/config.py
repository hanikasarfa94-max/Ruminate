"""Configuration is read explicitly, with no directory creation or network access."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ruminate.errors import RuminateError


def default_data_dir(environ: Mapping[str, str], platform: str = sys.platform) -> Path:
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ruminate"
    if platform == "win32":
        return (
            Path(environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Ruminate"
        )
    return Path(environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ruminate"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    network_mode: str = "ask"
    allowed_roots: tuple[Path, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    max_source_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 500
    parse_timeout: float = 10.0
    fetch_timeout: float = 10.0
    root_scopes: tuple[tuple[Path, bool], ...] = field(default=(), repr=False)
    review_timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.review_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise RuminateError("invalid_config", "Unknown review timezone") from exc
        if self.network_mode not in {"ask", "off", "allowlist"}:
            raise RuminateError("invalid_config", "network_mode must be ask, off or allowlist")
        if self.max_source_bytes <= 0 or self.max_pdf_pages <= 0:
            raise RuminateError("invalid_config", "Source size and page limits must be positive")
        if not 0 < self.parse_timeout <= 60 or not 0 < self.fetch_timeout <= 60:
            raise RuminateError("invalid_config", "Parser and fetch timeouts must be in (0, 60]")
        try:
            object.__setattr__(self, "data_dir", self.data_dir.expanduser().resolve())
            roots = tuple(p.expanduser().resolve() for p in self.allowed_roots)
        except (OSError, RuntimeError) as exc:
            raise RuminateError(
                "invalid_config", "Cannot resolve configured storage or file scopes"
            ) from exc
        previous_scopes = dict(self.root_scopes)
        object.__setattr__(self, "allowed_roots", roots)
        # Freeze file-vs-directory grants. Replacing a granted file with a folder
        # must not widen authorization; dataclasses.replace preserves known grants.
        object.__setattr__(
            self,
            "root_scopes",
            tuple(
                (root, previous_scopes[root] if root in previous_scopes else root.is_dir())
                for root in roots
            ),
        )
        object.__setattr__(
            self, "allowed_hosts", tuple(h.lower().rstrip(".") for h in self.allowed_hosts)
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "ruminate.sqlite3"

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "Config":
        env = os.environ
        directory = data_dir or Path(env.get("RUMINATE_DATA_DIR") or default_data_dir(env))
        roots = tuple(Path(p) for p in env.get("RUMINATE_ALLOWED_ROOTS", "").split(os.pathsep) if p)
        hosts = tuple(
            h.strip() for h in env.get("RUMINATE_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
        try:
            return cls(
                directory,
                env.get("RUMINATE_NETWORK_MODE", "ask"),
                roots,
                hosts,
                max_source_bytes=int(env.get("RUMINATE_MAX_SOURCE_BYTES", str(20 * 1024 * 1024))),
                max_pdf_pages=int(env.get("RUMINATE_MAX_PDF_PAGES", "500")),
                parse_timeout=float(env.get("RUMINATE_PARSE_TIMEOUT", "10")),
                fetch_timeout=float(env.get("RUMINATE_FETCH_TIMEOUT", "10")),
                review_timezone=env.get("RUMINATE_REVIEW_TIMEZONE", "Asia/Shanghai"),
            )
        except ValueError as exc:
            raise RuminateError("invalid_config", "Invalid numeric resource limit") from exc

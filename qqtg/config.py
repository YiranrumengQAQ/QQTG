"""Bootstrap configuration.

Only the *bootstrap* values live here (paths, bind address, secret key).
Everything the user can tune at runtime lives in the ``settings`` table and is
edited from the web panel (see ``qqtg.settings``).

Resolution order for every value: environment variable -> ``config.env`` file
-> default.  ``config.env`` is a plain ``KEY=VALUE`` file created by
``install.sh`` (or by ``qqtg init``).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = "/opt/qqtg-bridge"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass
class Config:
    home: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    tmp_dir: Path
    db_path: Path
    bind: str = "127.0.0.1"
    port: int = 8321
    secret_key: str = ""
    log_level: str = "INFO"
    public_url: str = ""  # optional, e.g. https://bridge.example.com (shown in panel)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def env_file(self) -> Path:
        return self.config_dir / "config.env"

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.data_dir, self.logs_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_config(home: str | None = None) -> Config:
    home_path = Path(home or os.environ.get("QQTG_HOME") or DEFAULT_HOME).expanduser()
    config_dir = Path(os.environ.get("QQTG_CONFIG_DIR") or home_path / "config")
    file_values = _parse_env_file(config_dir / "config.env")

    def get(key: str, default: str) -> str:
        return os.environ.get(key) or file_values.get(key) or default

    data_dir = Path(get("QQTG_DATA_DIR", str(home_path / "data")))
    cfg = Config(
        home=home_path,
        config_dir=config_dir,
        data_dir=data_dir,
        logs_dir=Path(get("QQTG_LOGS_DIR", str(home_path / "logs"))),
        tmp_dir=Path(get("QQTG_TMP_DIR", str(home_path / "tmp"))),
        db_path=Path(get("QQTG_DB_PATH", str(data_dir / "bridge.db"))),
        bind=get("QQTG_BIND", "127.0.0.1"),
        port=int(get("QQTG_PORT", "8321")),
        secret_key=get("QQTG_SECRET_KEY", ""),
        log_level=get("QQTG_LOG_LEVEL", "INFO").upper(),
        public_url=get("QQTG_PUBLIC_URL", "").rstrip("/"),
        ffmpeg=get("QQTG_FFMPEG", "ffmpeg"),
        ffprobe=get("QQTG_FFPROBE", "ffprobe"),
        extra=file_values,
    )
    return cfg


def write_env_file(cfg: Config, values: dict[str, str]) -> None:
    """Write (or rewrite) ``config.env`` preserving unknown keys."""
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    existing = _parse_env_file(cfg.env_file)
    existing.update(values)
    lines = [
        "# QQTG Bridge bootstrap configuration.",
        "# Runtime settings are managed in the web panel; only edit this file for",
        "# bind address / port / paths.  Never share QQTG_SECRET_KEY.",
        "",
    ]
    for key in sorted(existing):
        lines.append(f"{key}={existing[key]}")
    cfg.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg.env_file, 0o600)
    except OSError:
        pass


def generate_secret_key() -> str:
    return secrets.token_urlsafe(48)

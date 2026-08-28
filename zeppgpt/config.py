"""Configuration loading with no third-party dependency."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOSTS = (
    "https://api-mifit.zepp.com",
    "https://api-mifit.huami.com",
)
ALLOWED_HOST_SUFFIXES = (".zepp.com", ".huami.com")


class ConfigError(ValueError):
    """Raised when credential or host configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class ZeppConfig:
    app_token: str
    user_id: str
    api_hosts: tuple[str, ...]
    timeout_seconds: float = 20.0
    source: str = "run.mifit.huami.com"
    env_path: Path | None = None

    @classmethod
    def load(
        cls,
        env_path: str | Path = ".env",
        *,
        environ: dict[str, str] | None = None,
        require_credentials: bool = True,
    ) -> ZeppConfig:
        path = Path(env_path)
        file_values = _read_env_file(path) if path.is_file() else {}
        source_environ = os.environ if environ is None else environ

        def value(name: str, default: str = "") -> str:
            return source_environ.get(name, file_values.get(name, default)).strip()

        app_token = value("ZEPP_APP_TOKEN")
        user_id = value("ZEPP_USER_ID")
        if require_credentials:
            missing = [
                name
                for name, configured in (
                    ("ZEPP_APP_TOKEN", app_token),
                    ("ZEPP_USER_ID", user_id),
                )
                if not configured
            ]
            if missing:
                raise ConfigError(
                    "Missing required configuration: " + ", ".join(missing)
                )

        primary_host = value("ZEPP_API_HOST")
        extra_hosts = [
            item.strip() for item in value("ZEPP_API_HOSTS").split(",") if item.strip()
        ]
        configured_hosts = ([primary_host] if primary_host else []) + extra_hosts
        hosts = tuple(_deduplicate(_validate_host(host) for host in configured_hosts))
        if not hosts:
            hosts = DEFAULT_HOSTS

        try:
            timeout = float(value("ZEPP_REQUEST_TIMEOUT_SECONDS", "20"))
        except ValueError as exc:
            raise ConfigError("ZEPP_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
        if not 1 <= timeout <= 120:
            raise ConfigError("ZEPP_REQUEST_TIMEOUT_SECONDS must be between 1 and 120")

        source = value("ZEPP_SOURCE", "run.mifit.huami.com")
        if not source:
            raise ConfigError("ZEPP_SOURCE cannot be empty")

        return cls(
            app_token=app_token,
            user_id=user_id,
            api_hosts=hosts,
            timeout_seconds=timeout,
            source=source,
            env_path=path if path.is_file() else None,
        )

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.env_path is None:
            warnings.append(
                "No .env file found; relying on process environment variables."
            )
        elif os.name != "nt":
            try:
                mode = self.env_path.stat().st_mode & 0o777
                if mode & 0o077:
                    warnings.append(
                        f"{self.env_path} is readable by group/others; consider chmod 600."
                    )
            except OSError:
                pass
        if self.api_hosts == DEFAULT_HOSTS:
            warnings.append(
                "ZEPP_API_HOST is not set; only the two global Zepp/Huami hosts will be tried."
            )
        return warnings


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry at {path}:{line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            raise ConfigError(f"Invalid .env name at {path}:{line_number}")
        values[name] = _unquote(raw_value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _validate_host(host: str) -> str:
    normalized = host.rstrip("/")
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        raise ConfigError(f"ZEPP API host must be an HTTPS origin: {host!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            f"ZEPP API host must not contain credentials or query data: {host!r}"
        )
    if parsed.path not in {"", "/"}:
        raise ConfigError(f"ZEPP API host must not contain a path: {host!r}")
    if not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise ConfigError(
            f"Refusing to send a Zepp token to an unapproved host: {hostname or host!r}"
        )
    return normalized


def _deduplicate(values) -> list[str]:
    result: list[str] = []
    for item in values:
        if item not in result:
            result.append(item)
    return result

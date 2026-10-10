"""Validate effective enterprise pip indexes before installing project dependencies.

This module uses only Python's standard library so it runs before creating .venv.
It never prints index URLs, which may embed credentials.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit


class PipSourceError(ValueError):
    """An index is missing or does not meet the enterprise installation policy."""


def pip_config_get(name: str) -> str:
    """Read pip's active layered config, including PIP_CONFIG_FILE where applicable."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "config", "get", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def configured_value(
    name: str,
    environ: Mapping[str, str],
    get_config: Callable[[str], str],
) -> str:
    env_key = "PIP_" + name.upper().replace("-", "_")
    # Environment settings override pip.conf. An empty explicit value is not an
    # index, so we refuse later rather than accidentally accepting public PyPI.
    if env_key in environ:
        return environ[env_key].strip()
    return get_config(f"install.{name}") or get_config(f"global.{name}")


def _entries(raw: str) -> list[str]:
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise PipSourceError("pip 索引配置的引号不完整，请检查企业 pip 配置。") from exc


def _allowed_hostname(host: str, environ: Mapping[str, str]) -> bool:
    company_suffixes = ("hsbc", "hsbc.com", "hsbc.co.uk")
    if any(host == suffix or host.endswith("." + suffix) for suffix in company_suffixes):
        return True
    # Allow company-approved alternate DNS zones, using an explicit exact
    # hostname rather than a wildcard. Public PyPI is never allowed.
    extras = {
        entry.strip().lower()
        for entry in environ.get("APP_BUILDER_ALLOWED_PIP_HOSTS", "").replace(",", " ").split()
        if entry.strip()
    }
    return host in extras


def validate_sources(
    environ: Mapping[str, str] | None = None,
    get_config: Callable[[str], str] = pip_config_get,
) -> tuple[int, int]:
    """Validate primary and secondary pip indexes without changing their config.

    Returns counts (primary, extra) and deliberately not the potentially secret URLs.
    """
    env = os.environ if environ is None else environ
    if env.get("PIP_NO_INDEX", "").lower() in {"1", "true", "yes"}:
        raise PipSourceError("PIP_NO_INDEX 已开启；请通过公司批准的离线安装流程处理。")

    primary = _entries(configured_value("index-url", env, get_config))
    extra = _entries(configured_value("extra-index-url", env, get_config))
    if len(primary) != 1:
        raise PipSourceError("未检测到唯一的企业主 PyPI 索引，请检查 pip config 的 index-url。")

    for label, urls in (("主", primary), ("备用", extra)):
        for raw in urls:
            parsed = urlsplit(raw)
            host = (parsed.hostname or "").lower().rstrip(".")
            if not host or parsed.scheme.lower() != "https" or parsed.query or parsed.fragment:
                raise PipSourceError(f"{label} pip 索引 URL 必须使用合法的 HTTPS 地址（不要包含查询参数）。")
            if host in {"pypi.org", "files.pythonhosted.org", "pythonhosted.org"}:
                raise PipSourceError(f"{label} pip 索引指向公网 PyPI，安装已停止。")
            if not _allowed_hostname(host, env):
                raise PipSourceError(
                    f"{label} pip 索引主机不属于已识别的企业域名。"
                    "如为公司批准的其他 Nexus 主机，可配置 APP_BUILDER_ALLOWED_PIP_HOSTS。"
                )
    return len(primary), len(extra)


def main() -> int:
    try:
        primary, secondary = validate_sources()
    except PipSourceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"企业 pip 索引检查通过：主索引 {primary} 个，备用索引 {secondary} 个；保留现有 CA 和认证配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

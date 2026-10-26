"""Non-macOS secret storage: OS keyring when available, else environment/Docker secrets.

macOS keeps using the ``security`` CLI inside ``auth.py``. Everywhere else a secret
is looked up in this order and never written to task JSON, config or logs:

1. environment variable ``APPBUILDER_SECRET_<SERVICE>_<ACCOUNT>`` (dots/dashes -> ``_``);
2. file named by ``<same variable>_FILE`` (Docker/Kubernetes secrets);
3. the `keyring` package (Windows Credential Manager, Linux Secret Service), if installed.

Writing needs a working keyring; headless servers/Docker supply secrets by 1 or 2.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PREFIX = 'APPBUILDER_SECRET_'


def env_name(service, account):
    return PREFIX + re.sub(r'[^A-Za-z0-9]+', '_', f'{service}_{account}').strip('_').upper()


def _from_env(service, account):
    name = env_name(service, account)
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    path = os.environ.get(name + '_FILE')
    if path:
        try:
            text = Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            return None
        return text or None
    return None


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - optional dependency or no usable backend
        return None


def read(service, account):
    found = _from_env(service, account)
    if found:
        return found
    kr = _keyring()
    if kr:
        try:
            return (kr.get_password(service, account) or '').strip() or None
        except Exception:  # noqa: BLE001
            return None
    return None


def write(service, account, secret):
    """True when stored in the OS keyring; False when no keyring is available."""
    kr = _keyring()
    if not kr:
        return False
    try:
        kr.set_password(service, account, secret)
        return True
    except Exception:  # noqa: BLE001
        return False

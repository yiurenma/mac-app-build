"""Readable, secret-scrubbed operation trace (not a Playwright network HAR)."""
from __future__ import annotations
import os
import re
import shlex
import time
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

_verbose = ContextVar('app_builder_verbose', default=False)


def set_verbose(enabled: bool):
    return _verbose.set(bool(enabled))


def is_verbose() -> bool:
    return _verbose.get()


def safe_text(value, env=None):
    """Best-effort redaction for log strings; never print credential-bearing commands verbatim."""
    s = str(value)
    variables = dict(os.environ)
    if env:
        variables.update(env)
    # Redact known secrets, including custom *_TOKEN / *_PASSWORD vars.
    for name, secret in variables.items():
        if (re.search(r'(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|AUTH_KEY|PRIVATE_KEY)$', name, re.I)
                and isinstance(secret, str) and len(secret) >= 4):
            s = s.replace(secret, '[REDACTED]')
    s = re.sub(r'(https?://)[^\s/@]+@', r'\1', s)
    # URLs may contain auth tokens in their query string or fragments.
    s = re.sub(r'https?://[^\s<>\"\']+',
               lambda match: (lambda u: urlunsplit((u.scheme, u.netloc, u.path, '', '')))(urlsplit(match.group())),
               s)
    s = re.sub(r'(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\s,;]+',
               r'\1=[REDACTED]', s)
    s = re.sub(r'(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*[^\s,;&]+',
               r'\1=[REDACTED]', s)
    return s


def safe_command(args, env=None):
    return ' '.join(shlex.quote(safe_text(x, env=env)) for x in args)


def debug(message):
    if is_verbose():
        print(time.strftime('%Y-%m-%d %H:%M:%S'), '[TRACE]', safe_text(message), flush=True)


def action(kind, message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), f'[{kind}]', safe_text(message), flush=True)


def operation(kind, message):
    """Show fine-grained actions only with --verbose; still always persist via --watch."""
    if is_verbose():
        action(kind, message)

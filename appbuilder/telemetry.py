"""Readable, secret-scrubbed operation trace (not a Playwright network HAR)."""
from __future__ import annotations
import os
import re
import shlex
import time
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

_verbose = ContextVar('app_builder_verbose', default=False)
_live_sink = ContextVar('app_builder_live_sink', default=None)

def set_live_sink(callback):
    return _live_sink.set(callback)


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
    s = re.sub(r'(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]*',
               r'\1=[REDACTED]', s)
    s = re.sub(r'(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*[^\s,;&]+',
               r'\1=[REDACTED]', s)
    # Structured errors and Playwright action logs can quote credential values.
    s = re.sub(r"(?i)([\"']?)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key)\1\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&]+)",
               r'\2=[REDACTED]', s)
    s = re.sub(r"(?i)\bfill\(\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*\)",
               'fill([REDACTED])', s)
    return s


def safe_command(args, env=None):
    return ' '.join(shlex.quote(safe_text(x, env=env)) for x in args)


def debug(message):
    if is_verbose():
        print(time.strftime('%Y-%m-%d %H:%M:%S'), '[TRACE]', safe_text(message), flush=True)


def action(kind, message):
    clean = safe_text(message)
    print(time.strftime('%Y-%m-%d %H:%M:%S'), f'[{kind}]', clean, flush=True)
    sink = _live_sink.get()
    if sink is not None:
        sink(f'[{kind}] {clean}')


def operation(kind, message):
    """Show fine-grained actions only with --verbose; still always persist via --watch."""
    if is_verbose():
        action(kind, message)

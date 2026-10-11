"""Bounded, private diagnostics for one TestFairy attempt; never browser dumps."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .telemetry import safe_text

_SENSITIVE = re.compile(r'password|passwd|secret|token|cookie|authorization|credential|session|api.?key', re.I)
_RAW = re.compile(r'(^|_)(html|dom|screenshot|body|headers|storage|network)(_|$)', re.I)
_BUILD_PATH = re.compile(r'(?:/job/[^/]+)+/[1-9][0-9]*/?\Z')
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)


class AttemptTrace:
    def __init__(self, data, secrets=()):
        self.attempt_id = uuid.uuid4().hex
        self.directory = Path(data) / 'testfairy-attempts' / self.attempt_id
        self.log_path = self.directory / 'trace.jsonl'
        self.evidence_path = self.directory / 'evidence.json'
        self._started = time.monotonic()
        self._secrets = sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True)
        self._first_error = None
        self._stages = {}
        self._logging_error = None
        self._lock = threading.RLock()
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.parent.chmod(0o700)
            self.directory.mkdir(mode=0o700)
            self._write(self.log_path, '', append=False)
            self._write(self.evidence_path, '{}\n', append=False)
        except Exception as exc:
            self._log_failure(exc)

    def _text(self, value, *, allow_build_path=False):
        try:
            text = str(value)
            for secret in self._secrets:
                text = text.replace(secret, '[REDACTED]')
            # Never retain supplied markup, including accidentally passed DOM evidence.
            if re.search(r'<\s*/?\s*[a-zA-Z][^>]*>', text):
                return '[OMITTED: markup]'
            def url(match):
                try:
                    parsed = urlsplit(match.group())
                    host = parsed.hostname or ''
                    if ':' in host:
                        host = '[' + host + ']'
                    if parsed.port:
                        host += ':' + str(parsed.port)
                    if allow_build_path and parsed.scheme == 'https' and _BUILD_PATH.fullmatch(parsed.path):
                        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
                    omitted = bool(parsed.path or parsed.query or parsed.fragment or parsed.username)
                    return urlunsplit((parsed.scheme, host, '', '', '')) + (' [path omitted]' if omitted else '')
                except ValueError:
                    return '[REDACTED URL]'
            text = safe_text(_URL.sub(url, text))
            # Redact again in case URL decoding/normalization revealed a known value.
            for secret in self._secrets:
                text = text.replace(secret, '[REDACTED]')
            return text[:1000].encode('utf-8', errors='replace').decode('utf-8')
        except Exception:
            return '[unavailable safe text]'

    def _sanitize(self, value, depth=0):
        if depth > 4:
            return '[truncated]'
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value if not isinstance(value, float) or math.isfinite(value) else None
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:30]:
                if not isinstance(key, str):
                    continue
                name = self._text(key)[:100]
                if _SENSITIVE.search(key):
                    result[name] = '[REDACTED]'
                elif _RAW.search(key):
                    result[name] = '[OMITTED]'
                elif key == 'build_url' and isinstance(item, str) and _URL.fullmatch(item):
                    result[name] = self._text(item, allow_build_path=True)
                else:
                    result[name] = self._sanitize(item, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item, depth + 1) for item in value[:30]]
        return '[OMITTED: unsupported value]'

    @staticmethod
    def _write(path, text, *, append):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)

    def _log_failure(self, exc):
        if self._logging_error is None:
            self._logging_error = {'type': type(exc).__name__, 'message': self._text(exc)}

    def _encoded(self, record):
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 16384:
            encoded = json.dumps({'truncated': True, 'stage': record.get('stage'),
                                  'event': record.get('event'), 'first_error': self._first_error}, ensure_ascii=False)
        return encoded + '\n'

    def event(self, stage, event, **fields):
        with self._lock:
            record = self._sanitize(fields)
            if event == 'start':
                self._stages[str(stage)] = time.monotonic()
            elif event == 'end' and str(stage) in self._stages:
                record['duration_seconds'] = round(time.monotonic() - self._stages.pop(str(stage)), 3)
            record.update(attempt_id=self.attempt_id, stage=self._text(stage), event=self._text(event),
                          elapsed_seconds=round(time.monotonic() - self._started, 3))
            try:
                self._write(self.log_path, self._encoded(record), append=True)
            except Exception as exc:
                self._log_failure(exc)

    def failure(self, stage, exc, evidence=None):
        with self._lock:
            error = {'type': type(exc).__name__, 'phase': self._text(stage), 'stage': self._text(stage), 'message': self._text(exc)}
            first = self._first_error is None
            if first:
                self._first_error = error
            self.event(stage, 'failure', error=error, first_error=self._first_error)
            if first:
                record = {'attempt_id': self.attempt_id, 'first_error': self._first_error,
                          'evidence': self._sanitize(evidence)}
                try:
                    self._write(self.evidence_path, self._encoded(record), append=False)
                except Exception as write_error:
                    self._log_failure(write_error)

    @property
    def summary(self):
        result = {'attempt_id': self.attempt_id, 'log_path': str(self.log_path),
                  'evidence_path': str(self.evidence_path), 'first_error': dict(self._first_error) if self._first_error else None}
        if self._logging_error:
            result['logging_error'] = dict(self._logging_error)
        return result

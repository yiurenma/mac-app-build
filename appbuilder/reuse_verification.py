"""Validate script-owned history only; never query old Jenkins logs or services."""
import re
from urllib.parse import urlsplit
from .reuse import _provenance, valid_artifacts
from .versions import ARTIFACTS


def https_url(value):
    if not isinstance(value, str):
        return False
    u = urlsplit(value)
    return u.scheme == 'https' and bool(u.hostname) and not u.username and not u.password


class LiveVerifier:
    # Kept as the runner's injection boundary; validation is entirely local.
    def __init__(self, config, data, home, params, env):
        pass

    def _common(self, record):
        return (record.get('result') == 'SUCCESS' and bool(record.get('source_task_id'))
                and bool(re.fullmatch(r'[a-f0-9]{40}', record.get('source_sha', '')))
                and isinstance(record.get('parameters'), dict)
                and https_url(record.get('repository')) and bool(record.get('branch'))
                and _provenance(record, ''))

    def plugin(self, record):
        downloads = record.get('downloads', {})
        complete = self._common(record) and valid_artifacts(record)
        complete = complete and isinstance(record.get('formal_number'), int) and record['formal_number'] > 0
        complete = complete and set(downloads) == set(ARTIFACTS)
        if complete:
            complete = all(isinstance(downloads[k], dict) and https_url(downloads[k].get('url'))
                           and downloads[k].get('version') == record['version']
                           and downloads[k].get('sha256') == record['hashes'][k] for k in ARTIFACTS)
        return {'history_complete': bool(complete)}

    def app(self, record):
        app = record.get('app', {})
        complete = self._common(record) and isinstance(app, dict)
        expected = dict(build_url=record.get('build_url'), number=record.get('build_number'),
                        sha=record.get('build_sha'), folder=record.get('folder'),
                        app_version=record.get('app_version'), app_build=record.get('app_build'),
                        share_url=record.get('share_url'))
        complete = complete and all(v and app.get(k) == v for k, v in expected.items())
        complete = complete and bool(app.get('path')) and https_url(record.get('share_url'))
        return {'history_complete': bool(complete)}

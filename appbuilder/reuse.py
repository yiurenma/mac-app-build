"""Match complete script-owned historical results without remote history reads."""
from copy import deepcopy
import re

from .versions import ARTIFACTS


def valid_artifacts(record):
    hashes = record.get('hashes', {})
    return (isinstance(hashes, dict) and isinstance(record.get('version'), str) and bool(record['version'])
            and set(hashes) == set(ARTIFACTS)
            and all(isinstance(v, str) and re.fullmatch(r'[a-fA-F0-9]{64}', v)
                    for v in hashes.values()))


def plugin_candidate(request, record, verify_current):
    """Return the original artifact provenance, never a new Job + old number."""
    if not valid_artifacts(record):
        return None
    required = ('repository', 'source_sha', 'parameters')
    if any(key not in request or record.get(key) != request[key] for key in required):
        return None
    version = request.get('version_input', '')
    effective = record.get('effective_version', '')
    if re.fullmatch(r'\d+\.\d+', version):
        if not re.fullmatch(re.escape(version) + r'\.\d+', effective):
            return None
    elif not re.fullmatch(r'\d+\.\d+\.\d+', version) or effective != version:
        return None
    if not _provenance(record, 'plugin'):
        return None
    try:
        # Validate the persisted result; old Jenkins logs are not required.
        proof = verify_current(deepcopy(record))
    except Exception:
        return None
    if not isinstance(proof, dict) or any(proof.get(k) is not True for k in (
            'history_complete',)):
        return None
    selected = deepcopy(record)
    digest = proof.get('effective_parameters_digest')
    if isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest):
        selected['effective_parameters_digest'] = digest
    return selected


def _provenance(record, kind):
    from urllib.parse import urlsplit
    job, build = record.get('job_url', ''), record.get('build_number')
    if not isinstance(job, str):
        return False
    u = urlsplit(job)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment
            or not job.endswith('/') or not isinstance(build, int) or isinstance(build, bool) or build < 1
            or record.get('build_url') != job + str(build) + '/'):
        return False
    if not re.fullmatch(r'[a-f0-9]{40}', record.get('build_sha', '')):
        return False
    return True


def app_candidate(request, record, verify_current):
    """Match independently built GMX output and retain its entire original origin."""
    keys = ('repository', 'source_sha', 'plugin_version', 'plugin_hashes', 'parameters', 'folder')
    if any(key not in request or record.get(key) != request[key] for key in keys):
        return None
    if not valid_artifacts({'version': record['plugin_version'], 'hashes': record['plugin_hashes']}):
        return None
    if not _provenance(record, 'gmx') or not all(record.get(k) for k in
            ('share_url', 'app_version', 'app_build', 'folder')):
        return None
    try:
        proof = verify_current(deepcopy(record))
    except Exception:
        return None
    if not isinstance(proof, dict) or any(proof.get(k) is not True for k in (
            'history_complete',)):
        return None
    selected = deepcopy(record)
    digest = proof.get('effective_parameters_digest')
    if isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest):
        selected['effective_parameters_digest'] = digest
    return selected

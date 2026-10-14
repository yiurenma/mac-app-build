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


def jenkins_app_evidence(record):
    """Require the complete saved HTML-console chain, bound to this GMX result."""
    from urllib.parse import urlsplit
    from .jenkins_links import source_build, validate_join_url
    app = record.get('app')
    if not isinstance(app, dict) or app.get('lookup_method') != 'jenkins-console-html':
        return False
    try:
        source = source_build({'gmx_job': app.get('source_job_url')},
                              {'gmx_build_number': app.get('source_build_number')})
        if (source['job_url'] != record.get('job_url') or source['number'] != record.get('build_number')
                or source['build_url'] != record.get('build_url')
                or source['build_url'] != app.get('source_build_url')):
            return False
        expected = {'build_url': record.get('build_url'), 'number': record.get('build_number'),
                    'sha': record.get('build_sha'), 'folder': record.get('folder'),
                    'share_url': record.get('share_url')}
        if any(not value or app.get(key) != value for key, value in expected.items()):
            return False
        downstream = source_build({'gmx_job': app.get('downstream_job_url')},
                                  {'gmx_build_number': app.get('downstream_build_number')})
        origin = urlsplit(source['job_url'])
        target = urlsplit(downstream['job_url'])
        if (target.scheme, target.hostname, target.port or 443) != (origin.scheme, origin.hostname, origin.port or 443):
            return False
        if not target.path.endswith('/job/upload-to-testfairy/job/master/'):
            return False
        return (downstream['build_url'] == app.get('downstream_build_url')
                and app.get('downstream_result') == 'SUCCESS'
                and validate_join_url(app['share_url']) == app['share_url'])
    except (RuntimeError, ValueError, TypeError, KeyError):
        return False


def app_candidate(request, record, verify_current):
    """Match independently built GMX output and retain its entire original origin."""
    keys = ('repository', 'source_sha', 'plugin_version', 'plugin_hashes', 'parameters', 'folder')
    if any(key not in request or record.get(key) != request[key] for key in keys):
        return None
    if not valid_artifacts({'version': record['plugin_version'], 'hashes': record['plugin_hashes']}):
        return None
    if not _provenance(record, 'gmx') or not record.get('folder') or not record.get('share_url'):
        return None
    app = record.get('app', {})
    if isinstance(app, dict) and app.get('lookup_method') == 'jenkins-console-html':
        if not jenkins_app_evidence(record):
            return None
    elif not all(record.get(k) for k in ('app_version', 'app_build')):
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

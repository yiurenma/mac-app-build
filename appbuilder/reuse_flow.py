"""Persistent reuse orchestration. Reuse only complete script-owned historical results."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from . import store
from .reuse import plugin_candidate, app_candidate
from .jenkins import NeedsAttention


def effective_params(params, runtime):
    result = dict(params)
    for site, key in (('plugin', 'plugin_reuse'), ('gmx', 'app_reuse')):
        origin = runtime.get(key)
        if isinstance(origin, dict):
            result[site + '_job'] = origin['job_url']
            result[site + '_branch'] = origin['branch']
            if site == 'plugin':
                result['plugin_version'] = origin['effective_version']
    return result


def plugin_parameters(params):
    # No Plugin editable build parameters exist today. Preserve explicit future
    # inputs and the Job family in the historical result.
    return {'requested': deepcopy(params.get('plugin_parameters', {})),
            'job_parent': params['plugin_job'].rstrip('/').rsplit('/', 1)[0] + '/'}


def app_parameters(params):
    return {'gmx': deepcopy(params.get('gmx_parameters', {})), 'country': params.get('country'),
            'project': params.get('project'),
            'job_parent': params['gmx_job'].rstrip('/').rsplit('/', 1)[0] + '/',
            'testfairy_login': params.get('testfairy', {}).get('login_url'),
            'testfairy_folder': params.get('testfairy', {}).get('folder_path')}


def app_folder(params):
    return params.get('gmx_parameters', {}).get('CUSTOM_FOLDER') or f"0_A_{params['country']}_{params['project']}"


def plugin_record(task_id, params, rt):
    number = rt.get('plugin_artifact_number')
    source = rt.get('plugin_source_sha') or rt.get('plugin_base_sha')
    if not number or not source or not rt.get('plugin_sha') or not rt.get('artifact_version'):
        return None
    p = effective_params(params, rt)
    return dict(schema=1, result=rt.get('plugin_artifact_result'), dispatch_identity=deepcopy(rt.get('plugin_formal_identity', {})), source_task_id=task_id, repository=params['plugin_url'], source_sha=source,
                parameters=plugin_parameters(params), effective_version=p['plugin_version'],
                branch=p['plugin_branch'], job_url=p['plugin_job'], build_number=number,
                formal_number=rt.get('plugin_formal_number'),
                queue_id=rt.get('plugin_formal_dispatch', {}).get('queue_id'), build_url=p['plugin_job']+str(number)+'/',
                build_sha=rt['plugin_sha'], version=rt['artifact_version'],
                hashes=deepcopy(rt.get('plugin_artifact_hashes', {})),
                downloads=deepcopy(rt.get('plugin_artifact_downloads', {})))


def app_record(task_id, params, rt):
    app = rt.get('testfairy_app')
    if not isinstance(app, dict) or not rt.get('gmx_base_sha') or not rt.get('gmx_sha'):
        return None
    p = effective_params(params, rt)
    number = rt.get('gmx_build_number') or rt.get('gmx_formal_number')
    if not number or any(app.get(k) != v for k, v in dict(build_url=p['gmx_job']+str(number)+'/',
                   number=number, sha=rt['gmx_sha'], folder=app_folder(params)).items()):
        return None
    return dict(schema=1, result=rt.get('gmx_formal_result'), dispatch_identity=deepcopy(rt.get('gmx_formal_identity', {})), source_task_id=task_id, repository=params['gmx_url'], source_sha=rt['gmx_base_sha'],
                parameters=app_parameters(params), branch=p['gmx_branch'], job_url=p['gmx_job'],
                build_number=number, queue_id=rt.get('gmx_formal_dispatch', {}).get('queue_id'), build_url=app['build_url'], build_sha=rt['gmx_sha'],
                plugin_version=rt.get('artifact_version'), plugin_hashes=deepcopy(rt.get('plugin_artifact_hashes', {})),
                folder=app['folder'], share_url=app.get('share_url'), app_version=app.get('app_version'),
                app_build=app.get('app_build'), app=deepcopy(app))


def remember(task_id, params, kind):
    rt = store.read_state(task_id)['runtime']
    # Keep the original source task across chains of reused results.
    original = rt.get('plugin_reuse' if kind == 'plugin' else 'app_reuse')
    try:
        record = original or (plugin_record if kind == 'plugin' else app_record)(task_id, params, rt)
    except (KeyError, TypeError, ValueError):
        record = None
    if record:
        records = {**rt.get('reuse_records', {}), kind: deepcopy(record)}
        store.patch_runtime(task_id, reuse_records=records)
    return record


def candidates(task_id, kind):
    seen = set()
    for row in store.task_list():
        if row['id'] == task_id:
            continue
        try:
            params = json.loads(row['params']); rt = store.read_state(row['id'])['runtime']
            record = rt.get('reuse_records', {}).get(kind)
            if not record:
                record = (plugin_record if kind == 'plugin' else app_record)(row['id'], params, rt)
            if not record or record.get('schema') != 1:
                continue
            key = (record['job_url'], record['build_number'], record['build_sha'])
            if key in seen:
                continue
            seen.add(key)
            yield deepcopy(record)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Incomplete old records cannot establish reuse.


def select(task_id, kind, request, verifier):
    matcher = plugin_candidate if kind == 'plugin' else app_candidate
    checks = []
    def persist():
        rt = store.read_state(task_id)['runtime']
        store.patch_runtime(task_id, reuse_checks={**rt.get('reuse_checks', {}), kind: checks[-20:]})
    for record in candidates(task_id, kind):
        called = []
        def verify(value):
            called.append(True)
            return getattr(verifier, kind)(value)
        try:
            matched = matcher(request, record, verify)
        except (KeyError, ValueError, TypeError):
            matched = None
        checks.append({'source_task_id': record.get('source_task_id'),
                       'decision': 'VERIFIED' if matched else 'EVIDENCE_INSUFFICIENT' if called else 'INPUT_MISMATCH'})
        if matched:
            persist()
            return matched
    persist()
    return None


def _accept(task_id, kind, record):
    values = {'reuse_checked_at': datetime.now(timezone.utc).isoformat()}
    if kind == 'plugin':
        values.update(plugin_reuse=record, plugin_source_sha=record['source_sha'],
            plugin_base_sha=record['source_sha'], plugin_sha=record['build_sha'],
            artifact_version=record['version'], plugin_artifact_hashes=record['hashes'],
            plugin_artifact_number=record['build_number'], plugin_formal_number=record['formal_number'])
        if record['build_number'] != record['formal_number']:
            values['plugin_callback_number'] = record['build_number']
    else:
        values.update(app_reuse=record, gmx_sha=record['build_sha'], gmx_base_sha=record['source_sha'],
            gmx_build_number=record['build_number'], gmx_formal_number=record['build_number'],
            testfairy_app=record['app'], upload_validation='REUSED_VERIFIED_APP')
    store.patch_runtime(task_id, **deepcopy(values))
    store.complete(task_id, 'PLUGIN_REUSE' if kind == 'plugin' else 'GMX_REUSE')


def try_plugin(task_id, params, env, verifier):
    from .plugin_git import remote_tip
    state = store.read_state(task_id); rt = state['runtime']
    existing = rt.get('plugin_reuse')
    if not existing and (rt.get('plugin_preparation') or any(s in state['completed'] for s in
                              ('PLUGIN_PREPARE','PLUGIN_CHECKOUT','PLUGIN_JENKINS'))):
        return False
    source = existing['source_sha'] if existing else remote_tip(params['plugin_url'], params['plugin_source'], env)
    request = dict(repository=params['plugin_url'], source_sha=source,
                   parameters=plugin_parameters(params), version_input=params['plugin_version_input'])
    record = plugin_candidate(request, existing, verifier.plugin) if existing else select(task_id,'plugin',request,verifier)
    if existing and not record:
        raise NeedsAttention('已采用的 Plugin 复用来源当前无法重新核验；保留快照，不切换构建来源')
    if record:
        _accept(task_id,'plugin',record)
        return True
    return False


def try_app(task_id, params, verifier):
    state = store.read_state(task_id); rt = state['runtime']; existing = rt.get('app_reuse')
    if not existing and any(s in state['completed'] for s in ('GMX_EDIT','GMX_PUSH','GMX_JENKINS')):
        return False
    request = dict(repository=params['gmx_url'], source_sha=rt['gmx_base_sha'],
                   plugin_version=rt['artifact_version'], plugin_hashes=rt['plugin_artifact_hashes'],
                   parameters=app_parameters(params), folder=app_folder(params))
    record = app_candidate(request,existing,verifier.app) if existing else select(task_id,'app',request,verifier)
    if existing and not record:
        raise NeedsAttention('已采用的 APP 复用来源当前无法重新核验；保留旧构建来源')
    if record:
        _accept(task_id,'app',record)
        return True
    return False

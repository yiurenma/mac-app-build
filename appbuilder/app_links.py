"""One read-only Jenkins APP-link lookup shared by builds, UI tests and CLI."""
from datetime import datetime, timezone
import json
import re
from pathlib import Path

from .jenkins import JenkinsBrowser, NeedsAttention
from .jenkins_links import source_build, lookup_app_link, validate_join_url


def token_lookup(config, source, *, emit=None, credentials=None, fetch=None):
    """Try the Jenkins API token first. Returns None (caller uses the browser) when no
    token is saved or Jenkins refuses it; the reason is reported, never the token."""
    from .telemetry import action
    report = emit if emit is not None else (lambda message: action('APP-LINK', message))
    if fetch is None:
        if credentials is None:
            from .auth import read_jenkins_api_token, read_jenkins_username
            token, user = read_jenkins_api_token(), read_jenkins_username()
            if not token:
                return None
            if not user:
                report('[APP-LINK] 已保存 Jenkins API Token，但没有已保存的 Jenkins 用户名；改用浏览器')
                return None
            credentials = (user, token)
        from .jenkins_api_links import http_fetch
        jenkins = (config or {}).get('jenkins', {})
        fetch = http_fetch(*credentials, timeout=int(jenkins.get('api_timeout_seconds', 30)),
                           ca_bundle=jenkins.get('ca_bundle'))
    from .jenkins_api_links import api_lookup
    try:
        report('[APP-LINK] 使用 Jenkins API Token 读取日志（不打开浏览器）')
        result = api_lookup(source['job_url'], source['number'], fetch, emit=report)
    except NeedsAttention as exc:
        reason = str(exc) if type(exc) is NeedsAttention else '未知原因'
        report(f'[APP-LINK] Token 方式未取到链接，改用浏览器：{reason}')
        return None
    except Exception as exc:
        report(f'[APP-LINK] Token 方式出错（{type(exc).__name__}），改用浏览器')
        return None
    result['transport'] = 'jenkins-api-token'
    return result


def lookup_task_app(config, data, workdir, params, runtime, *, visible=None, emit=None):
    """Use the saved GMX source pair; never create or select a newer build."""
    source = source_build(params, runtime)
    show = bool(params.get('visible', True) if visible is None else visible)
    # Use the task's saved Jenkins settings, just as the formal build does.
    from .runner import effective_task_config
    config = effective_task_config(config, params)
    result = token_lookup(config, source, emit=emit)
    if result is None:
        with JenkinsBrowser(config, Path(data), Path(workdir), 'gmx', visible=show,
                            verbose=bool(params.get('verbose')), manual_login=show) as browser:
            result = lookup_app_link(browser, source['job_url'], source['number'], emit=emit)
    # Preserve the old delivery object's identity fields, without inventing an
    # App version/build that the Jenkins URL alone does not establish.
    reused = runtime.get('app_reuse')
    gmx = params.get('gmx_parameters', {})
    if isinstance(reused, dict):
        # Never label an old source with the current task's SHA or folder.
        folder = reused.get('folder')
        sha = reused.get('build_sha')
    else:
        reused = {}
        folder = gmx.get('CUSTOM_FOLDER')
        if not folder and params.get('country') and params.get('project'):
            folder = f"0_A_{params['country']}_{params['project']}"
        sha = runtime.get('gmx_sha')
    app = {**result, 'share_url': validate_join_url(result['share_url']),
            'source_job_url': source['job_url'], 'source_build_number': source['number'],
            'source_build_url': source['build_url'], 'build_url': source['build_url'],
            'number': source['number'], 'sha': sha,
            'folder': folder, 'app_version': None, 'app_build': None,
            'lookup_method': 'jenkins-console-html', 'downstream_result': 'SUCCESS',
            'matched_at': datetime.now(timezone.utc).isoformat()}
    previous = runtime.get('testfairy_app') or reused.get('app')
    if (isinstance(previous, dict) and isinstance(app['sha'], str)
            and re.fullmatch(r'[a-fA-F0-9]{40}', app['sha']) and app['folder']
            and all(previous.get(key) == app[key] for key in ('build_url', 'number', 'sha', 'folder'))):
        for key in ('app_version', 'app_build', 'path'):
            if previous.get(key):
                app[key] = previous[key]
    return app


def lookup_history(task_id, *, config, data, visible=True, emit=None):
    """Extract and save a historical task's link without restarting its steps."""
    from . import store
    row = store.task(task_id)
    if row['status'] in ('RUNNING', 'QUEUED'):
        raise NeedsAttention('历史取链不能改写仍在运行或排队的任务；请等待构建停止后重试')
    params = json.loads(row['params'])
    runtime = store.read_state(task_id).get('runtime', {})
    app = lookup_task_app(config, data, store.task_folder(task_id) / 'app-link-lookup',
                          params, runtime, visible=visible, emit=emit)
    store.patch_runtime(task_id, testfairy_app=app)
    return app

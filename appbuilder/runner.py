from __future__ import annotations
import json
from copy import deepcopy
import fcntl
from contextlib import contextmanager
import os
import re
import subprocess
import signal
import time
import threading
from pathlib import Path
from .config import DATA, load_config, token_or_error
from .store import task, read_state, patch_runtime, complete, update_task, task_folder
from .gitops import credentials, clone_branch, git, changed_files, commit_and_push, verify_pushed, recover_pushed_commit
from .verify import plugin_commit, gmx_commit
from .edits import edit_plugin_versions, update_mobile_ci, validate_lock, ensure_lock_other_entries_unchanged, XCCONFIGS
from .jenkins import JenkinsBrowser, ensure_bootstrap, formal_build, plugin_artifact_version, NeedsAttention
from .telemetry import set_verbose, debug, action, operation, safe_text, is_verbose


def log(message):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)


def step(task_id, name, fn):
    if name in read_state(task_id)['completed']:
        log(f'[SKIP] {name}: 已完成')
        return
    update_task(task_id, stage=name)
    log(f'[STEP] {name}')
    fn()
    complete(task_id, name)
    log(f'[DONE] {name}')


def checkout(params, name, root: Path, source, branch, env):
    if root.exists():
        if not (root / '.git').exists():
            raise NeedsAttention(f'{name} 仓库目录部分创建，需人工检查: {root}')
        if git(['branch', '--show-current'], cwd=root) != branch:
            raise NeedsAttention(f'{name} 现有工作目录分支与目标分支不同: {root}')
        remote = git(['remote', 'get-url', 'origin'], cwd=root).removesuffix('.git')
        if remote.rstrip('/') != params[name + '_url'].removesuffix('.git').rstrip('/'):
            raise NeedsAttention(f'{name} 工作目录 origin 不符合输入 URL')
        return git(['rev-parse', 'HEAD'], cwd=root)
    return clone_branch(params[name + '_url'], source, branch, root, env)


def run_bootstrap(root: Path, logfile: Path, timeout_minutes=60):
    script = root / 'bootstrap.sh'
    if not script.is_file():
        raise FileNotFoundError(f'GMX 根目录缺少 {script}')
    if not os.access(script, os.X_OK):
        raise RuntimeError('GMX bootstrap.sh 不具备执行权限，请检查 Git 中的可执行位')
    action('SHELL', f'$ ./bootstrap.sh  (cwd={root})')
    action('SHELL', f'子进程输出保存至 {logfile}；--verbose 时同时流式显示（已尝试隐藏凭据）')
    with logfile.open('a', encoding='utf-8') as f:
        p = subprocess.Popen(['./bootstrap.sh'], cwd=root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, errors='replace', bufsize=1,
                             start_new_session=True, env=os.environ.copy())
        show_child_output = is_verbose()  # ContextVar is not inherited by Python threads.
        def consume_output():
            # Read in another thread so a long silent subprocess can still time out.
            try:
                for line in p.stdout:
                    clean = safe_text(line.rstrip('\r\n'))
                    f.write(clean + '\n')
                    f.flush()
                    if show_child_output:
                        action('BOOTSTRAP', clean[:2000])
            finally:
                p.stdout.close()
        reader = threading.Thread(target=consume_output, name='bootstrap-output', daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            deadline = started + 60 * timeout_minutes
            while p.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('./bootstrap.sh', 60 * timeout_minutes)
                try:
                    p.wait(timeout=min(15, remaining))
                except subprocess.TimeoutExpired:
                    action('WAIT', f'bootstrap.sh 运行 {int(time.monotonic()-started)}s; 当前日志 {logfile.stat().st_size} bytes')
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            raise TimeoutError('bootstrap.sh 执行超时；请查看 bootstrap.log')
        finally:
            reader.join(timeout=10)
    action('SHELL', f'./bootstrap.sh exit={p.returncode} elapsed={time.monotonic()-started:.1f}s')
    if p.returncode != 0:
        raise RuntimeError(f'bootstrap.sh 失败 (exit={p.returncode})，请查看 {logfile}')


@contextmanager
def worker_lease(task_id):
    """Prevent two local workers from operating the same task simultaneously.

    macOS flock is held for the entire run, independent of SQLite status/PID.
    """
    lockfile = task_folder(task_id) / 'worker.lock'
    with lockfile.open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NeedsAttention('此任务已有 Worker 正在执行；禁止并发恢复同一任务') from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run(task_id):
    with worker_lease(task_id):
        return _run_unlocked(task_id)


def effective_task_config(config, params):
    """Resolve task-scoped preferences without mutating other tasks or config.yaml."""
    conf = deepcopy(config)
    for k, v in params.get('jenkins_settings', {}).items():
        if k == 'gmx_fields':
            conf['jenkins']['gmx_fields'] = {**conf['jenkins'].get('gmx_fields', {}), **v}
        else:
            conf['jenkins'][k] = v
    conf['build'].update(params.get('build_settings', {}))
    return conf


def _run_unlocked(task_id):
    row = task(task_id)
    if row['status'] == 'SUCCESS':
        log('任务已经成功完成，不再执行')
        return
    params = json.loads(row['params'])
    set_verbose(params.get('verbose', False))
    conf = effective_task_config(load_config(), params)
    token_or_error(conf)
    home = task_folder(task_id)
    plugin = home / 'plugin'
    gmx = home / 'gmx'
    plugin_branch = params['plugin_branch']
    gmx_branch = params['gmx_branch']
    version = params['plugin_version']
    max_lines = int(conf.get('build', {}).get('max_changed_lines', 100))
    update_task(task_id, status='RUNNING', pid=os.getpid(), note='')
    log(f'启动任务 {task_id}; plugin={plugin_branch}; gmx={gmx_branch}')
    log(f'[MODE] watch=terminal-observer visible={bool(params.get("visible"))} verbose={bool(params.get("verbose"))}')
    try:
        with credentials(conf.get('github', {}).get('token_env', 'GITHUB_TOKEN')) as env:
            def plugin_checkout():
                base = checkout(params, 'plugin', plugin, params['plugin_source'], plugin_branch, env)
                patch_runtime(task_id, plugin_base_sha=base)
            step(task_id, 'PLUGIN_CHECKOUT', plugin_checkout)

            def plugin_edit():
                changes = edit_plugin_versions(plugin, version)
                expected = set(XCCONFIGS)
                extras = set(changed_files(plugin)) - expected
                if extras:
                    raise NeedsAttention(f'Plugin 意外变更: {sorted(extras)}')
                patch_runtime(task_id, plugin_edits=changes)
            step(task_id, 'PLUGIN_EDIT', plugin_edit)

            def plugin_push():
                rt = read_state(task_id)['runtime']
                if 'plugin_sha' in rt:
                    verify_pushed(plugin, plugin_branch, rt['plugin_sha'], env)
                    return
                message = f'build: set plugin version {version}'
                validator = lambda: plugin_commit(plugin, rt['plugin_base_sha'], version)
                sha = recover_pushed_commit(plugin, plugin_branch, set(XCCONFIGS), message,
                                            rt['plugin_base_sha'], env, validate_before_push=validator)
                if not sha:
                    sha = commit_and_push(plugin, set(XCCONFIGS), message,
                                          plugin_branch, env, max_lines=max_lines,
                                          validate_before_push=validator)
                patch_runtime(task_id, plugin_sha=sha)
            step(task_id, 'PLUGIN_PUSH', plugin_push)

            def plugin_build():
                rt = read_state(task_id)['runtime']
                verify_pushed(plugin, plugin_branch, rt['plugin_sha'], env)
                with JenkinsBrowser(conf, DATA, home, 'plugin', visible=params.get('visible', False), verbose=params.get('verbose', False)) as browser:
                    ensure_bootstrap(browser, params['plugin_job'], task_id, 'plugin')
                    num, _ = formal_build(browser, params['plugin_job'], task_id, 'plugin')
                    v, artifact_build = plugin_artifact_version(browser, params['plugin_job'], num, task_id)
                    patch_runtime(task_id, artifact_version=v, plugin_artifact_number=artifact_build)
                    log(f'[ARTIFACT] 完整依赖版本: {v}, Build #{artifact_build}')
            step(task_id, 'PLUGIN_JENKINS', plugin_build)

            artifact = read_state(task_id)['runtime'].get('artifact_version')
            if not artifact:
                raise NeedsAttention('Plugin Artifact version 尚未记录')

            def gmx_checkout():
                base = checkout(params, 'gmx', gmx, params['gmx_source'], gmx_branch, env)
                patch_runtime(task_id, gmx_base_sha=base)
            step(task_id, 'GMX_CHECKOUT', gmx_checkout)

            def gmx_edit():
                old_versions = update_mobile_ci(gmx / 'mobile-ci.json', artifact)
                if set(changed_files(gmx)) - {'mobile-ci.json'}:
                    raise NeedsAttention('修改 GMX JSON 后出现非预期变更')
                patch_runtime(task_id, gmx_previous_versions=old_versions)
            step(task_id, 'GMX_EDIT', gmx_edit)

            def gmx_resolve():
                lock = gmx / 'mobile-ci.lock'
                before = lock.read_text(encoding='utf-8')
                run_bootstrap(gmx, home / 'bootstrap.log')
                digests = validate_lock(lock, artifact)
                ensure_lock_other_entries_unchanged(before, lock.read_text(encoding='utf-8'))
                extras = set(changed_files(gmx)) - {'mobile-ci.json', 'mobile-ci.lock'}
                if extras:
                    raise NeedsAttention(f'bootstrap.sh 生成非预期变更（已阻止提交）：{sorted(extras)}')
                patch_runtime(task_id, lock_hashes=digests)
                log(f'[LOCK] 版本与 SHA256 验证成功: {artifact}')
            step(task_id, 'GMX_BOOTSTRAP', gmx_resolve)

            def gmx_push():
                rt = read_state(task_id)['runtime']
                if 'gmx_sha' in rt:
                    verify_pushed(gmx, gmx_branch, rt['gmx_sha'], env)
                    return
                message = f'build: update PayByBank plugins to {artifact}'
                expected = {'mobile-ci.json', 'mobile-ci.lock'}
                validator = lambda: gmx_commit(gmx, rt['gmx_base_sha'], artifact)
                sha = recover_pushed_commit(gmx, gmx_branch, expected, message,
                                            rt['gmx_base_sha'], env, validate_before_push=validator)
                if not sha:
                    sha = commit_and_push(gmx, expected, message,
                                          gmx_branch, env, max_lines=max_lines,
                                          validate_before_push=validator)
                patch_runtime(task_id, gmx_sha=sha)
            step(task_id, 'GMX_PUSH', gmx_push)

            def gmx_build():
                rt = read_state(task_id)['runtime']
                verify_pushed(gmx, gmx_branch, rt['gmx_sha'], env)
                with JenkinsBrowser(conf, DATA, home, 'gmx', visible=params.get('visible', False), verbose=params.get('verbose', False)) as browser:
                    browser.gmx_settings = params.get('gmx_parameters', {})
                    ensure_bootstrap(browser, params['gmx_job'], task_id, 'gmx')
                    num, console = formal_build(browser, params['gmx_job'], task_id, 'gmx',
                                                country=params['country'], project=params['project'])
                    regex = conf.get('build', {}).get('upload_success_pattern', '')
                    syncing = params.get('gmx_parameters', {}).get('SYNC_UPLOAD', True)
                    uploaded = ('NOT_INDEPENDENTLY_VERIFIED' if syncing
                                else 'DOWNSTREAM_UPLOAD_NOT_VERIFIED')
                    if not syncing:
                        log('[GMX] SYNC_UPLOAD=false：此 Jenkins SUCCESS 不等于下游 Upload 完成，需独立核对')
                    if regex:
                        # Check actual final build console, not previously cached success.
                        if not console:
                            browser.visit(params['gmx_job'] + str(num) + '/consoleText')
                            console = browser.page.locator('body').evaluate('(e) => e.innerText.slice(-30000)')
                        if not re.search(regex, console):
                            raise NeedsAttention('GMX Build 已成功，但未检测到配置中的 Upload 成功日志')
                        uploaded = 'VERIFIED_BY_CONFIGURED_LOG_PATTERN'
                    patch_runtime(task_id, gmx_build_number=num, upload_validation=uploaded)
                    log(f'[GMX] Jenkins SUCCESS #{num} | 上传校验: {uploaded}')
            step(task_id, 'GMX_JENKINS', gmx_build)

        # Persist result before announcing SUCCESS; interrupted result writing
        # can be retried instead of leaving a SUCCESS task with no report.
        result = {'task_id': task_id, 'status': 'SUCCESS', 'plugin_branch': plugin_branch,
                  'plugin_job': params['plugin_job'], 'gmx_branch': gmx_branch,
                  'gmx_job': params['gmx_job'], 'country': params['country'],
                  'project': params['project'], 'folder': params.get('gmx_parameters', {}).get('CUSTOM_FOLDER') or f"0_A_{params['country']}_{params['project']}",
                  'runtime': read_state(task_id)['runtime']}
        tmp = home / 'result.json.tmp'
        tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(tmp, home / 'result.json')
        update_task(task_id, status='SUCCESS', stage='FINISHED', note='Jenkins SUCCESS')
        log('=== SUCCESS：Plugin、GMX Jenkins 构建成功。上传是否独立确认请检查 result.json ===')
    except NeedsAttention as e:
        update_task(task_id, status='NEEDS_ATTENTION', note=str(e)[:500])
        log(f'[NEEDS_ATTENTION] {e}')
        raise
    except TimeoutError as e:
        update_task(task_id, status='TIMEOUT', note=str(e)[:500])
        log(f'[TIMEOUT] {e}')
        raise
    except BaseException as e:
        update_task(task_id, status='FAILED', note=str(e)[:500])
        log(f'[FAILED] {type(e).__name__}: {e}')
        raise

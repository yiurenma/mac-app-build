from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from .config import DATA, ROOT, load_config
from .versions import validate_args, branch_names, job_urls, folder
from .store import create_task, task, task_list, update_task, update_task_params, task_folder, active_count
from .jenkins import interactive_login, saved_auth_path
from .auth import store_credentials, read_credentials, CredentialError


def args_parser():
    p = argparse.ArgumentParser(prog='app-build', description='macOS Plugin -> GMX APP 自动构建')
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='创建并启动独立 APP 任务')
    for name in ('plugin-branch', 'gmx-branch', 'plugin-version', 'project-name'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--plugin-url', help='覆盖 Plugin GitHub URL')
    run.add_argument('--update', action='store_true', help='启动前安全执行 git pull --ff-only（由 ./app-build 处理）')
    run.add_argument('--jenkins-user', help='首次填写 Jenkins 用户名并保存到 Mac Keychain')
    run.add_argument('--jenkins-password', help='首次填写 Jenkins 密码并保存到 Mac Keychain（命令历史可能可见）')
    run.add_argument('--gmx-url', help='覆盖 GMX GitHub URL')
    run.add_argument('--country', default='UK')
    run.add_argument('--dry-run', action='store_true', help='仅预览，不克隆/修改/提交/触发 Jenkins')
    run.add_argument('--wait', action='store_true', help='前台运行，否则后台独立进程执行')
    run.add_argument('--watch', action='store_true', help='后台运行但实时跟踪终端日志；Ctrl+C 仅退出观察')
    run.add_argument('--live', action='store_true', help='完整现场模式：相当于 --watch --visible --verbose')
    run.add_argument('--visible', action='store_true', help='显示 Playwright 操作的 Chrome 窗口（headful）')
    run.add_argument('--verbose', action='store_true', help='逐项输出 Git 命令、浏览器动作、状态变化（隐藏凭据）')
    resume = sub.add_parser('resume', help='恢复上次未完成任务')
    resume.add_argument('task_id')
    resume.add_argument('--wait', action='store_true')
    resume.add_argument('--update', action='store_true')
    resume.add_argument('--jenkins-user')
    resume.add_argument('--jenkins-password')
    resume.add_argument('--watch', action='store_true', help='恢复任务后实时监控')
    resume.add_argument('--live', action='store_true', help='恢复时启用完整现场模式')
    resume.add_argument('--visible', action='store_true', help='恢复后显示真实浏览器')
    resume.add_argument('--verbose', action='store_true', help='恢复后显示详细操作轨迹')
    watch = sub.add_parser('watch', help='实时查看现有任务进度、日志和等待时间')
    watch.add_argument('task_id')
    watch.add_argument('--interval', type=int, default=15, help='无新日志时显示状态的间隔秒数，默认15')
    worker = sub.add_parser('worker', help=argparse.SUPPRESS)
    worker.add_argument('task_id')
    sub.add_parser('status', help='查看任务')
    logs = sub.add_parser('logs', help='显示任务日志')
    logs.add_argument('task_id')
    logs.add_argument('--follow', '-f', action='store_true')
    login = sub.add_parser('login', help='手动登录 Jenkins，保存本地认证状态')
    login.add_argument('--site', choices=['plugin', 'gmx'], required=True)
    sub.add_parser('doctor', help='检查本机 Python/Git/Playwright/Token')
    auth = sub.add_parser('auth', help='仅首次保存 Jenkins 账号密码到 Mac Keychain')
    auth.add_argument('--jenkins-user', required=True)
    auth.add_argument('--jenkins-password', required=True)
    return p


def create_params(a, c):
    plugin_url = a.plugin_url or c['github']['plugin_url']
    gmx_url = a.gmx_url or c['github']['gmx_url']
    country = a.country.upper()
    validate_args(a.plugin_version, a.plugin_branch, a.gmx_branch, country, a.project_name)
    return {'plugin_source': a.plugin_branch, 'gmx_source': a.gmx_branch,
            'plugin_version_input': a.plugin_version, 'country': country,
            'project': a.project_name, 'plugin_url': plugin_url, 'gmx_url': gmx_url,
            'visible': bool(a.visible), 'verbose': bool(a.verbose)}


def spawn(task_id, wait=False):
    if wait:
        from .runner import run
        run(task_id)
        return
    log_path = task_folder(task_id) / 'run.log'
    cmd = [sys.executable, '-m', 'appbuilder', 'worker', task_id]
    if sys.platform == 'darwin' and shutil.which('caffeinate'):
        cmd = ['caffeinate', '-i', *cmd]
    with log_path.open('ab') as out:
        p = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    # Only the Worker updates its own pid/status. A fast worker can already
    # have reached SUCCESS before Popen returns; never overwrite it here.
    print(f'任务后台启动: {task_id} (PID={p.pid})\n日志: ./app-build logs {task_id} --follow')



STAGES = (
    'PLUGIN_CHECKOUT', 'PLUGIN_EDIT', 'PLUGIN_PUSH', 'PLUGIN_JENKINS',
    'GMX_CHECKOUT', 'GMX_EDIT', 'GMX_BOOTSTRAP', 'GMX_PUSH', 'GMX_JENKINS', 'FINISHED',
)


def stage_indicator(stage):
    """Workflow stage, NOT an estimated Jenkins build completion percentage."""
    return f"{STAGES.index(stage)+1}/{len(STAGES)}" if stage in STAGES else '-/10'


def watch_task(task_id, interval=15):
    """Passive monitor: prints log lines plus heartbeat; never controls Jenkins."""
    path = task_folder(task_id) / 'run.log'
    finished = ('SUCCESS', 'FAILED', 'TIMEOUT', 'NEEDS_ATTENTION', 'INTERRUPTED')
    last_message = time.monotonic()
    last_stage = None
    stream = None
    try:
        while True:
            # Worker can create the log just after task creation.
            if stream is None and path.exists():
                stream = path.open('r', encoding='utf-8', errors='replace')
            line = stream.readline() if stream else ''
            if line:
                print(line, end='', flush=True)
                last_message = time.monotonic()
                continue
            row = task(task_id)
            if row['stage'] != last_stage:
                print(f"[WATCH] 阶段 {stage_indicator(row['stage'])} {row['stage']} status={row['status']} note={row['note']}", flush=True)
                last_stage = row['stage']
            if row['status'] in finished:
                print(f"[WATCH] 任务结束: {row['status']} {row['note']}", flush=True)
                return
            if row['status'] == 'RUNNING' and not alive(row['pid']):
                print('[WATCH] Worker 进程已经退出；可运行 ./app-build status / resume', flush=True)
                return
            if time.monotonic() - last_message >= interval:
                print(f"[WATCH] 仍在等待：阶段 {stage_indicator(row['stage'])} {row['stage']} | status={row['status']} | 最近 {interval}s 无新日志", flush=True)
                last_message = time.monotonic()
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[WATCH] 已退出观察，后台构建任务继续运行。', flush=True)
    finally:
        if stream:
            stream.close()


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True



def doctor_check(config, *, strict=False):
    """Preflight performed on every real run/resume; no network side-effects."""
    issues = []
    if sys.version_info < (3, 11):
        issues.append('Python 版本必须 >= 3.11')
    if not shutil.which('git'):
        issues.append('未安装 Git')
    try:
        import playwright.sync_api
        import yaml
    except ImportError:
        issues.append('缺少 Playwright / PyYAML（请运行 ./install.sh）')
    browser = config.get('jenkins', {}).get('browser_channel', 'chromium')
    if browser not in ('chromium', 'chrome', 'msedge'):
        issues.append('配置的 Jenkins 浏览器通道不合法')
    if sys.platform == 'darwin' and browser == 'chrome':
        chrome_locations = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                            Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
        if not any(v.exists() for v in chrome_locations):
            issues.append('找不到 Google Chrome，请安装公司管理的 Chrome 或选择其他 browser_channel')
    token_name = config.get('github', {}).get('token_env', 'GITHUB_TOKEN')
    if not os.environ.get(token_name):
        issues.append(f'{token_name} 尚未设置')
    auth_cached = all(saved_auth_path(DATA, site).exists() for site in ('plugin', 'gmx'))
    keychain = read_credentials() is not None
    if not auth_cached and not keychain:
        issues.append('Jenkins 认证未配置：首次请指定 --jenkins-user 和 --jenkins-password')
    print(f'[DOCTOR] Python={sys.version.split()[0]} | Git={bool(shutil.which("git"))} | '
          f'Playwright={"缺失" if "缺少 Playwright / PyYAML（请运行 ./install.sh）" in issues else "已安装"} | '
          f'Jenkins 认证={"已保存" if auth_cached or keychain else "未配置"}', flush=True)
    for issue in issues:
        print('[DOCTOR] ' + issue, flush=True)
    if strict and issues:
        raise RuntimeError('启动前环境检查未通过；请先处理上面提示的问题')
    return not issues


def setup_credentials(args):
    user = getattr(args, 'jenkins_user', None)
    password = getattr(args, 'jenkins_password', None)
    if bool(user) != bool(password):
        raise CredentialError('请同时填写 --jenkins-user 与 --jenkins-password')
    if user and password:
        store_credentials(user, password)
        print('[AUTH] Jenkins 凭据已保存到 macOS Keychain（后续运行无需重复输入）', flush=True)


def main(argv=None):
    p = args_parser()
    a = p.parse_args(argv)
    if a.command in ('run', 'resume') and a.live:
        a.watch = a.visible = a.verbose = True
    if a.command == 'doctor':
        doctor_check(load_config())
        return
    if a.command == 'auth':
        setup_credentials(a)
        return
    if a.command == 'login':
        interactive_login(load_config(), DATA, a.site)
        return
    if a.command == 'run':
        c = load_config()
        params = create_params(a, c)
        if a.dry_run:
            stamp = datetime.now().strftime('%Y%m%d%H%M')
            generated = branch_names(params['plugin_version_input'], stamp)
            urls = job_urls(c, generated['plugin_branch'], generated['gmx_branch'])
            print(json.dumps({**params, **generated, **urls, 'folder': folder(params['country'], params['project']),
                              'note': '预览而已：没有 Git、Jenkins、文件写入操作。正式任务时间戳由并发管理器分配。'},
                             ensure_ascii=False, indent=2))
            return
        if a.wait and a.watch:
            p.error('--wait 和 --watch 不能同时使用')
        setup_credentials(a)
        doctor_check(c, strict=True)
        if active_count() >= int(c.get('build', {}).get('max_parallel', 2)):
            p.error('并发任务已达上限，请先等待任务结束或调整 config.yaml')
        # Refuse to queue tasks that cannot read an environment token.
        from .config import token_or_error
        token_or_error(c)
        payload = create_task(c, params)
        spawn(payload['id'], a.wait)
        if a.watch:
            watch_task(payload['id'])
        return
    if a.command == 'worker':
        from .runner import run
        run(a.task_id)
        return
    if a.command == 'watch':
        task(a.task_id)  # verify ID even when log has not appeared yet
        watch_task(a.task_id, interval=max(1, a.interval))
        return
    if a.command == 'resume':
        if a.wait and a.watch:
            p.error('--wait 和 --watch 不能同时使用')
        setup_credentials(a)
        doctor_check(load_config(), strict=True)
        r = task(a.task_id)
        if r['status'] in ('RUNNING', 'QUEUED') and alive(r['pid']):
            p.error('任务进程仍在运行，不能重复恢复')
        if r['status'] == 'SUCCESS':
            print('已 SUCCESS，不需要恢复')
            return
        if r['status'] in ('RUNNING', 'QUEUED') and not alive(r['pid']):
            update_task(r['id'], status='INTERRUPTED', note='先前的 Worker 进程已退出')
        if active_count() >= int(load_config().get('build', {}).get('max_parallel', 2)):
            p.error('并发任务已达上限')
        from .config import token_or_error
        token_or_error(load_config())
        if a.visible or a.verbose:
            update_task_params(a.task_id, visible=(True if a.visible else None),
                               verbose=(True if a.verbose else None))
        spawn(a.task_id, a.wait)
        if a.watch:
            watch_task(a.task_id)
        return
    if a.command == 'status':
        for r in task_list():
            status = r['status']
            if status == 'RUNNING' and not alive(r['pid']):
                status = 'INTERRUPTED'
                update_task(r['id'], status=status, note='进程不在运行；可执行 resume')
            print(f"{r['id']:23s} {status:17s} {stage_indicator(r['stage'])} {r['stage']:20s} {r['note'][:85]}")
        return
    if a.command == 'logs':
        if a.follow:
            task(a.task_id)
            watch_task(a.task_id)
            return
        path = task_folder(a.task_id) / 'run.log'
        if not path.exists():
            print('尚未找到日志文件（前台 --wait 运行直接显示到终端）')
            return
        with path.open(encoding='utf-8', errors='replace') as f:
            while True:
                line = f.readline()
                if line:
                    print(line, end='', flush=True)
                elif a.follow:
                    time.sleep(1)
                else:
                    break


if __name__ == '__main__':
    main()

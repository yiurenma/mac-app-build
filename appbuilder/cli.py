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
from .store import create_task, task, task_list, update_task, task_folder, active_count
from .jenkins import interactive_login


def args_parser():
    p = argparse.ArgumentParser(prog='app-build', description='macOS Plugin -> GMX APP 自动构建')
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='创建并启动独立 APP 任务')
    for name in ('plugin-branch', 'gmx-branch', 'plugin-version', 'project-name'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--plugin-url', help='覆盖 Plugin GitHub URL')
    run.add_argument('--gmx-url', help='覆盖 GMX GitHub URL')
    run.add_argument('--country', default='UK')
    run.add_argument('--dry-run', action='store_true', help='仅预览，不克隆/修改/提交/触发 Jenkins')
    run.add_argument('--wait', action='store_true', help='前台运行，否则后台独立进程执行')
    resume = sub.add_parser('resume', help='恢复上次未完成任务')
    resume.add_argument('task_id')
    resume.add_argument('--wait', action='store_true')
    worker = sub.add_parser('worker', help=argparse.SUPPRESS)
    worker.add_argument('task_id')
    sub.add_parser('status', help='查看任务')
    logs = sub.add_parser('logs', help='显示任务日志')
    logs.add_argument('task_id')
    logs.add_argument('--follow', '-f', action='store_true')
    login = sub.add_parser('login', help='手动登录 Jenkins，保存本地认证状态')
    login.add_argument('--site', choices=['plugin', 'gmx'], required=True)
    sub.add_parser('doctor', help='检查本机 Python/Git/Playwright/Token')
    return p


def create_params(a, c):
    plugin_url = a.plugin_url or c['github']['plugin_url']
    gmx_url = a.gmx_url or c['github']['gmx_url']
    country = a.country.upper()
    validate_args(a.plugin_version, a.plugin_branch, a.gmx_branch, country, a.project_name)
    return {'plugin_source': a.plugin_branch, 'gmx_source': a.gmx_branch,
            'plugin_version_input': a.plugin_version, 'country': country,
            'project': a.project_name, 'plugin_url': plugin_url, 'gmx_url': gmx_url}


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


def main(argv=None):
    p = args_parser()
    a = p.parse_args(argv)
    if a.command == 'doctor':
        c = load_config()
        print('Python:', sys.version.split()[0], '| macOS:', sys.platform == 'darwin')
        print('git:', shutil.which('git') or '未安装')
        print('caffeinate:', shutil.which('caffeinate') or '未找到 (macOS 运行时推荐)')
        print(c['github'].get('token_env', 'GITHUB_TOKEN') + ':', '已设置' if os.environ.get(c['github'].get('token_env', 'GITHUB_TOKEN')) else '未设置')
        try:
            import playwright
            print('playwright: 已安装；浏览器通道:', c.get('jenkins', {}).get('browser_channel', 'chromium'))
        except ImportError:
            print('playwright: 未安装')
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
        if active_count() >= int(c.get('build', {}).get('max_parallel', 2)):
            p.error('并发任务已达上限，请先等待任务结束或调整 config.yaml')
        # Refuse to queue tasks that cannot read an environment token.
        from .config import token_or_error
        token_or_error(c)
        payload = create_task(c, params)
        spawn(payload['id'], a.wait)
        return
    if a.command == 'worker':
        from .runner import run
        run(a.task_id)
        return
    if a.command == 'resume':
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
        spawn(a.task_id, a.wait)
        return
    if a.command == 'status':
        for r in task_list():
            status = r['status']
            if status == 'RUNNING' and not alive(r['pid']):
                status = 'INTERRUPTED'
                update_task(r['id'], status=status, note='进程不在运行；可执行 resume')
            print(f"{r['id']:23s} {status:17s} {r['stage']:20s} {r['note'][:85]}")
        return
    if a.command == 'logs':
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

from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path
from contextlib import contextmanager
from tempfile import TemporaryDirectory
import time
from .telemetry import debug, safe_command, operation, safe_text


@contextmanager
def credentials(token_env="GITHUB_TOKEN"):
    """Read selected enterprise token env; use temporary private askpass only."""
    with TemporaryDirectory(prefix='appbuilder-askpass-') as td:
        script = Path(td) / 'askpass.sh'
        script.write_text('''#!/bin/sh
case "$1" in
  *[Uu]sername*) printf 'x-access-token\\n' ;;
  *[Pp]assword*) printf '%s\\n' "$APP_BUILDER_GIT_TOKEN" ;;
  *) exit 1 ;;
esac
''', encoding='utf-8')
        script.chmod(0o700)
        env = os.environ.copy()
        token = os.environ.get(token_env, '').strip()
        if not token:
            raise RuntimeError(f'未设置 {token_env}; 无法 Git 认证')
        env['APP_BUILDER_GIT_TOKEN'] = token
        env['GIT_ASKPASS'] = str(script)
        env['GIT_TERMINAL_PROMPT'] = '0'
        yield env


def git(args, cwd=None, env=None, capture=True):
    command = ['git', '-c', 'credential.helper=', *args]
    started = time.monotonic()
    debug(f'$ {safe_command(command, env)} (cwd={cwd or "."})')
    p = subprocess.run(command, cwd=cwd, env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    operation('GIT', f'结束: git {args[0]} exit={p.returncode} elapsed={time.monotonic()-started:.1f}s')
    if p.stdout.strip():
        clean = safe_text(p.stdout.strip(), env)
        if len(clean) > 8000:
            clean = clean[:8000] + '\n... [输出过长已截断，Git 命令仍完整执行]'
        for line in clean.splitlines():
            operation('GIT-OUT', line[:1500])
    if p.returncode:
        # git itself should not print token; avoid echoing untrusted credential messages
        msg = p.stdout[-2500:]
        # Redact both the configured credential and the internal askpass variable.
        for secret in (os.environ.get('GITHUB_TOKEN'), (env or {}).get('APP_BUILDER_GIT_TOKEN')):
            if secret:
                msg = msg.replace(secret, '[REDACTED]')
        raise RuntimeError(f"Git 操作失败 (exit {p.returncode}): {args[0]}\n{msg}")
    return p.stdout.strip() if capture else ''


def clone_branch(url: str, source: str, target: str, directory: Path, env):
    if directory.exists():
        raise RuntimeError(f'仓库目录已存在且尚未记录完成: {directory}; 为避免覆盖，请检查后恢复')
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Repository URL is HTTPS without embedded credentials.
    if not url.startswith('https://') or '@' in url.split('/')[2]:
        raise ValueError('仓库 URL 必须为无 Token 的 HTTPS 地址')
    git(['clone', '--no-checkout', url, str(directory)], env=env)
    git(['fetch', '--no-tags', 'origin', f'+refs/heads/{source}:refs/remotes/origin/{source}'], cwd=directory, env=env)
    base_sha = git(['rev-parse', f'refs/remotes/origin/{source}'], cwd=directory)
    git(['checkout', '-b', target, base_sha], cwd=directory)
    return base_sha


def status(directory):
    return git(['status', '--porcelain', '--untracked-files=normal'], cwd=directory)


def changed_files(directory):
    lines = git(['diff', '--name-only', 'HEAD'], cwd=directory).splitlines()
    untracked = git(['ls-files', '--others', '--exclude-standard'], cwd=directory).splitlines()
    return sorted(set(lines + untracked))


def commit_and_push(directory: Path, expected: set[str], message: str,
                    target: str, env, max_lines=100, validate_before_push=None):
    if git(['branch', '--show-current'], cwd=directory) != target:
        raise ValueError(f'{directory} 不是目标分支 {target}')
    changed = set(changed_files(directory))
    if not changed:
        raise ValueError('没有任何文件变化，不能提交空 Commit')
    if changed - expected:
        raise ValueError(f'检测到非预期文件变化: {sorted(changed - expected)}；已停止提交')
    git(['diff', '--check'], cwd=directory)
    # Never stage unrelated paths. Explicitly disallow Git hook rewrites to files outside allowlist.
    git(['add', '--', *sorted(changed)], cwd=directory)
    staged = set(git(['diff', '--cached', '--name-only'], cwd=directory).splitlines())
    if staged != changed:
        raise ValueError(f'Staged 文件不符合预期: {staged} != {changed}')
    change_stats = git(['diff', '--cached', '--numstat'], cwd=directory)
    total_lines = 0
    for row in change_stats.splitlines():
        a, d, *_ = row.split('\t')
        if not a.isdecimal() or not d.isdecimal():
            raise ValueError('检测到二进制文件变更，不自动提交')
        total_lines += int(a) + int(d)
    if total_lines > max_lines:
        raise ValueError(f'本次修改 {total_lines} 行，超过安全上限 {max_lines}，请人工检查')
    git(['commit', '-m', message], cwd=directory)
    sha = git(['rev-parse', 'HEAD'], cwd=directory)
    # Hooks may amend, stage or alter files: check actual commit and worktree.
    actual = set(git(['diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'], cwd=directory).splitlines())
    if actual != changed:
        raise ValueError(f'Commit 实际修改文件不一致: {actual} != {changed}；禁止 Push')
    if set(changed_files(directory)):
        raise ValueError('Commit Hook 或其他进程生成了新的工作区修改；禁止 Push')
    if validate_before_push is not None:
        validate_before_push()
    # No force-push. Verify new remote commit actually points to ours.
    git(['push', '-u', 'origin', f'HEAD:refs/heads/{target}'], cwd=directory, env=env)
    remote = git(['ls-remote', 'origin', f'refs/heads/{target}'], cwd=directory, env=env)
    if not remote or remote.split()[0] != sha:
        raise RuntimeError('Push 已尝试，但远端 Commit SHA 与本地不同')
    return sha


def verify_pushed(directory: Path, target: str, sha: str, env):
    if git(['rev-parse', 'HEAD'], cwd=directory) != sha:
        raise RuntimeError('本地 HEAD SHA 已改变，禁止继续')
    remote = git(['ls-remote', 'origin', f'refs/heads/{target}'], cwd=directory, env=env)
    if not remote or remote.split()[0] != sha:
        raise RuntimeError('远端分支 SHA 不匹配或不可访问')


def recover_pushed_commit(directory: Path, target: str, expected: set[str],
                           expected_message: str, base_sha: str, env, validate_before_push=None):
    """Recover crash after commit or push, but before runtime state was saved."""
    if set(changed_files(directory)):
        return None
    sha = git(['rev-parse', 'HEAD'], cwd=directory)
    if sha == base_sha:
        return None
    subject = git(['log', '-1', '--format=%s'], cwd=directory)
    if subject != expected_message:
        raise RuntimeError('工作区没有修改但 HEAD 信息不符合自动构建 Commit，已停止')
    if git(['rev-parse', 'HEAD^'], cwd=directory) != base_sha:
        raise RuntimeError('工作分支首个 Commit 未基于记录的源分支 SHA')
    actual = set(git(['diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'], cwd=directory).splitlines())
    if actual != expected:
        raise RuntimeError(f'恢复 Commit 实际文件异常: {sorted(actual)}')
    if validate_before_push is not None:
        validate_before_push()
    remote = git(['ls-remote', 'origin', f'refs/heads/{target}'], cwd=directory, env=env)
    if not remote:
        git(['push', '-u', 'origin', f'HEAD:refs/heads/{target}'], cwd=directory, env=env)
    verify_pushed(directory, target, sha, env)
    return sha


def git_blob(directory: Path, rev: str, relpath: str):
    """Exact bytes: unlike git() it does not strip trailing whitespace or CRLF."""
    operation('GIT', f'$ git show {rev}:{relpath} (cwd={directory}) [内容不回显]')
    p = subprocess.run(['git', 'show', f'{rev}:{relpath}'], cwd=directory,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    operation('GIT', f'git show exit={p.returncode} bytes={len(p.stdout)}')
    if p.returncode:
        raise RuntimeError(f'无法读取 Git 版本 {rev} 的文件 {relpath}')
    return p.stdout

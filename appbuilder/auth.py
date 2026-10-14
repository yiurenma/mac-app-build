"""User opt-in Jenkins credentials stored in macOS Keychain, never task JSON.

Keychain is a convenience, NOT an SSO/MFA bypass. Only recognized and
allowlisted login forms can be submitted automatically.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

SERVICE = 'mac-app-builder.jenkins'


class CredentialError(RuntimeError):
    pass


def store_credentials(username: str, password: str):
    username = str(username or '').strip()
    if not username or not password:
        raise CredentialError('必须同时提供 Jenkins 用户名和密码')
    if sys.platform != 'darwin':
        raise CredentialError('自动保存 Jenkins 凭据目前仅支持 macOS Keychain')
    # Keychain receives a password exactly once; it is never written to files,
    # task state, Git commits or terminal logs. Avoid shell=True.
    proc = subprocess.run(
        ['security', 'add-generic-password', '-U', '-s', SERVICE, '-a', username, '-w', password],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise CredentialError('无法将 Jenkins 凭据写入 Mac Keychain；请检查钥匙串访问权限')
    # Separately stored account name is not a secret; don't persist password.
    proc = subprocess.run(['security', 'add-generic-password', '-U', '-s', SERVICE + '.account',
                           '-a', 'default', '-w', username], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise CredentialError('已保存密码但无法保存 Jenkins 账号索引，请检查 Mac Keychain')


def read_credentials():
    """Return (username, password), or None when not configured."""
    if sys.platform != 'darwin':
        return None
    name = subprocess.run(['security', 'find-generic-password', '-s', SERVICE + '.account',
                           '-a', 'default', '-w'], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if name.returncode or not name.stdout.strip():
        return None
    username = name.stdout.strip()
    pwd = subprocess.run(['security', 'find-generic-password', '-s', SERVICE,
                          '-a', username, '-w'], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if pwd.returncode or not pwd.stdout.strip():
        return None
    return username, pwd.stdout.rstrip('\n')


def approved_login_host(url: str, jenkins_url: str, extra_hosts=()):
    """Prevent entering credentials on untrusted redirect destinations."""
    u = urlsplit(url)
    expected = urlsplit(jenkins_url)
    if u.scheme != 'https' or not u.hostname:
        return False
    host = u.hostname.lower().rstrip('.')
    candidates = [expected.hostname] + list(extra_hosts or ())
    for rule in candidates:
        rule = str(rule or '').lower().strip().rstrip('.')
        if rule.startswith('*.') and host.endswith(rule[1:]) and host != rule[2:]:
            return True
        if host == rule:
            return True
    return False


def _one_visible(page, selectors):
    """Return one uniquely recognizable and visible login input."""
    found = []
    for css in selectors:
        locator = page.locator(css)
        if locator.count() == 1 and locator.is_visible():
            found.append(locator)
    # Different selectors can refer to the same element. Use the first specific
    # match, but refuse if the page has multiple visible password fields.
    return found[0] if found else None


def submit_recognized_login(page, username, password, *, log=None, validate_target=None):
    """Conservatively fill known Jenkins/enterprise form controls.

    True means one submit click was attempted, not verified authentication.
    Callers must verify their exact authenticated destination; never retry a
    password submit merely because its asynchronous handler has not completed.
    Return False for an unknown login page rather than accidentally entering
    corporate passwords into arbitrary forms. MFA stays subject to IdP rules.
    """
    def note(message):
        if log:
            log(message)

    initial = urlsplit(getattr(page, 'url', ''))

    def approved(url):
        try:
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                return False
            if validate_target is not None:
                return bool(validate_target(url))
            return (parsed.scheme, parsed.hostname, parsed.port) == (initial.scheme, initial.hostname, initial.port)
        except Exception:
            return False

    def safe_control(control):
        # Re-evaluate after each SSO navigation and immediately before each action.
        if not approved(getattr(page, 'url', '')):
            note('登录目标未获批准，停止自动填写或提交')
            return False
        try:
            targets = control.evaluate("""el => {
              const urls=[];
              if(el.form) {
                urls.push(el.form.action || location.href);
                for(const button of el.form.querySelectorAll('[formaction]')) urls.push(button.formAction);
              }
              if(el.hasAttribute('formaction')) urls.push(el.formAction);
              return urls;
            }""")
            allowed = isinstance(targets, list) and all(isinstance(url, str) and approved(url) for url in targets)
        except Exception:
            allowed = False
        if not allowed:
            note('登录表单提交目标未获批准，停止自动填写或提交')
        return allowed

    def ready(control, *, editable=False, submitting=False):
        try:
            control.wait_for(state='visible', timeout=5000)
            deadline = time.monotonic() + 5
            while not control.is_enabled() or (editable and not control.is_editable()):
                if time.monotonic() >= deadline:
                    note('登录控件仍未启用或不可编辑，停止自动填写')
                    return False
                page.wait_for_timeout(50)
            if submitting and not control.evaluate('el => !el.form || el.form.checkValidity()'):
                note('登录表单校验未通过，未提交认证')
                return False
        except Exception:
            return False
        return safe_control(control)

    user_fields = ['input[name="j_username"]', 'input[name="username"]',
                   'input[name="loginfmt"]', 'input#i0116',
                   'input[type="email"]', 'input[autocomplete="username"]']
    pass_fields = ['input[name="j_password"]', 'input[type="password"]',
                   'input[autocomplete="current-password"]']
    user = _one_visible(page, user_fields)
    pwd = _one_visible(page, pass_fields)
    if not user and not pwd:
        # DOM load is not form readiness: allow bounded asynchronous initialization.
        try:
            page.locator(','.join(css + ':visible' for css in user_fields + pass_fields)).first.wait_for(state='visible', timeout=5000)
        except Exception:
            return False
        user = _one_visible(page, user_fields)
        pwd = _one_visible(page, pass_fields)
        if not user and not pwd:
            return False
    if user:
        if not ready(user, editable=True):
            return False
        note('填写 Jenkins/SSO 账号（已隐藏）')
        user.fill(username)
    if not pwd:
        # Only a known 2-step email/login flow can be advanced safely.
        selectors = ['#idSIButton9', 'button[type="submit"]', 'input[type="submit"]']
        btn = _one_visible(page, selectors)
        if not btn:
            return False
        if not ready(btn, submitting=True):
            return False
        note('点击登录下一步')
        btn.click()
        try:
            page.locator('input[type="password"]').wait_for(state='visible', timeout=15000)
        except Exception:
            return False
        pwd = _one_visible(page, pass_fields)
    if not pwd:
        return False
    if not ready(pwd, editable=True):
        return False
    note('填写 Jenkins/SSO 密码（已隐藏）')
    pwd.fill(password)
    submit = _one_visible(page, ['#idSIButton9', 'button[type="submit"]',
                                 'input[type="submit"]'])
    if not submit:
        return False
    if not ready(submit, submitting=True):
        return False
    note('点击认证表单提交')
    submit.click()
    return True

GITHUB_SERVICE = 'mac-app-builder.github-token'


def store_github_token(token: str):
    """Store PAT in local Keychain; never in browser storage, source or task params."""
    token = str(token or '').strip()
    if not token or len(token) > 4096 or '\n' in token or '\r' in token:
        raise CredentialError('GitHub PAT 不能为空、过长或包含换行')
    if sys.platform != 'darwin':
        raise CredentialError('保存 GitHub PAT 目前仅支持 macOS Keychain')
    result = subprocess.run(
        ['security', 'add-generic-password', '-U', '-s', GITHUB_SERVICE,
         '-a', 'enterprise', '-w', token],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CredentialError('GitHub PAT 保存到 Mac Keychain 失败')


def read_github_token():
    """Read locally stored PAT without printing it or copying it to task DB."""
    if sys.platform != 'darwin':
        return None
    result = subprocess.run(
        ['security', 'find-generic-password', '-s', GITHUB_SERVICE,
         '-a', 'enterprise', '-w'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        return None
    return result.stdout.strip() or None


JIRA_SERVICE = 'mac-app-builder.jira-token'


def store_jira_token(token: str):
    """Store the Jira PAT in the local Keychain; never in the page, task params or logs."""
    token = str(token or '').strip()
    if not token or len(token) > 4096 or '\n' in token or '\r' in token:
        raise CredentialError('Jira Token 不能为空、过长或包含换行')
    if sys.platform != 'darwin':
        raise CredentialError('保存 Jira Token 目前仅支持 macOS Keychain')
    result = subprocess.run(
        ['security', 'add-generic-password', '-U', '-s', JIRA_SERVICE, '-a', 'enterprise', '-w', token],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CredentialError('Jira Token 保存到 Mac Keychain 失败')


def read_jira_token():
    if sys.platform != 'darwin':
        return None
    result = subprocess.run(
        ['security', 'find-generic-password', '-s', JIRA_SERVICE, '-a', 'enterprise', '-w'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (result.stdout.strip() or None) if result.returncode == 0 else None

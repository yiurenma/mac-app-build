"""User opt-in Jenkins credentials stored in macOS Keychain, never task JSON.

Keychain is a convenience, NOT an SSO/MFA bypass. Only recognized and
allowlisted login forms can be submitted automatically.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
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


def submit_recognized_login(page, username, password, *, log=None):
    """Conservatively fill known Jenkins/enterprise form controls.

    Return False for an unknown login page rather than accidentally entering
    corporate passwords into arbitrary forms. MFA stays subject to IdP rules.
    """
    def note(message):
        if log:
            log(message)

    user_fields = ['input[name="j_username"]', 'input[name="username"]',
                   'input[name="loginfmt"]', 'input#i0116',
                   'input[type="email"]', 'input[autocomplete="username"]']
    pass_fields = ['input[name="j_password"]', 'input[type="password"]',
                   'input[autocomplete="current-password"]']
    user = _one_visible(page, user_fields)
    pwd = _one_visible(page, pass_fields)
    if not user and not pwd:
        return False
    if user:
        note('填写 Jenkins/SSO 账号（已隐藏）')
        user.fill(username)
    if not pwd:
        # Only a known 2-step email/login flow can be advanced safely.
        selectors = ['#idSIButton9', 'button[type="submit"]', 'input[type="submit"]']
        btn = _one_visible(page, selectors)
        if not btn:
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
    note('填写 Jenkins/SSO 密码（已隐藏）')
    pwd.fill(password)
    submit = _one_visible(page, ['#idSIButton9', 'button[type="submit"]',
                                 'input[type="submit"]'])
    if not submit:
        return False
    note('点击认证表单提交')
    submit.click()
    return True

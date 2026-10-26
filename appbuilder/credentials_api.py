"""Settings page: GitHub token and the Jenkins browser sign-in (fallback), saved to the Keychain.

These used to be typed into the New build form. They are set once, so they now live on the
settings page. Nothing secret is ever returned to the page.
"""
from .telemetry import safe_text


def status():
    from .auth import read_credentials, read_github_token
    try:
        github = bool(read_github_token())
    except Exception:
        github = False
    try:
        saved = read_credentials()
    except Exception:
        saved = None
    return {'github': github, 'jenkins_user': saved[0] if saved else ''}


def save(body):
    from .auth import CredentialError, store_credentials, store_github_token
    github = str(body.get('github_token') or '').strip()
    user = str(body.get('jenkins_user') or '').strip()
    password = str(body.get('jenkins_password') or '')
    if bool(user) != bool(password):
        raise ValueError('Jenkins 用户名和密码要一起填写')
    if not github and not user:
        raise ValueError('请填写要保存的内容')
    if len(github) > 4096 or len(user) > 256 or len(password) > 4096:
        raise ValueError('填写内容过长')
    try:
        if github:
            store_github_token(github)
        if user:
            store_credentials(user, password)
    except CredentialError as exc:
        raise ValueError(str(exc)) from None
    return status()


def handle_get(path, query):
    if path == '/api/credentials/status':
        return 200, status()
    return None


def handle_post(path, body):
    if path != '/api/credentials/save':
        return None
    try:
        return 200, save(body)
    except ValueError as exc:
        return 400, {'error': safe_text(str(exc))}

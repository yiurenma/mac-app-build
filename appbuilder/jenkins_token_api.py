"""Web endpoints for the Jenkins API-token login (staff ID + one token per Jenkins)."""
from .config import load_config

SITES = {'plugin': ('plugin_parent', 'Plugin Jenkins'), 'gmx': ('gmx_parent', 'Platform Jenkins')}


def status():
    from .auth import read_jenkins_api_token, read_jenkins_username
    return {'user': read_jenkins_username() or '',
            'tokens': {site: bool(read_jenkins_api_token(site)) for site in SITES}}


def save(body):
    from .auth import CredentialError, store_jenkins_api_token, store_jenkins_api_user
    user = str(body.get('user') or '').strip()
    tokens = {site: str(body.get(site + '_token') or '').strip() for site in SITES}
    if not user and not any(tokens.values()):
        raise ValueError('请填写工号或至少一个 Token')
    try:
        if user:
            store_jenkins_api_user(user)
        for site, token in tokens.items():
            if token:
                store_jenkins_api_token(site, token)
    except CredentialError as exc:
        raise ValueError(str(exc)) from None
    return status()


def test(http_factory=None):
    """Read-only: ask each Jenkins who the saved staff ID + token is."""
    from .auth import jenkins_api_credentials
    from .jenkins import NeedsAttention
    from .jenkins_api import JenkinsHttp, who_am_i
    config = load_config()
    out = {}
    for site, (key, label) in SITES.items():
        creds = jenkins_api_credentials(site)
        if not creds:
            out[site] = {'label': label, 'ok': False, 'message': '未保存工号或 Token'}
            continue
        http = (http_factory or (lambda c: JenkinsHttp(*c, timeout=20,
                                                       ca_bundle=config['jenkins'].get('ca_bundle'))))(creds)
        try:
            name = who_am_i(http, config['jenkins'][key])
            out[site] = {'label': label, 'ok': True, 'message': f'Jenkins 识别为：{name}'}
        except NeedsAttention as exc:
            out[site] = {'label': label, 'ok': False, 'message': str(exc)}
    return out


def handle_get(path, query):
    if path == '/api/jenkins-token/status':
        return 200, status()
    return None


def handle_post(path, body):
    try:
        if path == '/api/jenkins-token/save':
            return 200, save(body)
        if path == '/api/jenkins-token/test':
            return 200, test()
    except ValueError as exc:
        return 400, {'error': str(exc)}
    return None

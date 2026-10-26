"""Read-only paginated GitHub branches; credentials never leave the Mac server."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import token_or_error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization header to another host.


def list_branches(body, config):
    from .web import _integer_field, _plain_url
    kind = body.get('kind')
    if kind not in ('plugin', 'gmx'):
        raise ValueError('请选择 Plugin 或 GMX 仓库')
    configured = urlsplit(config['github'][kind + '_url'])
    url = urlsplit(_plain_url(body.get('url'), 'GitHub 仓库'))
    if url.netloc.lower() != configured.netloc.lower():
        raise ValueError('仓库域名与 config.yaml 不一致；请先在本机配置可信 GitHub 域名，再刷新分支')
    parts = url.path.strip('/').removesuffix('.git').split('/')
    if len(parts) != 2 or any(not part or part in ('.', '..') for part in parts):
        raise ValueError('仓库地址应为 https://GitHub域名/组织/仓库')
    page = _integer_field(body, 'page', 1, 1, 100000)
    host = 'https://api.github.com' if url.hostname == 'github.com' else f'https://{url.netloc}/api/v3'
    endpoint = host + '/repos/' + '/'.join(quote(part, safe='') for part in parts)
    endpoint += f'/branches?per_page=100&page={page}'
    try:
        token = token_or_error(config)
    except RuntimeError:
        raise ValueError('本机未找到 GitHub Token；请先配置 Mac Keychain 或环境凭据，再刷新；也可手动填写分支') from None
    request = Request(endpoint, headers={'Authorization': 'Bearer ' + token,
                                        'Accept': 'application/vnd.github+json',
                                        'User-Agent': 'Mac-APP-Builder'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            data = json.load(response)
    except HTTPError as exc:
        messages = {401: 'GitHub 凭据无效，请更新本机 Keychain Token',
                    403: 'GitHub 拒绝访问或请求限流，请检查仓库权限/SSO 后重试',
                    404: '找不到仓库或无权读取，请核对地址及 Token 权限'}
        raise ValueError(messages.get(exc.code, f'GitHub 分支读取失败（HTTP {exc.code}），可手动输入分支')) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ValueError('无法读取 GitHub 分支；请检查公司网络/VPN 后刷新，也可手动输入分支') from None
    if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in data):
        raise ValueError('GitHub 返回的分支数据无效，可手动输入分支后重试')
    return {'branches': [item['name'] for item in data], 'page': page,
            'has_more': len(data) == 100}

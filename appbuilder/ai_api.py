"""Settings page: offline AI card (status, one-click install, use a downloaded file, switches)."""
from .telemetry import safe_text
from . import ai_setup as S

SAMPLE = ('TRANSACT-206313: Fixed payment-consent api param for UK bank transfer',
          'The payment-consent request sent the wrong debtor account parameter for UK bank transfers. '
          'Now the selected account id is sent. Check the consent screen and a full transfer.')


def try_it():
    """Run both helpers once on fixed English samples so the person can see the output."""
    from .config import load_config
    from .local_llm import get_llm, tester_note, failure_hint
    llm = get_llm(load_config())
    if not llm:
        raise ValueError('AI 还没有启用')
    note = tester_note(llm, *SAMPLE)
    hint = failure_hint(llm, 'GMX build', 'Jenkins build #2 finished with result FAILURE.',
                        ['error: No signing certificate "iOS Distribution" found',
                         '** ARCHIVE FAILED **'])
    return {'tester_note': note or '（这次没有得到合格的句子，会退回原来的写法）',
            'failure_hint': hint or '（这次没有得到合格的建议，邮件里就不显示这一段）'}


def try_pr(url, send=True):
    """Test with one real pull request: show exactly what goes to the AI and, if send, the result.
    Only the configured GitHub host is contacted (the token must never go anywhere else)."""
    from urllib.parse import urlsplit
    from .config import load_config, token_or_error
    from .jira_notify import PRLookup
    from .local_llm import get_llm, tester_note, tester_note_prompt
    from .pr_context import fetch, parse_pr_url
    repo_url, number = parse_pr_url(url)
    config = load_config()
    allowed = urlsplit((config.get('github') or {}).get('plugin_url', '')).netloc
    if not allowed or urlsplit(repo_url).netloc != allowed:
        raise ValueError(f'只能试 {allowed or "已配置的企业 GitHub"} 上的 PR')
    llm = get_llm(config)
    if not llm:
        raise ValueError('AI 还没有启用')
    try:
        prs = PRLookup(repo_url, token_or_error(config), ca_bundle=(config.get('jira') or {}).get('ca_bundle'))
        pr = prs.repo_get(f'/pulls/{number}')
    except RuntimeError as exc:
        raise ValueError(safe_text(str(exc))[:300]) from None
    except Exception as exc:  # noqa: BLE001
        raise ValueError(safe_text(f'读取 PR 失败：{type(exc).__name__}')) from None
    full = bool(getattr(llm, 'rich', False)) and S.effective(config).get('pr_context', True)
    extra, stats = fetch(prs.repo_get, number) if full else ('', {})
    title, body = pr.get('title') or '', pr.get('body') or ''
    system, user, _ = tester_note_prompt(title, body, extra, rich=bool(getattr(llm, 'rich', False)))
    out = {'to': 'VS Code Copilot' if getattr(llm, 'rich', False) else '本机离线模型', 'title': title,
           'full_context': full, 'stats': stats, 'chars': len(system) + len(user),
           'sent': (system + '\n\n' + user)[:6000], 'cut': len(system) + len(user) > 6000}
    if send:
        out['tester_note'] = tester_note(llm, title, body, extra) or '（这次没有得到合格的句子，会退回原来的写法）'
    return out


def handle_get(path, query):
    if path == '/api/ai/status':
        return 200, S.status()
    return None


def handle_post(path, body):
    try:
        if path == '/api/ai/install':
            return 200, S.start('download', str(body.get('model') or S.DEFAULT_MODEL))
        if path == '/api/ai/use-file':
            return 200, S.start('file', str(body.get('path') or '').strip())
        if path == '/api/ai/settings':
            values = {k: body[k] for k in ('enabled', *S.FEATURES) if isinstance(body.get(k), bool)}
            if body.get('provider') in S.PROVIDERS:
                values['provider'] = body['provider']
            provider = values.get('provider') or S.saved().get('provider', 'local')
            if values.get('enabled') and provider == 'local' and not S.saved().get('model_path'):
                raise ValueError('请先安装或选择模型文件')
            if values.get('provider') == 'copilot':
                S.setup_copilot()               # install the extension and switch on: nothing else to click
                values.pop('provider')
                values.pop('enabled', None)
            S.save(values)
            from . import local_llm
            local_llm._CACHE.clear()
            return 200, S.status()
        if path == '/api/ai/try-pr':
            return 200, try_pr(str(body.get('url') or ''), send=bool(body.get('send', True)))
        if path == '/api/ai/try':
            return 200, try_it()
    except ValueError as exc:
        return 400, {'error': safe_text(str(exc))}
    return None

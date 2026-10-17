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
        raise ValueError('离线 AI 还没有启用')
    note = tester_note(llm, *SAMPLE)
    hint = failure_hint(llm, 'GMX build', 'Jenkins build #2 finished with result FAILURE.',
                        ['error: No signing certificate "iOS Distribution" found',
                         '** ARCHIVE FAILED **'])
    return {'tester_note': note or '（这次没有得到合格的句子，会退回原来的写法）',
            'failure_hint': hint or '（这次没有得到合格的建议，邮件里就不显示这一段）'}


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
            if values.get('enabled') and not S.saved().get('model_path'):
                raise ValueError('请先安装或选择模型文件')
            S.save(values)
            from . import local_llm
            local_llm._CACHE.clear()
            return 200, S.status()
        if path == '/api/ai/try':
            return 200, try_it()
    except ValueError as exc:
        return 400, {'error': safe_text(str(exc))}
    return None

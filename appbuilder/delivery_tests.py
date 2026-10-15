"""Explicit, independent user-triggered connection/email tests. No build tasks."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from .config import DATA,load_config
from .jenkins import NeedsAttention
from .telemetry import safe_text
from . import lifecycle

_LOCK=threading.Lock()
_ACTIVE=set()


def testfairy_diagnostics(value, secret=()):
    """Expose bounded diagnostic references, never arbitrary browser state."""
    if not isinstance(value,dict):return {}
    def clean(item):
        text=str(item)
        for part in secret or ():
            if isinstance(part,str) and part:text=text.replace(part,'[REDACTED]')
        return safe_text(text)[:1200]
    result={key:clean(value[key]) for key in ('attempt_id','log_path','evidence_path','logging_error')
            if value.get(key) and isinstance(value[key],(str,int,float))}
    first=value.get('first_error')
    if isinstance(first,dict):
        result['first_error']={key:clean(first[key]) for key in ('stage','type','message') if first.get(key)}
        if first.get('phase'):result['first_error']['stage']=clean(first['phase'])
    error=value.get('logging_error')
    if isinstance(error,dict):
        result['logging_error']=' · '.join(clean(error[key]) for key in ('type','message') if error.get(key))
    return result


def connect():
    DATA.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA/'delivery-tests.sqlite',timeout=20);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS tests (id TEXT PRIMARY KEY,kind TEXT,fingerprint TEXT,created REAL,status TEXT,result TEXT)')
    db.commit();return db


def finish(test_id,status,result):
    with closing(connect()) as db,db:
        previous=db.execute('SELECT result FROM tests WHERE id=?',(test_id,)).fetchone()
        if previous:
            audit=json.loads(previous['result'])
            result={**{key:audit[key] for key in ('allow_repeat_test','repeated_unknown_ids','requested_at') if key in audit},**result}
        db.execute('UPDATE tests SET status=?,result=? WHERE id=?',(status,json.dumps(result),test_id))


def poll(test_id):
    if not re.fullmatch(r'[a-f0-9-]{16,64}',test_id):raise ValueError('测试编号无效')
    with closing(connect()) as db:
        row=db.execute('SELECT * FROM tests WHERE id=?',(test_id,)).fetchone()
    if not row:raise ValueError('测试不存在')
    result=json.loads(row['result'])
    status=row['status']
    if status in ('RUNNING','SUBMITTING') and test_id not in _ACTIVE:
        status='UNKNOWN' if row['kind']=='outlook' else 'INTERRUPTED'
    return {'id':test_id,'kind':row['kind'],'status':status,'result':result}


@lifecycle.admitted
def start(body):
    from .jenkins_links import source_build,validate_join_url
    from .outlook import recipients,valid_address
    kind=body.get('kind');test_id=body.get('request_id','')
    if kind not in ('testfairy','outlook') or not re.fullmatch(r'[a-f0-9-]{16,64}',test_id):raise ValueError('测试类型或请求编号无效')
    allow_repeat=body.get('allow_repeat_test',False)
    if not isinstance(allow_repeat,bool):raise ValueError('允许重复测试必须是开关值')
    if kind!='outlook' and allow_repeat:raise ValueError('允许重复测试仅适用于测试邮件')
    config=load_config()
    if kind=='testfairy':
        task_id=body.get('task_id','')
        if not isinstance(task_id,str) or not re.fullmatch(r'\d{12}-[0-9a-f]{6}',task_id):
            raise ValueError('请选择已保存 GMX Job URL 和 Build 编号的历史任务')
        from .store import task,read_state
        row=task(task_id);params=json.loads(row['params']);runtime=read_state(task_id).get('runtime',{})
        try:
            source=source_build(params,runtime)
        except (NeedsAttention,KeyError) as exc:
            raise ValueError('所选任务的匹配信息不足：'+safe_text(str(exc))) from None
        wanted=body.get('expected_share_url','')
        if not isinstance(wanted,str):raise ValueError('预期 APP 链接必须是文本')
        try:
            wanted=validate_join_url(wanted.strip()) if wanted.strip() else ''
        except NeedsAttention as exc:
            raise ValueError(str(exc)) from None
        inputs={'task_id':task_id,'source':source,'wanted':wanted};secret=None
    else:
        if body.get('confirm_send') is not True:raise ValueError('请明确点击发送测试邮件')
        to=recipients(body.get('recipients',''));account=body.get('account','').strip()
        if not to or (account and not valid_address(account)):raise ValueError('请填写有效测试收件人和发件账号')
        task_id=body.get('task_id','')
        if not isinstance(task_id,str) or not re.fullmatch(r'\d{12}-[0-9a-f]{6}',task_id):
            raise ValueError('请选择用于测试报告的历史构建')
        from .store import task,read_state
        from .outlook import outcome_preview,read_log_tail
        row=task(task_id);params=json.loads(row['params'])
        preview=outcome_preview(row,params,read_state(task_id),read_log_tail(task_id))
        preview.update(subject='[APP Builder TEST] '+preview['subject'],to=to,account=account)
        preview['body']='这是您主动发送的历史构建测试报告，不代表重新构建或正式通知。\n\n'+preview['body']
        inputs={'to':to,'account':account,'task_id':task_id};secret=None
    fingerprint=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    now=time.time()
    with _LOCK,closing(connect()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM tests WHERE id=?',(test_id,)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint or existing['kind']!=kind:raise ValueError('测试编号已用于不同输入，禁止重复提交')
            return {'id':test_id}
        uncertain_ids=[]
        if kind=='outlook':
            # Include older fixed-content tests: changing task must not silently replay an uncertain send.
            for previous in db.execute("SELECT * FROM tests WHERE kind='outlook' AND status IN ('RUNNING','SUBMITTING','UNKNOWN')"):
                result=json.loads(previous['result'])
                same_recipients={address.lower() for address in result.get('to',[])}=={address.lower() for address in inputs['to']}
                same_sender=not inputs['account'] or not result.get('account') or result['account'].lower()==inputs['account'].lower()
                if previous['fingerprint']==fingerprint or (same_recipients and same_sender):
                    if previous['status'] in ('RUNNING','SUBMITTING') and (previous['id'] in _ACTIVE or now-previous['created']<900):
                        raise ValueError('同一目标的测试邮件正在执行；允许重复测试也不能并发发送')
                    uncertain_ids.append(previous['id'])
        if uncertain_ids and not allow_repeat:
            raise ValueError('同一目标已有结果不明的测试邮件，请先核对 Outlook 发件箱/已发送；再次发送须明确开启允许重复测试')
        recent=db.execute('SELECT id,status,created FROM tests WHERE kind=? ORDER BY created DESC LIMIT 1',(kind,)).fetchone()
        if recent and (now-recent['created']<30 or (recent['status'] in ('RUNNING','SUBMITTING') and (recent['id'] in _ACTIVE or now-recent['created']<900))):
            raise ValueError('同类测试正在执行或刚执行过，请等待至少 30 秒后再试')
        initial=({'to':inputs['to'],'account':inputs['account'],'task_id':inputs['task_id'],
                  'allow_repeat_test':allow_repeat,'repeated_unknown_ids':uncertain_ids,'requested_at':now}
                 if kind=='outlook' else {})
        db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',(test_id,kind,fingerprint,now,'RUNNING',json.dumps(initial)))
        _ACTIVE.add(test_id)
    def execute():
        try:
            if kind=='testfairy':
                from .app_links import lookup_task_app
                notes=[]
                # The test always tries the saved Jenkins API token first, even for tasks
                # created before the token switch existed; the browser is the fallback.
                app=lookup_task_app(config,DATA,DATA/'delivery-tests'/test_id,params,runtime,visible=True,
                                    emit=notes.append,prefer_token=True)
                actual=app['share_url']
                if app.get('transport')=='jenkins-api-token':
                    transport='方式：Jenkins API Token（未打开浏览器）'
                else:
                    reasons=[n.replace('[APP-LINK] ','') for n in notes if '改用浏览器' in n]
                    transport='方式：浏览器'+('（'+safe_text(reasons[-1])+'）' if reasons else '')
                matched=actual==inputs['wanted'] if inputs['wanted'] else None
                status=('MATCH' if matched else 'MISMATCH') if matched is not None else 'EXTRACTED'
                finish(test_id,status,{
                    'message':'已从指定 GMX 构建的 Jenkins 完整日志提取 APP 链接'+
                              ('，预期链接'+('匹配' if matched else '不匹配') if matched is not None else '')+
                              '；未启动打包\n'+transport,
                    'transport':app.get('transport') or 'browser',
                    'expected_share_url':inputs['wanted'],'actual_share_url':actual,
                    'task_id':inputs['task_id'],'build_url':app['build_url'],
                    'source_job_url':app['source_job_url'],'source_build_number':app['source_build_number'],
                    'source_build_url':app['source_build_url'],
                    'downstream_job_url':app.get('downstream_job_url'),
                    'downstream_build_number':app.get('downstream_build_number'),
                    'downstream_build_url':app.get('downstream_build_url'),
                    'app':app,
                    'link_semantics':'已确认来源 GMX 与 upload-to-testfairy 构建；链接是否固定历史版本尚未独立验证',
                    'diagnostics':testfairy_diagnostics(app.get('diagnostics'),secret)})
            else:
                from .outlook import OutlookAdapter,call_diagnostic
                adapter=OutlookAdapter();account=adapter.resolve(inputs['account'])
                finish(test_id,'SUBMITTING',{'account':account['email'],'to':inputs['to'],'message':'正在提交测试邮件，尚未确认结果'})
                try:
                    result=adapter.submit(preview,account)
                    if result.get('status')!='SUBMITTED':raise NeedsAttention('unknown')
                except BaseException as exc:
                    not_sent=getattr(exc,'not_submitted',False)
                    finish(test_id,'BLOCKED' if not_sent else 'UNKNOWN',{'message':
                        ('未调用发送，请修复后手动重试。' if not_sent else '发送结果不明，请核对 Outlook 发件箱/已发送；不会自动重试。')+safe_text(str(exc)),
                        **call_diagnostic(exc),'account':account['email'],'to':inputs['to'],'task_id':inputs['task_id']});return
                finish(test_id,'SUBMITTED',{**result,'message':'已提交本机 Outlook；尚未验证服务器发出或收件人送达','account':account['email'],'to':inputs['to']})
        except (NeedsAttention,ValueError) as exc:
            diagnostic={}
            if kind=='outlook' and hasattr(exc,'stage'):
                from .outlook import call_diagnostic
                diagnostic=call_diagnostic(exc)
            elif kind=='testfairy':
                diagnostic={'diagnostics':testfairy_diagnostics(getattr(exc,'testfairy_diagnostics',None),secret)}
            message=(testfairy_diagnostics({'first_error':{'message':str(exc)}},secret)['first_error'].get('message','测试未完成')
                     if kind=='testfairy' and str(exc) else safe_text(str(exc)))
            finish(test_id,'BLOCKED',{'message':message,**diagnostic})
        except Exception as exc:
            diagnostic=({'diagnostics':testfairy_diagnostics(getattr(exc,'testfairy_diagnostics',None),secret)}
                        if kind=='testfairy' else {})
            finish(test_id,'ERROR',{'message':'测试未完成；请检查本机浏览器/Outlook。不会记录密码或自动重发',**diagnostic})
        finally:
            with _LOCK:_ACTIVE.discard(test_id)
    try:threading.Thread(target=execute,daemon=True).start()
    except Exception:
        _ACTIVE.discard(test_id);finish(test_id,'ERROR',{'message':'无法启动测试'});raise
    return {'id':test_id}

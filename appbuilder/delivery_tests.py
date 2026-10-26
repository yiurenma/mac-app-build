"""Explicit, independent user-triggered App-link tests. No build tasks."""
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
        status='INTERRUPTED'
    return {'id':test_id,'kind':row['kind'],'status':status,'result':result}


@lifecycle.admitted
def start(body):
    from .jenkins_links import source_build,validate_join_url
    kind=body.get('kind');test_id=body.get('request_id','')
    if kind!='testfairy' or not re.fullmatch(r'[a-f0-9-]{16,64}',test_id):raise ValueError('测试类型或请求编号无效')
    config=load_config()
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
    fingerprint=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    now=time.time()
    with _LOCK,closing(connect()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM tests WHERE id=?',(test_id,)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint or existing['kind']!=kind:raise ValueError('测试编号已用于不同输入，禁止重复提交')
            return {'id':test_id}
        recent=db.execute('SELECT id,status,created FROM tests WHERE kind=? ORDER BY created DESC LIMIT 1',(kind,)).fetchone()
        if recent and (now-recent['created']<30 or (recent['status'] in ('RUNNING','SUBMITTING') and (recent['id'] in _ACTIVE or now-recent['created']<900))):
            raise ValueError('同类测试正在执行或刚执行过，请等待至少 30 秒后再试')
        db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',(test_id,kind,fingerprint,now,'RUNNING',json.dumps({})))
        _ACTIVE.add(test_id)
    def execute():
        try:
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
        except (NeedsAttention,ValueError) as exc:
            diagnostic={'diagnostics':testfairy_diagnostics(getattr(exc,'testfairy_diagnostics',None),secret)}
            message=(testfairy_diagnostics({'first_error':{'message':str(exc)}},secret)['first_error'].get('message','测试未完成')
                     if str(exc) else '测试未完成')
            finish(test_id,'BLOCKED',{'message':message,**diagnostic})
        except Exception as exc:
            diagnostic={'diagnostics':testfairy_diagnostics(getattr(exc,'testfairy_diagnostics',None),secret)}
            finish(test_id,'ERROR',{'message':'测试未完成；请检查本机浏览器。不会记录密码',**diagnostic})
        finally:
            with _LOCK:_ACTIVE.discard(test_id)
    try:threading.Thread(target=execute,daemon=True).start()
    except Exception:
        _ACTIVE.discard(test_id);finish(test_id,'ERROR',{'message':'无法启动测试'});raise
    return {'id':test_id}

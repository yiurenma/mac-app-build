"""Explicit, independent user-triggered connection/email tests. No build tasks."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import urlsplit,urlunsplit
from .config import DATA,load_config
from .jenkins import NeedsAttention
from .telemetry import safe_text

_LOCK=threading.Lock()
_ACTIVE=set()


def connect():
    DATA.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA/'delivery-tests.sqlite',timeout=20);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS tests (id TEXT PRIMARY KEY,kind TEXT,fingerprint TEXT,created REAL,status TEXT,result TEXT)')
    db.commit();return db


def finish(test_id,status,result):
    with closing(connect()) as db,db:
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


def start(body):
    from .testfairy import approved_target,DEFAULT_LOGIN,expected_build,share_url
    from .outlook import recipients,valid_address
    kind=body.get('kind');test_id=body.get('request_id','')
    if kind not in ('testfairy','outlook') or not re.fullmatch(r'[a-f0-9-]{16,64}',test_id):raise ValueError('测试类型或请求编号无效')
    config=load_config()
    if kind=='testfairy':
        login=body.get('url') or DEFAULT_LOGIN;approved_target(login,config)
        user=body.get('username','');password=body.get('password','')
        if not isinstance(user,str) or not isinstance(password,str) or not user.strip() or not password or max(len(user),len(password))>256:
            raise ValueError('TestFairy 测试须填写账号密码，各不超过 256 字符')
        task_id=body.get('task_id','')
        if not re.fullmatch(r'\d{12}-[0-9a-f]{6}',task_id):
            raise ValueError('请选择已保存 GMX 构建信息的历史任务；需 GMX Job URL、Build 编号、SHA 和上传目录')
        from .store import task,read_state
        row=task(task_id);params=json.loads(row['params'])
        try:
            expected=expected_build(params,read_state(task_id).get('runtime',{}))
        except (NeedsAttention,KeyError) as exc:
            raise ValueError('所选任务的匹配信息不足：'+safe_text(str(exc))) from None
        wanted=body.get('expected_share_url','')
        if not isinstance(wanted,str) or not wanted.strip():raise ValueError('请输入预期 App /join/ 分享链接（不是登录入口）')
        wanted=share_url(wanted.strip(),approved_target(login,config))
        inputs={'url':login,'task_id':task_id,'expected':expected,'wanted':wanted};secret=(user.strip(),password)
    else:
        if body.get('confirm_send') is not True:raise ValueError('请明确点击发送测试邮件')
        to=recipients(body.get('recipients',''));account=body.get('account','').strip()
        if not to or (account and not valid_address(account)):raise ValueError('请填写有效测试收件人和发件账号')
        inputs={'to':to,'account':account};secret=None
    fingerprint=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    now=time.time()
    with _LOCK,closing(connect()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM tests WHERE id=?',(test_id,)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint or existing['kind']!=kind:raise ValueError('测试编号已用于不同输入，禁止重复提交')
            return {'id':test_id}
        uncertain=db.execute("SELECT id FROM tests WHERE kind=? AND fingerprint=? AND status IN ('SUBMITTING','UNKNOWN') LIMIT 1",(kind,fingerprint)).fetchone()
        if kind=='outlook' and uncertain:
            raise ValueError('同一目标已有结果不明的测试邮件，请先核对 Outlook 发件箱/已发送；禁止盲目重发')
        recent=db.execute('SELECT id,status,created FROM tests WHERE kind=? ORDER BY created DESC LIMIT 1',(kind,)).fetchone()
        if recent and (now-recent['created']<30 or (recent['status'] in ('RUNNING','SUBMITTING') and (recent['id'] in _ACTIVE or now-recent['created']<900))):
            raise ValueError('同类测试正在执行或刚执行过，请等待至少 30 秒后再试')
        db.execute('INSERT INTO tests VALUES(?,?,?,?,?,?)',(test_id,kind,fingerprint,now,'RUNNING','{}'))
        _ACTIVE.add(test_id)
    def execute():
        try:
            if kind=='testfairy':
                from .testfairy import TestFairyBrowser
                options={'login_url':inputs['url'],'save_session':False}
                test_config={**config,'testfairy':{**config.get('testfairy',{}),'wait_seconds':min(60,float(config.get('testfairy',{}).get('wait_seconds',60)))}}
                with TestFairyBrowser(test_config,DATA,options,login_credentials=secret,visible=True) as browser:
                    app=browser.find(inputs['expected'])
                    actual=share_url(app['share_url'],approved_target(inputs['url'],config))
                    matched=actual==inputs['wanted']
                    u=urlsplit(browser.page.url)
                    final=urlunsplit((u.scheme,u.netloc,u.path,'',''))
                    finish(test_id,'MATCH' if matched else 'MISMATCH',{
                        'message':'已按保存的 GMX 构建提取已有分享链接，结果'+('匹配' if matched else '不匹配')+'；未启动打包或更改分享权限',
                        'requested_url':inputs['url'],'final_url':final,'expected_share_url':inputs['wanted'],
                        'actual_share_url':actual,'task_id':inputs['task_id'],'build_url':app['build_url'],'app_version':app['app_version']})
            else:
                from .outlook import OutlookAdapter
                adapter=OutlookAdapter();account=adapter.resolve(inputs['account'])
                finish(test_id,'SUBMITTING',{'account':account['email'],'to':inputs['to'],'message':'正在提交测试邮件，尚未确认结果'})
                preview={'subject':'[APP Builder TEST] Outlook 邮件连通测试 '+test_id[:8],
                         'body':'这是一封由您点击发送的 APP Builder 测试邮件。不包含构建资料、App 链接或登录凭据。请求编号：'+test_id,
                         'to':inputs['to']}
                try:
                    result=adapter.submit(preview,account)
                    if result.get('status')!='SUBMITTED':raise NeedsAttention('unknown')
                except BaseException:
                    finish(test_id,'UNKNOWN',{'message':'发送结果不明，请核对 Outlook 发件箱/已发送；不会自动重试','account':account['email'],'to':inputs['to']});return
                finish(test_id,'SUBMITTED',{**result,'message':'已提交本机 Outlook；尚未验证服务器发出或收件人送达','account':account['email'],'to':inputs['to']})
        except (NeedsAttention,ValueError) as exc:
            finish(test_id,'BLOCKED',{'message':safe_text(str(exc))})
        except Exception:
            finish(test_id,'ERROR',{'message':'测试未完成；请检查本机浏览器/Outlook。不会记录密码或自动重发'})
        finally:
            with _LOCK:_ACTIVE.discard(test_id)
    try:threading.Thread(target=execute,daemon=True).start()
    except Exception:
        _ACTIVE.discard(test_id);finish(test_id,'ERROR',{'message':'无法启动测试'});raise
    return {'id':test_id}

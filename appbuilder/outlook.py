"""Optional native Outlook delivery, fail-closed on unsupported Mac interfaces.

No SMTP/OAuth credentials. A submitted request is not proof of remote delivery.
"""
from __future__ import annotations
import json
import errno
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from .jenkins import NeedsAttention

EMAIL=re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")


def valid_address(value):
    if not isinstance(value,str) or len(value)>254 or not EMAIL.fullmatch(value):return False
    local,domain=value.rsplit('@',1)
    return (len(local)<=64 and not local.startswith('.') and not local.endswith('.') and '..' not in local
            and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label) for label in domain.split('.')))


def recipients(value):
    if not isinstance(value,str) or len(value)>4000 or '\r' in value or '\n' in value:
        raise ValueError('收件人须为邮箱地址，以英文逗号或分号分隔，不含换行')
    parts=[x.strip() for x in re.split(r'[,;]',value) if x.strip()]
    if len(parts)>50 or any(not valid_address(x) for x in parts):
        raise ValueError('收件人邮箱格式无效，最多 50 个；不要填写昵称或显示名')
    return list(dict.fromkeys(parts))


def preferences(body):
    enabled=body.get('outlook_auto_send',False)
    if not isinstance(enabled,bool):raise ValueError('Outlook 自动发送必须是开关值')
    to=recipients(body.get('outlook_recipients',''))
    account=body.get('outlook_account','').strip()
    if account and not valid_address(account):raise ValueError('Outlook 发件账号必须是完整邮箱地址')
    if enabled and not to:
        raise ValueError('自动发送需填写明确收件人')
    return {'enabled':enabled,'to':to,'account':account}


PROBE_JS='''function run(argv) {
 let stage='probe_application';
 try {
  const app=Application('Microsoft Outlook');
  stage='probe_version';const version=app.version();
  const accounts=[], collections=[];
  for(const type of ['exchangeAccounts','imapAccounts','popAccounts']) {
   stage='probe_'+type;const found=app[type]();
   if(!Array.isArray(found))throw Error('Account collection is not an array');
   collections.push({type:type,count:found.length});
   found.forEach(a=>{
    const record={type:type,field_errors:[]};
    for(const [key,property] of [['email','emailAddress'],['id','id']]) {
     try {const value=a[property]();record[key]=value==null?null:String(value);}
     catch(e){record[key]=null;record.field_errors.push({field:key,error_number:e.errorNumber||null});}
    }
    accounts.push(record);
   });
  }
  return JSON.stringify({status:'OK',version:version,accounts:accounts,collections:collections});
 } catch(e) {
  return JSON.stringify({status:'ERROR',stage:stage,send_attempted:false,error:String(e),error_number:e.errorNumber||null});
 }
}'''
SEND_JS='''ObjC.import('Foundation');
function run(argv) {
 let stage='read_payload', id='', send_attempted=false;
 try {
  const raw=$.NSString.stringWithContentsOfFileEncodingError(argv[0],$.NSUTF8StringEncoding,null);
  const p=JSON.parse(ObjC.unwrap(raw));
  const app=Application('Microsoft Outlook');
  stage='resolve_account';
  const accounts=app.exchangeAccounts().concat(app.imapAccounts(),app.popAccounts());
  const matches=accounts.filter(a=>{
   const email=a.emailAddress();
   if(typeof email!=='string'||!email.trim())throw Error('Unreadable account identity; no message created');
   if(email.trim().toLowerCase()!==p.account.toLowerCase())return false;
   const id=a.id();if(id==null||String(id)!==p.account_id)throw Error('Matching account ID changed; no message created');
   return true;
  });
  if(matches.length!==1)throw Error('Account changed; no message created');
  stage='create_message';
  const msg=app.OutgoingMessage({subject:p.subject,plainTextContent:p.body});
  app.outgoingMessages.push(msg);
  stage='set_account';
  msg.account=matches[0];
  stage='set_recipients';
  p.to.forEach(address=>msg.toRecipients.push(app.ToRecipient({emailAddress:{address:address}})));
  stage='message_id';id=String(msg.id());
  if(!id||id==='undefined'||id==='null')throw Error('Outlook did not return a message ID');
  stage='send';send_attempted=true;app.send(msg);
  stage='response';return JSON.stringify({message_id:id,status:'SUBMITTED'});
 } catch(e) {
  return JSON.stringify({status:'ERROR',stage:stage,send_attempted:send_attempted,message_id:id,error:String(e),error_number:e.errorNumber||null});
 }
}'''


class OutlookCallError(NeedsAttention):
    def __init__(self, stage, detail, *, not_submitted=False, returncode=None, error_number=None):
        from .telemetry import safe_text
        self.stage=stage
        self.not_submitted=not_submitted
        self.returncode=returncode
        self.error_number=error_number if isinstance(error_number,int) else None
        self.send_attempted=False if not_submitted else (True if stage=='send' else None)
        super().__init__('Outlook 阶段 '+stage+'：'+safe_text(str(detail))[:800])



class OutlookAdapter:
    def __init__(self, app_path='/Applications/Microsoft Outlook.app'):
        self.app_path=app_path
    def _dictionary(self):
        if sys.platform!='darwin':raise NeedsAttention('本机不是 macOS，无法验证或调用本地 Outlook；邮件预览已保留')
        if not Path(self.app_path).is_dir():raise NeedsAttention('未找到本地 Microsoft Outlook，邮件预览已保留')
        try:
            result=subprocess.run(['/usr/bin/sdef',self.app_path],capture_output=True,text=True,timeout=20)
            if result.returncode:raise ValueError()
            root=ET.fromstring(result.stdout)
            names={node.get('name') for node in root.iter()}
            required={'outgoing message','to recipient','exchange account','imap account','pop account','send','email address','plain text content','account'}
            if not required<=names:raise ValueError()
        except (OSError,ValueError,ET.ParseError,subprocess.TimeoutExpired):
            raise NeedsAttention('当前 Outlook 未提供所需原生脚本接口；不保证新版 Outlook 支持，保留预览且不发送') from None
    def _script(self,source,args=()):
        probe_only=source==PROBE_JS
        try:
            result=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',source,*args],capture_output=True,text=True,timeout=60)
        except subprocess.TimeoutExpired:
            if probe_only:
                raise OutlookCallError('probe_timeout','只读账号检查超过 60 秒；未创建或发送邮件',not_submitted=True) from None
            raise OutlookCallError('timeout','原生调用超过 60 秒；发送可能已开始，请核对发件箱/已发送') from None
        except OSError as exc:
            raise OutlookCallError('probe_invoke' if probe_only else 'invoke',exc,
                not_submitted=probe_only or exc.errno in (errno.ENOENT,errno.EACCES,errno.ENOEXEC)) from None
        if result.returncode:
            raise OutlookCallError('probe_native_call' if probe_only else 'native_call',
                ('只读账号检查失败，未创建或发送邮件；' if probe_only else '')+
                f'退出代码 {result.returncode}: '+(result.stderr or '无错误输出'),
                not_submitted=probe_only,returncode=result.returncode)
        try:
            payload=json.loads(result.stdout)
            if not isinstance(payload,dict):raise ValueError()
        except (ValueError,TypeError):
            if probe_only:
                raise OutlookCallError('probe_response_parse','只读账号检查返回不是有效 JSON 对象；未创建或发送邮件，不回显原始内容',not_submitted=True) from None
            raise OutlookCallError('response_parse','原生返回不是有效 JSON 对象；不回显原始内容，发送结果需人工核对') from None
        if payload.get('status')=='ERROR':
            stage=payload.get('stage','native_call')
            probe_stages={'probe_application','probe_version','probe_exchangeAccounts','probe_imapAccounts','probe_popAccounts'}
            if stage not in {'read_payload','resolve_account','create_message','set_account','set_recipients','message_id','send','response'}|probe_stages:stage='native_call'
            raise OutlookCallError(stage,payload.get('error','未知原生错误'),
                not_submitted=payload.get('send_attempted') is False and stage in ({
                    'read_payload','resolve_account','create_message','set_account','set_recipients','message_id'}|probe_stages),error_number=payload.get('error_number'))
        return payload
    def probe(self):
        self._dictionary()
        result=self._script(PROBE_JS)
        accounts=result.get('accounts')
        if not isinstance(accounts,list):
            raise OutlookCallError('probe_response','账号集合缺失或不是列表；不能据此判断登录状态或 Outlook 模式',not_submitted=True)
        if not accounts:
            raise OutlookCallError('probe_accounts','原生接口返回 0 个账号；请检查 Outlook 当前账户及原生脚本支持，不能据此断言未登录',not_submitted=True)
        valid=[];invalid=[];unverified=[]
        for index,account in enumerate(accounts):
            fields=[]
            if not isinstance(account,dict):
                invalid.append({'index':index,'fields':['record']});unverified.append(None);continue
            email=account.get('email');email=email.strip() if isinstance(email,str) else email
            identifier=account.get('id')
            if not valid_address(email):fields.append('email')
            if (not isinstance(identifier,(str,int)) or isinstance(identifier,bool)
                    or str(identifier).strip().lower() in ('','0','none','null','undefined','nan')):fields.append('id')
            if fields:
                invalid.append({'index':index,'fields':fields})
                unverified.append(email if valid_address(email) else None)
            else:valid.append({**account,'email':email,'id':str(identifier)})
        diagnostics={'returned':len(accounts),'valid':len(valid),'invalid':invalid}
        if not valid:
            raise OutlookCallError('probe_accounts','账号校验失败：'+json.dumps(diagnostics,ensure_ascii=False),not_submitted=True)
        return {**result,'accounts':valid,'account_diagnostics':diagnostics,'unverified_account_emails':unverified}
    def resolve(self,requested=''):
        result=self.probe();accounts=result['accounts']
        if not requested and result.get('account_diagnostics',{}).get('invalid'):
            raise OutlookCallError('resolve_account','账号列表包含不完整记录，无法证明唯一发件账号；请明确填写目标公司邮箱。'+json.dumps(result['account_diagnostics'],ensure_ascii=False),not_submitted=True)
        if requested and any(email is None or email.lower()==requested.lower() for email in result.get('unverified_account_emails',[])):
            raise OutlookCallError('resolve_account','不完整账号可能与目标邮箱重复，无法唯一核验；请检查 Outlook 账号接口。'+json.dumps(result['account_diagnostics'],ensure_ascii=False),not_submitted=True)
        choices=[a for a in accounts if a['email'].lower()==requested.lower()] if requested else accounts
        if len(choices)!=1:raise NeedsAttention('Outlook 发件账号不唯一，请在表单明确填写已登录公司邮箱')
        return choices[0]
    def submit(self,preview,account):
        payload={**preview,'account':account['email'],'account_id':account['id']}
        try:
            fd,path=tempfile.mkstemp(prefix='app-builder-mail-',suffix='.json')
        except OSError as exc:
            raise OutlookCallError('prepare_payload',exc,not_submitted=True) from None
        try:
            try:
                with os.fdopen(fd,'w') as f:json.dump(payload,f,ensure_ascii=False)
                os.chmod(path,0o600)
            except (OSError,ValueError,TypeError) as exc:
                raise OutlookCallError('prepare_payload',exc,not_submitted=True) from None
            result=self._script(SEND_JS,(path,))
            if result.get('status')!='SUBMITTED' or not result.get('message_id'):
                raise OutlookCallError('response','Outlook 未返回邮件编号，发送结果不明')
            return result
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass  # cleanup must never turn a confirmed submission into a retryable failure


def outcome_preview(row, params, state, log_text=''):
    from .telemetry import safe_text
    from .testfairy import mail_preview,expected_build
    runtime=state.get('runtime',{})
    from .reuse_flow import effective_params
    params=effective_params(params,runtime)
    status=row['status'];stage=row['stage']
    app=runtime.get('testfairy_app')
    detail='';verified_share_url=None
    if status=='SUCCESS' and app:
        try:
            expected=expected_build(params,runtime)
            if all(app.get(k)==expected[k] for k in ('build_url','number','sha','folder')):
                detail=mail_preview(params,runtime,row['created'])['body']
                from .jenkins_links import validate_join_url
                candidate=validate_join_url(app['share_url'])
                # Keep only the verified public join URL intact; all other
                # message text still goes through the unchanged log scrubber.
                masked=candidate.replace('https://','APP_LINK_SCHEME:',1)
                if safe_text(masked)==masked:
                    verified_share_url=candidate
        except (NeedsAttention,KeyError,TypeError):
            pass  # Corrupt or incomplete old metadata cannot authorize a link.
    if not detail:
        details=[('国家/地区',params.get('country','')),('项目',params.get('project','')),
            ('Plugin 来源',params.get('plugin_source','')),('Plugin 构建分支',params.get('plugin_branch','')),
            ('GMX 来源',params.get('gmx_source','')),('GMX 构建分支',params.get('gmx_branch','')),
            ('Plugin Job',params.get('plugin_job','')),('Plugin 正式编号',runtime.get('plugin_formal_number','')),
            ('实际产物版本',runtime.get('artifact_version','')),('GMX Job',params.get('gmx_job','')),
            ('GMX Build 编号',runtime.get('gmx_build_number',runtime.get('gmx_formal_number',''))),
            ('GMX SHA',runtime.get('gmx_sha','')),('创建时间',row['created'])]
        detail='\n'.join(f'{k}: {v}' for k,v in details)+'\nTestFairy 链接: 未提供（尚未完成精确匹配或任务未成功）'
    reason=safe_text(row.get('note',''))
    safe_lines=safe_text(log_text).splitlines()
    relevant=[line for line in safe_lines if re.search(r'\[(?:STEP|DONE|FAILED|NEEDS_ATTENTION|TIMEOUT|JENKINS|ARTIFACT|LOCK|GMX)\]',line)]
    summary='\n'.join(relevant[-12:])[-6000:] or '未记录可用于邮件的步骤/错误摘要'
    body=(f"任务: {row['id']}\n实际状态: {status}\n当前阶段: {stage}\n"
          f"已完成: {', '.join(state.get('completed',[])) or '无'}\n原因: {reason}\n"
          f"需要处理: {'否（邮件提交不代表送达）' if status=='SUCCESS' else '是，请查看原任务并按失败原因处理；不要重新 Start build'}\n\n"
          +detail+'\n\n脱敏日志摘要:\n'+summary)
    from .reporting import render_steps
    body += '\n\n'+render_steps(params,state,row)
    if verified_share_url:
        import uuid
        placeholder='APP_LINK_'+uuid.uuid4().hex
        body=safe_text(body.replace(verified_share_url,placeholder)).replace(placeholder,verified_share_url)
    else:
        body=safe_text(body)
    return {'subject':f"[{status}] {params.get('country','')} {params.get('project','')} · {row['id']}",
            'body':body,'to':params.get('outlook',{}).get('to',[]),
            'account':params.get('outlook',{}).get('account',''),'status':'PREVIEW_ONLY','mode':'preview',
            'task_id':row['id']}


def call_diagnostic(exc):
    return {'stage':getattr(exc,'stage','submit'), 'send_attempted':getattr(exc,'send_attempted',None),
            'returncode':getattr(exc,'returncode',None), 'error_number':getattr(exc,'error_number',None)}


def read_log_tail(task_id):
    from .store import task_folder
    path=task_folder(task_id)/'run.log'
    try:
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-16000))
            return stream.read().decode('utf-8',errors='replace')
    except OSError:
        return ''


def notify_outcome(task_id, adapter=None):
    import hashlib
    from .store import read_state,patch_runtime,task
    row=task(task_id)
    if row['status'] not in ('SUCCESS','FAILED','NEEDS_ATTENTION','TIMEOUT','INTERRUPTED'):return
    params=json.loads(row['params']);options=params.get('outlook',{})
    if not options.get('enabled') and not params.get('testfairy',{}).get('enabled'):return
    state=read_state(task_id);runtime=state.get('runtime',{})
    preview=outcome_preview(row,params,state,read_log_tail(task_id))
    identity={'status':row['status'],'stage':row['stage'],
              'build':runtime.get('gmx_build_number',runtime.get('gmx_formal_number')),
              'link':runtime.get('testfairy_app',{}).get('share_url') if row['status']=='SUCCESS' else None}
    key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    ledger=runtime.get('mail_notifications',{})
    previous=ledger.get(key,{})
    patch_runtime(task_id,mail_preview=preview)
    if previous.get('status') in ('SUBMITTING','SUBMITTED','UNKNOWN'):
        patch_runtime(task_id,mail_delivery=previous);return
    if not options.get('enabled'):return
    adapter=adapter or OutlookAdapter()
    def record(value):
        ledger[key]=value
        patch_runtime(task_id,mail_notifications=ledger,mail_delivery=value)
    try:
        if not options.get('to'):raise NeedsAttention('未填写明确收件人')
        account=adapter.resolve(options.get('account',''))
    except Exception as exc:
        from .telemetry import safe_text
        record({'status':'BLOCKED','reason':safe_text(str(exc)),'outcome':row['status']});return
    base={'account':account['email'],'to':options['to'],'outcome':row['status'],'notification_key':key}
    record({**base,'status':'SUBMITTING'})
    try:
        result=adapter.submit({**preview,'to':options['to']},account)
        if result.get('status')!='SUBMITTED':raise ValueError('unconfirmed submission')
    except BaseException as exc:
        from .telemetry import safe_text
        not_sent=getattr(exc,'not_submitted',False)
        record({**base,'status':'BLOCKED' if not_sent else 'UNKNOWN','reason':
                ('未调用发送，请修复后手动重试。' if not_sent else '提交结果不明，请核对 Outlook 发件箱/已发送；禁止自动重发。')+safe_text(str(exc)),
                **call_diagnostic(exc)})
        return
    record({**base,**result})

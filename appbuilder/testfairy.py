"""Read-only TestFairy matching for the exact completed GMX build."""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin, parse_qsl, urlunsplit
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from .jenkins import NeedsAttention, browser_launch_options
from .auth import submit_recognized_login

DEFAULT_LOGIN = 'https://hsbc-mobilex.testfairy.com/login'


def origin(login):
    u = urlsplit(login)
    if (u.scheme != 'https' or not u.hostname or not u.hostname.endswith('.testfairy.com')
            or u.username or u.password or u.port not in (None,443)
            or u.query or u.fragment or u.path.rstrip('/') != '/login'):
        raise ValueError('TestFairy 登录地址必须是租户的 HTTPS /login，无账号密码或查询参数')
    return 'https://' + u.netloc


def approved_target(login, config):
    base=origin(login)
    allowed=config.get('testfairy',{}).get('allowed_hosts',['hsbc-mobilex.testfairy.com'])
    if urlsplit(base).hostname not in allowed:
        raise ValueError('TestFairy 目标未列入本机配置允许名单，禁止提交公司凭据')
    return base


def saved_auth_available(login, data):
    base=origin(login);host=urlsplit(base).hostname
    if (Path(data)/'auth'/('testfairy-'+host+'.json')).is_file():return True
    if sys.platform!='darwin':return False
    service='mac-app-builder.testfairy.'+host
    # Existence checks only: do not read password values to validate a schedule.
    for item in (service,service+'.account'):
        result=subprocess.run(['security','find-generic-password','-s',item],capture_output=True,timeout=10)
        if result.returncode:return False
    return True


def credentials(login, value=None):
    """Isolated opt-in Keychain entry. Never copy credentials into task metadata."""
    host = urlsplit(origin(login)).hostname
    service = 'mac-app-builder.testfairy.' + host
    if sys.platform != 'darwin':
        if value is not None:
            raise ValueError('TestFairy 保存凭据仅支持 Mac Keychain')
        return None
    def command(args):
        return subprocess.run(['security', *args],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if value is not None:
        user,password=value
        a=command(['add-generic-password','-U','-s',service,'-a',user,'-w',password])
        if a.returncode:raise ValueError('TestFairy Keychain 保存失败，请检查本机权限')
        b=command(['add-generic-password','-U','-s',service+'.account','-a','default','-w',user])
        if b.returncode:raise ValueError('TestFairy Keychain 账号索引保存失败')
        return None
    a=command(['find-generic-password','-s',service+'.account','-a','default','-w'])
    if a.returncode or not a.stdout.strip():return None
    user=a.stdout.strip()
    b=command(['find-generic-password','-s',service,'-a',user,'-w'])
    return (user,b.stdout.rstrip('\n')) if not b.returncode and b.stdout.rstrip('\n') else None


def settings(body, config=None):
    enabled=body.get('testfairy_enabled',False)
    persist=body.get('testfairy_save_credentials',False)
    if not isinstance(enabled,bool) or not isinstance(persist,bool):raise ValueError('TestFairy 开关必须是布尔值')
    login=body.get('testfairy_login_url') or DEFAULT_LOGIN
    approved_target(login, config or {})
    path=body.get('testfairy_folder_path','').strip()
    if path and (len(path)>512 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,127}',p) for p in path.split('/'))):
        raise ValueError('TestFairy 目录需使用逐层名称，以 / 分隔')
    return {'enabled':enabled,'login_url':login,'folder_path':path,'save_session':persist}


def expected_build(params, runtime):
    number=runtime.get('gmx_build_number') or runtime.get('gmx_formal_number')
    sha=runtime.get('gmx_sha','')
    if not isinstance(number,int) or isinstance(number,bool) or number<1 or not re.fullmatch(r'[0-9a-fA-F]{40}',sha):
        raise NeedsAttention('缺少当前 GMX Build 编号或完整提交 SHA，禁止猜测 TestFairy App')
    root=params['gmx_job'].rstrip('/')+'/'
    u=urlsplit(root)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise NeedsAttention('保存的 GMX Job URL 无效')
    gmx=params.get('gmx_parameters',{})
    folder=gmx.get('CUSTOM_FOLDER') or f"0_A_{params['country']}_{params['project']}"
    path=params.get('testfairy',{}).get('folder_path') or f"IOS_Upload/IOS_{gmx.get('BUILD_TYPE','Develop')}/{folder}"
    return {'build_url':root+str(number)+'/', 'number':number,'sha':sha.lower(),'folder':folder,'path':path}


def parse_candidate(text, expected):
    """All identity evidence must occur in the same App card/release notes."""
    urls=re.findall(r'https://[^\s<>"\']+',text)
    target=expected['build_url'].rstrip('/')
    if target not in [x.rstrip('/.,;)') for x in urls]:return None
    def field(label):
        m=re.search(r'(?im)^\s*'+label+r'\s*[:：=]?\s*([^\n\r]+)',text)
        return m.group(1).strip() if m else ''
    if field(r'Build\s*number')!=str(expected['number']):return None
    if field(r'(?:Commit\s*SHA|CommitSHA)') .lower()!=expected['sha']:return None
    if field(r'Folder\s*Param')!=expected['folder']:return None
    versions=set(re.findall(r'(?<![\w.])(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\s*\((\d+)\)',text))
    if len(versions)!=1:raise NeedsAttention('匹配的 TestFairy App 版本缺失或不唯一，不能生成邮件')
    version,build=next(iter(versions))
    return {'app_version':version,'app_build':build,**expected}


def share_url(raw, base):
    u=urlsplit(urljoin(base,raw));expected=urlsplit(base)
    if (u.scheme.lower()!='https' or (u.hostname or '').lower()!=(expected.hostname or '').lower()
            or u.port not in (None,443) or u.fragment or u.username or u.password
            or not re.fullmatch(r'/join/[A-Za-z0-9_-]+/?',u.path)):
        raise NeedsAttention('TestFairy 分享地址不是当前租户已有的 /join/ 链接')
    if any(re.search(r'token|password|secret|session|auth|key',k,re.I) for k,v in parse_qsl(u.query)):
        raise NeedsAttention('分享链接包含敏感认证查询参数，拒绝保存或展示')
    # Only normalize scheme/host/default port; path and query remain distinct.
    return urlunsplit(('https',u.hostname.lower(),u.path,u.query,''))


_FOLDER_SELECTOR = 'a, button, [role="treeitem"], [role="button"], [role="link"], [onclick], [data-folder-id], .folder-node, .folder-container .folder-card'
_FOLDER_CANDIDATES = r"""({label,parents,path,selector}) => {
  const norm=x=>(x||'').replace(/\s+/g,' ').trim();
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const crumbSelector='nav[aria-label*="readcrumb"], [role="navigation"][aria-label*="readcrumb"], .breadcrumb, .breadcrumbs, [data-breadcrumb]';
  const name=e=>{
    if(e.matches('.folder-container .folder-card')){
      const labels=Array.from(e.querySelectorAll('.app-name')).filter(visible);
      if(labels.length!==1)return '';
      const text=norm(labels[0].textContent),title=norm(labels[0].getAttribute('title'));
      return text&&(!title||title===text)?text:'';
    }
    if(e.getAttribute('aria-label'))return norm(e.getAttribute('aria-label'));
    const clone=e.cloneNode(true);
    clone.querySelectorAll('ul,ol,[role="group"]').forEach(n=>n.remove());
    return norm(clone.textContent);
  };
  const ancestors=e=>{
    const result=[];
    for(let p=e.parentElement;p;p=p.parentElement){if(p.matches('[role="treeitem"],li[data-folder-id],.folder-node'))result.unshift(name(p));}
    return result;
  };
  const containsPath=names=>{
    if(names[0]==='APPS')names=names.slice(1);
    return names.length>parents.length && [...parents,label].every((n,i)=>names[i]===n);
  };
  let current=false;
  document.querySelectorAll(crumbSelector).forEach(c=>{
    if(!visible(c))return;
    const names=Array.from(c.querySelectorAll('a,button,[aria-current],li,span'))
      .filter(e=>visible(e)&&!e.querySelector('a,button,[aria-current],li,span')).map(e=>norm(e.textContent));
    if(containsPath(names))current=true;
  });
  document.querySelectorAll('[role="treeitem"][aria-selected="true"], [role="treeitem"][aria-current="page"]').forEach(e=>{
    if(visible(e)&&containsPath([...ancestors(e),name(e)]))current=true;
  });
  const all=Array.from(document.querySelectorAll(selector));
  let candidates=all.map((e,index)=>({e,index})).filter(({e})=>visible(e)&&(!e.matches('.folder-card')||visible(e.closest('.folder-container')))&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&!e.closest(crumbSelector)&&name(e)===label);
  // A nested label link and its treeitem represent one node: prefer the interactive leaf.
  candidates=candidates.filter(({e})=>!candidates.some(x=>x.e!==e&&e.contains(x.e)));
  const scoped=candidates.filter(({e})=>{
    let names=ancestors(e);if(names[names.length-1]===label)names=names.slice(0,-1);
    if(names[0]==='APPS')names=names.slice(1);
    return names.length===parents.length&&names.every((n,i)=>n===parents[i]);
  });
  // A flat directory listing has no tree ancestors. A tree must match the full parent path.
  candidates=scoped.length?scoped:candidates.filter(({e})=>ancestors(e).length===0);
  return {current,indices:candidates.map(x=>x.index),summary:candidates.slice(0,8).map(({e})=>({tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',treeDepth:ancestors(e).length}))};
}"""



_FOLDER_STRUCTURE = r"""({label,selector}) => {
 const norm=x=>(x||'').replace(/\s+/g,' ').trim();
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const scope=document.querySelector('main,[role="main"]')||document.body;
 const roles=new Set(['button','link','treeitem','tree','list','listitem','grid','row','cell','navigation','main']);
 const tags=new Set(['a','button','div','span','li','ul','ol','main','nav','section','article','p','h1','h2','h3','svg','i']);
 const describe=e=>({tag:tags.has(e.localName)?e.localName:'other',role:roles.has(e.getAttribute('role'))?e.getAttribute('role'):'',
   href:e.hasAttribute('href'),onclick:e.hasAttribute('onclick'),tabindex:e.hasAttribute('tabindex'),
   folderSemantic:e.matches('[data-folder-id],.folder-node,[role="treeitem"]'),
   ariaLabel:e.hasAttribute('aria-label'),ariaLabelMatches:e.hasAttribute('aria-label')&&norm(e.getAttribute('aria-label'))===label,
   dataFolderId:e.hasAttribute('data-folder-id'),selectorMatch:e.matches(selector)});
 const matches=Array.from(scope.querySelectorAll('*')).filter(e=>visible(e)&&norm(e.textContent)===label&&!Array.from(e.children).some(c=>visible(c)&&norm(c.textContent)===label));
 return {scope:scope===document.body?'body':'main',exactVisibleLabels:matches.length,nodes:matches.slice(0,8).map(e=>{
   const ancestors=[];for(let p=e.parentElement;p&&ancestors.length<3;p=p.parentElement){ancestors.push(describe(p));if(p===scope)break;}
   const exclusionReasons=[];if(!e.matches(selector))exclusionReasons.push('outside_selector');
   if(e.hasAttribute('aria-label')&&norm(e.getAttribute('aria-label'))!==label)exclusionReasons.push('accessible_name_mismatch');
   if(e.disabled||e.getAttribute('aria-disabled')==='true')exclusionReasons.push('disabled');
   if(e.closest('nav[aria-label*="readcrumb"],.breadcrumb,.breadcrumbs,[data-breadcrumb]'))exclusionReasons.push('breadcrumb');
   return {node:describe(e),ancestors,exclusionReasons};
 })};
}"""

_LOADING_STATE = r"""() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const scope=document.querySelector('main,[role="main"]')||document.body;
 const busy=Array.from(scope.querySelectorAll('[aria-busy="true"],[role="progressbar"],progress')).some(visible)||scope.getAttribute('aria-busy')==='true';
 const alerts=Array.from(scope.querySelectorAll('[role="alert"]')).filter(visible).map(e=>e.innerText||'');
 const error=alerts.find(t=>/error|failed|unable|try again|失败|错误|无法|出错/i.test(t))||'';
 const ready=!busy&&(scope.getAttribute('aria-busy')==='false'||Array.from(scope.querySelectorAll('[role="tree"],[role="list"],[role="grid"],table,[aria-busy="false"]')).some(visible));
 return {busy,ready,error:error.slice(0,600)};
}"""


class TestFairyBrowser:
    def __init__(self, config, data, options, *, login_credentials=None, visible=True):
        self.config=config;self.options=options;self.base=approved_target(options['login_url'],config)
        self.auth_path=Path(data)/'auth'/('testfairy-'+urlsplit(self.base).hostname+'.json')
        self.login_credentials=login_credentials or credentials(options['login_url'])
        self.visible=visible
    def __enter__(self):
        self.engine=sync_playwright().start()
        try:
            self.browser=self.engine.chromium.launch(**browser_launch_options(self.config,headless=not self.visible))
            args={'accept_downloads':False}
            if self.options.get('save_session') and self.auth_path.exists():args['storage_state']=str(self.auth_path)
            self.context=self.browser.new_context(**args);self.page=self.context.new_page()
            self.page.set_default_timeout(12000)
            return self
        except Exception:
            if hasattr(self,'browser'):self.browser.close()
            self.engine.stop();raise
    def __exit__(self,*args):
        self.context.close();self.browser.close();self.engine.stop()
    def _same_origin(self):
        return urlsplit(self.page.url).netloc==urlsplit(self.base).netloc and urlsplit(self.page.url).scheme=='https'
    def login(self, expected):
        if self.options.get('save_session'):
            self.page.goto(self.base+'/my',wait_until='domcontentloaded')
            if (self._same_origin() and not self.page.locator('input[type=password]').count()
                    and self.page.get_by_text(expected['path'].split('/')[0],exact=True).count()):
                return
        self.page.goto(self.options['login_url'],wait_until='domcontentloaded')
        if not self._same_origin():raise NeedsAttention('TestFairy 登录跳转离开允许租户，未自动填写凭据')
        if self._same_origin() and self.page.locator('input[type=password]').count():
            if self.login_credentials:
                # Submit only same-origin TestFairy forms, never an IdP's password form.
                forms=self.page.locator('form:has(input[type=password])')
                if forms.count()!=1:raise NeedsAttention('TestFairy 登录表单不唯一')
                action=urljoin(self.page.url,forms.get_attribute('action') or self.page.url)
                if urlsplit(action).netloc!=urlsplit(self.base).netloc or urlsplit(action).scheme!='https':
                    raise NeedsAttention('TestFairy 登录提交地址不同源，停止自动填写')
                try:
                    submitted=submit_recognized_login(self.page,*self.login_credentials)
                except Exception:
                    raise NeedsAttention('TestFairy 登录填写或提交失败，凭据不记录到日志') from None
                if not submitted:
                    raise NeedsAttention('TestFairy 登录控件无法安全识别')
        deadline=time.monotonic()+(180 if self.visible else 25)
        opened_apps=False
        while time.monotonic()<deadline:
            if not self._same_origin():raise NeedsAttention('TestFairy 认证跳转离开允许租户，已停止')
            if (self._same_origin() and urlsplit(self.page.url).path.rstrip('/')!='/login'
                    and not self.page.locator('input[type=password]').count()):
                if not opened_apps:
                    self.page.goto(self.base+'/my',wait_until='domcontentloaded')
                    opened_apps=True
                if (self._same_origin() and not self.page.locator('input[type=password]').count()
                        and (self.page.get_by_text('APPS',exact=True).count()
                             or self.page.get_by_text(expected['path'].split('/')[0],exact=True).count())):
                    return
            self.page.wait_for_timeout(250)
        raise NeedsAttention('TestFairy 登录未完成；请在可见 Chrome 完成 MFA/额外验证，不会自动绕过')
    def _save_session(self):
        if not self.options.get('save_session'):return
        self.auth_path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=self.auth_path.parent,prefix='.testfairy-');os.close(fd)
        try:
            self.context.storage_state(path=name);os.chmod(name,0o600);os.replace(name,self.auth_path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def _lookup_deadline(self):
        return time.monotonic()+max(1,min(1800,float(self.config.get('testfairy',{}).get('wait_seconds',600))))

    def _observe_loading(self):
        if getattr(self,'_loading_observed',False):return
        self._loading_observed=True
        self._pending={};self._network_errors=[]
        def relevant(request):
            u=urlsplit(request.url);base=urlsplit(self.base)
            return u.scheme==base.scheme and u.netloc==base.netloc and request.resource_type in ('document','xhr','fetch')
        def request_started(request):
            if relevant(request):self._pending[id(request)]={'type':request.resource_type,'started':time.monotonic()}
        def response_received(response):
            if relevant(response.request) and response.status>=400:
                self._network_errors.append(f'{response.request.resource_type} HTTP {response.status}')
                self._network_errors[:]=self._network_errors[-8:]
        def finished(request):self._pending.pop(id(request),None)
        def failed(request):
            finished(request)
            if relevant(request):
                code=re.search(r'net::[A-Z0-9_]+',str(request.failure))
                self._network_errors.append(request.resource_type+' '+(code.group() if code else '请求失败'))
                self._network_errors[:]=self._network_errors[-8:]
        self.page.on('request',request_started)
        self.page.on('response',response_received)
        self.page.on('requestfinished',finished)
        self.page.on('requestfailed',failed)

    def _lookup_state(self):
        from .telemetry import safe_text
        if not self._same_origin():raise NeedsAttention('TestFairy 查找跳转离开允许租户，已停止')
        if self.page.locator('input[type="password"]:visible').count():
            raise NeedsAttention('TestFairy 会话已失效，停止查找，构建不会重跑')
        state=self.page.evaluate(_LOADING_STATE)
        if state['error']:
            raise NeedsAttention('TestFairy 页面明确加载错误：'+safe_text(state['error'])[:300])
        state['pending_requests']=len(self._pending)  # network activity alone is not directory loading evidence
        state['pending_structure']=[{'type':entry['type'],'age_seconds':round(max(0,time.monotonic()-entry['started']),1)}
            for entry in sorted(self._pending.values(),key=lambda entry:entry['started'])[:8]]
        return state

    def _lookup_failure(self,stage,state,evidence=None):
        detail='加载超时：仍观察到加载中' if state['busy'] else ('列表已就绪但未找到目标（空列表或目录未出现）' if state['ready'] else '等待超时：加载状态不明，尚未观察到目标')
        if evidence and len(evidence['indices'])>1:detail='目录存在多个可见交互候选，无法唯一定位'
        message='TestFairy '+stage+'：'+detail
        if evidence:
            message+='；可见交互候选 '+str(len(evidence['indices']))+'；候选结构 '+json.dumps(evidence['summary'],ensure_ascii=False)
        if evidence and evidence.get('structure') is not None:
            message+='；目标名称只读结构 '+json.dumps(evidence['structure'],ensure_ascii=False)+'（不据此自动点击未知控件）'
        if state.get('pending_requests'):
            message+='；未完成同源请求 '+str(state['pending_requests'])+'；请求类型/持续秒数 '+json.dumps(state.get('pending_structure',[]),ensure_ascii=False)+'（不能据此判断目录仍在加载；不采集请求地址或内容）'
        if self._network_errors:
            message+='；观测到同源请求错误 '+', '.join(self._network_errors)+'（未确认是否目录接口；不包含地址查询、请求体或响应内容）'
        raise NeedsAttention(message+'；未改分享权限或重新构建')

    def _lookup_action(self,action,deadline,**kwargs):
        if time.monotonic()>=deadline:
            self._lookup_failure('页面操作总等待预算已用尽',self._lookup_state())
        try:
            return action(timeout=max(1,int((deadline-time.monotonic())*1000)),**kwargs)
        except BrowserTimeout:
            self._lookup_failure('页面操作总等待预算已用尽',self._lookup_state())

    def _folder(self,path,deadline=None):
        deadline=self._lookup_deadline() if deadline is None else deadline
        self._observe_loading()
        self._lookup_action(self.page.goto,deadline,url=self.base+'/my',wait_until='domcontentloaded')
        if not self._same_origin() or self.page.locator('input[type=password]').count():
            raise NeedsAttention('TestFairy 会话已失效，停止查找，构建不会重跑')
        labels=path.split('/')
        selector=self.config.get('testfairy',{}).get('folder_selector',_FOLDER_SELECTOR)
        apps_activated=False
        for depth,label in enumerate(labels):
            while True:
                state=self._lookup_state()
                evidence=self.page.evaluate(_FOLDER_CANDIDATES, {'label':label,'parents':labels[:depth],
                    'path':labels,'selector':self.config.get('testfairy',{}).get('folder_selector',_FOLDER_SELECTOR)})
                if evidence['current']:
                    break  # breadcrumb/tree explicitly proves we are already at this path level
                if len(evidence['indices'])==1:
                    self._lookup_action(self.page.locator(selector).nth(evidence['indices'][0]).click,deadline)
                    if not self._same_origin():raise NeedsAttention('TestFairy 目录跳转离开当前租户')
                    break
                if depth==0 and not evidence['indices'] and not apps_activated:
                    apps=self.page.evaluate(_FOLDER_CANDIDATES, {'label':'APPS','parents':[],
                        'path':['APPS'],'selector':selector})
                    if len(apps['indices'])==1:
                        self._lookup_action(self.page.locator(selector).nth(apps['indices'][0]).click,deadline)
                        if not self._same_origin():raise NeedsAttention('APPS 跳转离开当前租户')
                        apps_activated=True
                        continue
                if time.monotonic()>=deadline:
                    evidence['structure']=self.page.evaluate(_FOLDER_STRUCTURE,{'label':label,'selector':selector})
                    self._lookup_failure('目录 '+label+'；已走过 '+('/'.join(labels[:depth]) or 'APPS'),state,evidence)
                self.page.wait_for_timeout(min(200,max(1,(deadline-time.monotonic())*1000)))
    def find(self,expected):
        self.login(expected)
        cfg=self.config.get('testfairy',{})
        deadline=self._lookup_deadline()
        self._folder(expected['path'],deadline)
        selector=cfg.get('card_selector','article, .app-card, .build-card, .version-card, [data-build-id]')
        while True:
            state=self._lookup_state()
            cards=self.page.locator(selector)
            matches=[]
            for i in range(min(cards.count(),100)):
                card=cards.nth(i);parsed=parse_candidate(self._lookup_action(card.inner_text,deadline),expected)
                if parsed:matches.append((card,parsed))
            if len(matches)>1:raise NeedsAttention('同一 GMX 构建匹配多个 TestFairy App，停止生成链接/邮件')
            if matches:
                card,parsed=matches[0]
                links=card.locator('a[href*="/join/"]')
                if not links.count():
                    # Only open the existing Share view. Never Enable/Create/Save permissions.
                    share=card.get_by_role('button',name=re.compile(r'^Share$',re.I))
                    if share.count()!=1:share=card.get_by_role('link',name=re.compile(r'^Share$',re.I))
                    if share.count()!=1:raise NeedsAttention('匹配 App 的现有 Share 入口无法唯一识别')
                    if self.page.locator('[role="dialog"]:visible, dialog[open]:visible').count():
                        raise NeedsAttention('Share 前已有可见弹窗，无法证明链接归属；请关闭后再测试')
                    self._lookup_action(share.click,deadline)
                    while True:
                        state=self._lookup_state()
                        if time.monotonic()>=deadline:
                            self._lookup_failure('Share 未出现属于当前 App 的唯一链接容器；不会从整页或其他 App 取链接',state)
                        dialog=self.page.locator('[role="dialog"]:visible, dialog[open]:visible')
                        if dialog.count()>1:raise NeedsAttention('Share 出现多个弹窗，无法唯一证明当前 App 链接归属')
                        # A newly opened unique dialog or the same verified card only; never search the page.
                        scope=dialog if dialog.count()==1 else card
                        if scope is card and (card.count()!=1 or not parse_candidate(self._lookup_action(card.inner_text,deadline),expected)):
                            raise NeedsAttention('Share 后原 App 卡片身份已变化，不能证明链接归属')
                        links=scope.locator('a[href*="/join/"]')
                        raw=[self._lookup_action(links.nth(i).get_attribute,deadline,name='href') for i in range(links.count())]
                        raw += scope.locator('input[value*="/join/"]').evaluate_all('(xs)=>xs.map(x=>x.value)')
                        if raw:break
                        if time.monotonic()>=deadline:
                            self._lookup_failure('Share 未出现属于当前 App 的唯一链接容器；不会从整页或其他 App 取链接',state)
                        self.page.wait_for_timeout(100)
                else:raw=[self._lookup_action(links.nth(i).get_attribute,deadline,name='href') for i in range(links.count())]
                urls={share_url(value,self.base) for value in raw if value}
                if len(urls)!=1:raise NeedsAttention('未找到唯一已有分享链接；不会创建公开权限')
                resolved=urls.pop()
                if urlsplit(resolved).query:
                    raise NeedsAttention('实际分享链接含查询参数，需人工核对；不会删改参数后发送另一地址')
                parsed.update(share_url=resolved,matched_at=datetime.now(timezone.utc).isoformat())
                self._save_session();return parsed
            if time.monotonic()>=deadline:self._lookup_failure('等待本次 GMX 对应 App 超时；未取最新包',state)
            self.page.wait_for_timeout(min(250,max(0,(deadline-time.monotonic())*1000)))


def mail_preview(params,runtime,created):
    app=runtime['testfairy_app']
    subject=f"[{params['country']}] {params['project']} {app['app_version']} ({app['app_build']})"
    rows=[('App 分享链接',app['share_url']),('App 版本',app['app_version']+' ('+app['app_build']+')'),
          ('国家/地区',params['country']),('项目',params['project']),
          ('Plugin 来源分支',params['plugin_source']),('Plugin 构建分支',params['plugin_branch']),
          ('GMX 来源分支',params['gmx_source']),('GMX 构建分支',params['gmx_branch']),
          ('实际 Plugin 产物版本',runtime.get('artifact_version','')),
          ('Plugin Job',params['plugin_job']),('Plugin 正式构建编号',runtime.get('plugin_formal_number','')),
          ('Plugin 产物构建编号',runtime.get('plugin_artifact_number','')),
          ('GMX Build',app['build_url']),('GMX Build 编号',app['number']),
          ('GMX 提交 SHA',app['sha']),('Plugin 提交 SHA',runtime.get('plugin_sha','')),
          ('上传目录',app['path']),('任务创建时间',created),('链接匹配时间',app['matched_at'])]
    return {'status':'PREVIEW_ONLY','subject':subject,'body':'\n'.join(f'{key}: {value}' for key,value in rows),
            'to':params.get('outlook',{}).get('to',[]),'account':params.get('outlook',{}).get('account',''),'mode':'preview','task_id':params.get('id','')}

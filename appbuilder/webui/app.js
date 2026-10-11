'use strict';
const state={csrf:null,selected:null,offset:0,tasks:[],loading:false};
const $=id=>document.getElementById(id);
const escapeText=s=>String(s??'');
const api=async(path, options={})=>{
  const config={...options,headers:{...options.headers}};
  if(options.body){config.headers['Content-Type']='application/json';config.headers['X-CSRF-Token']=state.csrf;}
  const resp=await fetch(path,config);
  const data=await resp.json();
  if(!resp.ok)throw Error(data.error||`HTTP ${resp.status}`);
  return data;
};
function toast(message,error=false){const node=$('toast');node.textContent=message;node.classList.toggle('error',error);node.classList.add('visible');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.remove('visible'),4500);}
function create(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=escapeText(text);return n;}
function setSelected(id){if(state.selected===id)return;state.selected=id;state.offset=0;$('terminal').replaceChildren(create('div','terminal-empty','正在读取真实操作日志...'));$('screenshots').replaceChildren();renderTasks();renderSelection();loadLog(true);loadScreens();}
function formatDate(s){if(!s)return '—';const d=new Date(s);return isNaN(d.getTime())?s.replace('T',' ').slice(0,16):d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
function renderTasks(){const list=state.tasks;const counts={total:list.length,running:0,success:0,attention:0};for(const t of list){if(['RUNNING','QUEUED'].includes(t.status))counts.running++;if(t.status==='SUCCESS')counts.success++;if(['FAILED','TIMEOUT','NEEDS_ATTENTION','INTERRUPTED'].includes(t.status))counts.attention++;}
  for(const n of Object.keys(counts))$('metric-'+n).textContent=counts[n];
  const tbody=$('tasks-body');tbody.replaceChildren();if(!list.length){const tr=create('tr'),td=create('td','empty-row','尚无构建任务。在左侧创建第一个 Build。');td.colSpan=6;tr.append(td);tbody.append(tr);return;}
  for(const t of list){const tr=create('tr',state.selected===t.id?'selected':'');tr.addEventListener('click',()=>{setSelected(t.id);document.getElementById('activity').scrollIntoView({behavior:'smooth',block:'start'});});
    const info=create('td'),name=create('strong','',t.project||'APP Build');const sub=create('small','',`${t.country} · ${t.id}`);info.append(name,sub);
    const version=create('td','',t.plugin_version||'—');const stage=create('td','',t.stage||'—');
    const date=create('td','',formatDate(t.created));const stat=create('td');stat.append(create('span','pill '+t.status.toLowerCase(),t.status.replace('_',' ')));
    tr.append(info,version,stage,date,stat,create('td','table-action','↗'));tbody.append(tr);}
}
function renderSelection(){const row=state.tasks.find(t=>t.id===state.selected);if(!row)return;
  $('selected-subtitle').textContent=`${row.project} · ${row.country} · ${row.id}`;
  $('tf-test-form').elements.tf_test_task.value=row.id;
  $('mail-test-form').elements.mail_test_task.value=row.id;
  const box=$('active-summary');box.replaceChildren();const icon=create('div','icon-circle',row.status==='SUCCESS'?'✓':'⌁');const info=create('div');info.append(create('strong','',row.stage+' · '+row.status),create('small','',row.note||'任务已创建'));box.append(icon,info);
  const links=$('jenkins-links');links.replaceChildren();
  for(const link of row.jenkins_links||[]){
    try{const target=new URL(link.url);if(target.protocol!=='https:'||target.username||target.password||target.search||target.hash)continue;
      const a=create('a','jenkins-link','↗ '+link.label);a.href=target.href;a.target='_blank';a.rel='noopener noreferrer';links.append(a);
    }catch(e){/* invalid stored URL is never rendered */}
  }
  if(row.delivery?.app){
    try{const url=new URL(row.delivery.app.share_url);if(url.protocol==='https:'&&url.hostname.endsWith('.testfairy.com')&&url.pathname.startsWith('/join/')){
      const a=create('a','jenkins-link','↗ TestFairy '+row.delivery.app.app_version);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';links.append(a);
    }}catch(e){}
  }
  if(row.delivery?.mail_preview){const details=create('details'),title=create('summary','',row.delivery.mail_delivery?.status==='SUBMITTED'?'邮件预览 · 已提交 Outlook（非送达确认）':row.delivery.mail_delivery?.status==='UNKNOWN'?'邮件预览 · 提交结果不明，禁止自动重发':row.delivery.mail_delivery?.status==='BLOCKED'?'邮件预览 · 发送受阻：'+row.delivery.mail_delivery.reason:'邮件预览 · 尚未发送'),text=create('pre','','To: '+row.delivery.mail_preview.to.join(', ')+'\nFrom: '+(row.delivery.mail_delivery?.account||row.delivery.mail_preview.account||'待本机核验')+'\n'+row.delivery.mail_preview.subject+'\n\n'+row.delivery.mail_preview.body);details.append(title,text);box.append(details);}
  const phases=['PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH','PLUGIN_JENKINS','GMX_CHECKOUT','GMX_EDIT','GMX_LOCK','GMX_PUSH','GMX_JENKINS'];const idx=phases.indexOf(row.stage);
  $('stage-strip').querySelectorAll('div').forEach((e,i)=>{e.className='';if(row.status==='SUCCESS'||(idx>=0&&i<idx))e.className='done';else if(i===idx)e.className='current';});
  $('resume-build').hidden=['SUCCESS','RUNNING','QUEUED'].includes(row.status);
}
async function loadTasks(){if(state.loading)return;state.loading=true;try{const res=await api('/api/tasks');state.tasks=res.tasks||[];if(!state.selected&&state.tasks.length){state.selected=state.tasks[0].id;state.offset=0;}renderTasks();renderSelection();}catch(e){toast('任务状态刷新失败：'+e.message,true)}finally{state.loading=false;}}
async function loadLog(reset=false){if(!state.selected)return;const selected=state.selected;if(reset)state.offset=0;
 try{const q=state.offset>0?`?offset=${state.offset}`:'';const data=await api(`/api/tasks/${encodeURIComponent(selected)}/log${q}`);if(state.selected!==selected)return;
  const terminal=$('terminal');const nearBottom=terminal.scrollTop+terminal.clientHeight>terminal.scrollHeight-75;
  if(reset||state.offset===0)terminal.replaceChildren();
  if(data.lines){terminal.append(document.createTextNode(data.lines));}
  state.offset=data.offset;$('log-line-count').textContent=`${(data.size/1024).toFixed(1)} KB`;
  if(nearBottom||reset)terminal.scrollTop=terminal.scrollHeight;
 }catch(e){$('terminal').append(document.createTextNode('\n[WEB] '+e.message));}}
async function loadScreens(){if(!state.selected)return;const selected=state.selected;try{const data=await api(`/api/tasks/${encodeURIComponent(selected)}/screenshots`);if(selected!==state.selected)return;const root=$('screenshots');root.replaceChildren();for(const filename of data.files){const a=create('a','',`◧ ${filename}`);a.href=`/api/tasks/${encodeURIComponent(selected)}/screenshots/${encodeURIComponent(filename)}`;a.target='_blank';a.rel='noopener noreferrer';root.append(a);}}catch(e){/* Screenshot list is optional */}}
// One DOM form and one serializer for manual builds and scheduled templates.
function buildBody(){
 const body={};
 for(const field of $('build-form').querySelectorAll('[data-build]'))
   body[field.name]=field.type==='checkbox'?field.checked:field.value;
 return body;
}
function applyBuildBody(body){
 const f=$('build-form').elements;
 for(const [key,value] of Object.entries(body))if(f[key]){
   if(f[key].type==='checkbox')f[key].checked=Boolean(value);else f[key].value=value??'';
 }
 customFolderEdited=Boolean(body.gmx_custom_folder)&&body.gmx_custom_folder!==`0_A_${f.country.value}_${f.project_name.value}`;syncSummary();
}
async function submitBuild(evt){evt.preventDefault();
 if($('save-as-schedule').checked)return window.scheduleEditor.submit();
 const button=$('submit-build'),form=$('build-form'),body=buildBody();
 button.disabled=true;button.textContent='Starting build...';$('form-message').textContent='正在检查环境并创建任务...';$('form-message').classList.remove('error');
 try{const res=await api('/api/tasks',{method:'POST',body:JSON.stringify(body)});form.elements.testfairy_user.value='';form.elements.testfairy_password.value='';form.elements.jenkins_password.value='';form.elements.github_token.value='';form.elements.jenkins_user.value='';$('form-message').textContent='任务已创建：'+res.task.id;toast('构建任务已启动 · '+res.task.id);await refreshDoctor();await loadTasks();setSelected(res.task.id);document.getElementById('activity').scrollIntoView({behavior:'smooth',block:'start'});}
 catch(e){$('form-message').textContent=e.message;$('form-message').classList.add('error');toast(e.message,true)}finally{button.disabled=false;button.textContent='▶   Start build  ↗';}}
async function resumeTask(){const id=state.selected;if(!id)return;if(!confirm(`恢复任务 ${id}？\n将复用既有分支和 Jenkins Build，不重新创建任务。`))return;
 try{await api(`/api/tasks/${encodeURIComponent(id)}/resume`,{method:'POST',body:JSON.stringify({visible:true,verbose:true})});toast('已请求恢复任务');state.offset=0;await loadTasks();await loadLog(true);}catch(e){toast(e.message,true);}}
async function refreshDoctor(){try{const r=await api('/api/doctor');const badge=$('doctor-badge');badge.className=r.ready?'dot-label':'dot-label bad';badge.textContent=r.ready?'● Environment ready':'● Setup required';badge.title=(r.messages||[]).join('\n');if(!r.ready)toast('环境检查：'+(r.messages||[]).slice(-1)[0],true);}catch(e){toast(e.message,true);}}
function nav(target){document.getElementById(target).scrollIntoView({behavior:'smooth',block:'start'});document.querySelectorAll('.nav').forEach(e=>e.classList.toggle('active',e.dataset.target===target));}
let customFolderEdited=false;
function syncSummary(){
  const f=$('build-form').elements;
  const country=(f.country.value||'UK').trim().toUpperCase();
  const project=(f.project_name.value||'PayByBank').trim();
  const computed=`0_A_${country}_${project}`;
  if(!customFolderEdited)f.gmx_custom_folder.value=computed;
  $('derived-plugin').textContent=`release/${f.plugin_version.value||'1.3'}.[timestamp]`;
  const keys=[...new Set((f.gmx_branch.value.match(/(?<![A-Za-z0-9])[A-Z]{1,10}-[0-9]+(?![A-Za-z0-9])/g)||[]))];
  const issue=f.gmx_issue_key.value.trim()||(keys.length===1?keys[0]:'');
  $('derived-gmx').textContent=issue?`upload/${issue}-auto[timestamp]`:'upload/AUTO-[timestamp]';
  $('derived-folder').textContent=f.gmx_custom_folder.value;
}
let buildDefaults={};
function installDefaults(defaults){
 const {gmx_fields={},max_parallel,...values}=defaults;
 buildDefaults={...values};
 for(const [key,value] of Object.entries(gmx_fields))buildDefaults['selector_'+key.toLowerCase()]=value;
 applyBuildBody(buildDefaults);
 $('build-form').elements.max_parallel_display.value=String(max_parallel);
}
function resetBuildForm(){
 $('build-form').reset();applyBuildBody(buildDefaults);
 document.querySelectorAll('#build-form details').forEach(panel=>panel.open=false);
 reloadBranches();
}
const branchLoads={};
async function loadBranches(kind){
 const f=$('build-form').elements, url=f[kind+'_url'].value.trim();
 const generation=(branchLoads[kind]||0)+1;branchLoads[kind]=generation;
 const list=$(kind+'-branches'),status=$(kind+'-branch-status');list.replaceChildren();
 let page=1,count=0;
 status.textContent='正在读取全部分支分页…';
 try{
   while(true){
     const data=await api('/api/branches',{method:'POST',body:JSON.stringify({kind,url,page})});
     if(branchLoads[kind]!==generation)return;
     for(const name of data.branches){const option=document.createElement('option');option.value=name;list.append(option);count++;}
     status.textContent=`已读取 ${count} 个分支${data.has_more?'，继续下一页…':'；输入关键词搜索或手动填写完整分支。'}`;
     if(!data.has_more)break;page++;
   }
 }catch(e){if(branchLoads[kind]===generation)status.textContent=`分支读取失败：${e.message}。已读取 ${count} 个；可刷新重试或手动填写。`;}
}
function reloadBranches(){for(const kind of ['plugin','gmx'])loadBranches(kind);}

let currentDiagnostic=null;
async function diagnoseJenkins(evt){
 evt.preventDefault();const form=evt.currentTarget,out=$('diagnose-output');
 const raw=form.elements.build_number.value.trim();
 const body={site:form.dataset.site,url:form.elements.url.value.trim(),build_number:raw?Number(raw):null,
   expected:form.elements.expected.value};
 if(body.site==='plugin')body.expected_artifacts={version:form.elements.expected_version.value.trim(),journey_sha256:form.elements.journey_sha256.value.trim(),domainmodel_sha256:form.elements.domainmodel_sha256.value.trim()};
 const button=form.querySelector('button[type="submit"]');
 button.disabled=true;const label=button.textContent;button.textContent='正在启动可见 Chrome…';
 $('diagnose-verdict').textContent=`RUNNING · ${body.site.toUpperCase()} · 允许人工登录 SSO（最长 180 秒）`;
 $('diagnose-verdict').className='diagnose-verdict';
 out.textContent=`[START] ${body.site.toUpperCase()} 开始只读单元测试。请查看新弹出的 Chrome，如有登录页可手动输入账号密码。\n`;
 try{
  const start=await api('/api/diagnose/start',{method:'POST',body:JSON.stringify(body)});
  currentDiagnostic=start.id;
  for(let i=0;i<240;i++){
    const progress=await api(`/api/diagnose/status/${encodeURIComponent(start.id)}`);
    if(currentDiagnostic!==start.id)break;
    out.textContent=progress.logs.join('\n');out.scrollTop=out.scrollHeight;
    if(progress.status==='DONE'){
      const r=progress.report||{},el=$('diagnose-verdict');
      el.textContent=`${r.verdict||'UNKNOWN'} · ${r.site?.toUpperCase()||body.site.toUpperCase()} Build #${r.build_number||'?'} · Expected ${r.expected||body.expected} · Actual ${r.actual||'UNKNOWN'}`;
      if(r.artifact_result&&r.site==='plugin')el.textContent+=` · Nexus 版本/SHA ${r.artifact_result.status}`;
      el.className='diagnose-verdict '+String(r.verdict||'unknown').toLowerCase();
      break;
    }
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
 }catch(err){$('diagnose-verdict').textContent='ERROR · '+err.message;$('diagnose-verdict').className='diagnose-verdict fail';out.textContent+='\n[ERROR] '+err.message;}
 finally{button.disabled=false;button.textContent=label;}
}
async function launchGMXLiveTest(evt){
 evt.preventDefault();const form=evt.currentTarget,kind=form.dataset.kind;
 const result=$(kind+'-live-result'),out=$(kind+'-live-output');
 const f=form.elements;const body={kind,branch:f.branch.value.trim()};
 if(kind==='gmx-build'){
   if(!f.confirm_trigger.checked){result.textContent='请先确认：这会真正触发 Jenkins Build';return;}
   if(!confirm('确认对分支 '+body.branch+' 真正执行 GMX Jenkins 构建？\n如果只想检查状态，请使用 Jenkins URL Test。'))return;
   Object.assign(body,{job_url:f.job_url.value.trim(),country:f.country.value.trim().toUpperCase(),
     project:f.project.value.trim(),build_type:f.build_type.value.trim(),
     signing_repo_branch:f.signing_repo_branch.value.trim(),custom_folder:f.custom_folder.value.trim(),
     sync_upload:f.sync_upload.checked,skip_test:f.skip_test.checked,
     force:f.force.checked,keep_alive:f.keep_alive.checked,confirm_trigger:true});
 }
 const btn=form.querySelector('button[type="submit"]'),previous=btn.textContent;
 btn.disabled=true;btn.textContent='执行中，请查看下方日志…';result.textContent='RUNNING · '+kind+' · '+body.branch;
 result.className='diagnose-verdict';out.textContent='[START] '+kind+' '+body.branch+'\n';
 try{
   const job=await api('/api/live-tests/start',{method:'POST',body:JSON.stringify(body)});
   for(let i=0;i<6000;i++){
     const p=await api('/api/live-tests/status/'+encodeURIComponent(job.id));
     out.textContent=p.logs.join('\n');out.scrollTop=out.scrollHeight;
     if(p.status==='DONE'){
       const report=p.report||{};
       result.textContent=String(report.verdict||'UNKNOWN')+' · '+kind+' · '+body.branch+
          (report.actual==='TRIGGERED'?' · 第二次 Build 已触发，未等待完成':'')+
          (report.build_number?' · Build #'+report.build_number:'')+
          (report.error?' · '+report.error:'');
       result.className='diagnose-verdict '+(report.verdict==='PASS'?'pass':'fail');
       break;
     }
     await new Promise(resolve=>setTimeout(resolve,2000));
   }
 }catch(e){result.className='diagnose-verdict fail';result.textContent='ERROR · '+e.message;out.textContent+='\n[ERROR] '+e.message;}
 finally{btn.disabled=false;btn.textContent=previous;}
}
async function init(){const serverAddress=document.querySelector('.connection strong');if(serverAddress)serverAddress.textContent=window.location.host;try{const boot=await api('/api/bootstrap');state.csrf=boot.csrf;installDefaults(boot.config);reloadBranches();$('doctor-badge').className=boot.doctor.ready?'dot-label':'dot-label bad';$('doctor-badge').textContent=boot.doctor.ready?'● Environment ready':'● Setup required';$('doctor-badge').title=boot.doctor.messages.join('\n');await loadTasks();await loadLog(true);await loadScreens();}catch(e){toast('本地服务连接失败：'+e.message,true);} }
document.querySelectorAll('.nav').forEach(e=>e.addEventListener('click',()=>nav(e.dataset.target)));
$('hero-start').addEventListener('click',()=>nav('new-build'));
$('build-form').elements.gmx_custom_folder.addEventListener('input',()=>{customFolderEdited=Boolean($('build-form').elements.gmx_custom_folder.value.trim());syncSummary();});
for(const field of ['plugin_version','country','project_name','gmx_branch','gmx_issue_key'])$('build-form').elements[field].addEventListener('input',syncSummary);
// Expand collapsed advanced editor if native form validation finds an error.
$('build-form').addEventListener('invalid',evt=>{
  let panel=evt.target.closest('details');
  while(panel){panel.open=true;panel=panel.parentElement.closest('details');}
},true);
$('build-form').addEventListener('submit',submitBuild);
for(const kind of ['plugin','gmx']){
 $('build-form').elements[kind+'_url'].addEventListener('change',()=>loadBranches(kind));
 document.querySelector(`.branch-refresh[data-kind="${kind}"]`).addEventListener('click',()=>loadBranches(kind));
}
$('diagnose-form').addEventListener('submit',diagnoseJenkins);
$('diagnose-gmx-form').addEventListener('submit',diagnoseJenkins);
$('gmx-build-live-form').addEventListener('submit',launchGMXLiveTest);
$('resume-build').addEventListener('click',resumeTask);
$('refresh-tasks').addEventListener('click',loadTasks);
$('doctor-refresh').addEventListener('click',refreshDoctor);
$('clear-log').addEventListener('click',()=>loadLog(true));
setInterval(async()=>{if(!state.csrf)return;await loadTasks();await loadLog();if(state.selected)await loadScreens();},3000);
init();

$('check-outlook').addEventListener('click',async()=>{const out=$('outlook-status');out.textContent='正在只读检查…';try{const r=await api('/api/outlook');out.textContent='Outlook '+r.version+' · '+r.accounts.map(a=>a.email).join(', ');if(r.accounts.length===1)$('build-form').elements.outlook_account.value=r.accounts[0].email;}catch(e){out.textContent=e.message;}});

async function runDeliveryTest(kind){
 const f=$(kind==='testfairy'?'tf-test-form':'mail-test-form').elements,button=$(kind==='testfairy'?'test-testfairy':'test-outlook-send'),out=$(kind==='testfairy'?'tf-test-result':'mail-test-result');
 const body={kind,request_id:crypto.randomUUID()};
 if(kind==='testfairy')Object.assign(body,{url:f.tf_test_url.value,username:f.tf_test_user.value,password:f.tf_test_password.value,task_id:f.tf_test_task.value,expected_share_url:f.tf_test_expected.value});
 else Object.assign(body,{task_id:f.mail_test_task.value,recipients:f.mail_test_to.value,account:$('build-form').elements.outlook_account.value,confirm_send:true});
 button.disabled=true;out.textContent='正在执行独立测试…';
 try{const r=await api('/api/delivery-tests/start',{method:'POST',body:JSON.stringify(body)});if(kind==='testfairy')f.tf_test_password.value='';delete body.password;
   for(let i=0;i<360;i++){const result=await api('/api/delivery-tests/'+encodeURIComponent(r.id));out.textContent=result.status+' · '+(result.result.message||'执行中')+(result.result.final_url?'\n实际地址（已去除敏感查询参数）：'+result.result.final_url:'')+(result.result.expected_share_url?'\n预期链接：'+result.result.expected_share_url+'\n实际链接：'+result.result.actual_share_url:'')+(result.result.account?'\n发件账号：'+result.result.account:'');if(!['RUNNING','SUBMITTING'].includes(result.status))return;await new Promise(resolve=>setTimeout(resolve,1000));}
   out.textContent+='\n等待结果超时；不会自动再次发送，请核对原测试结果。';
 }catch(e){out.textContent=e.message;}finally{button.disabled=false;}
}
$('test-testfairy').addEventListener('click',()=>runDeliveryTest('testfairy'));
$('test-outlook-send').addEventListener('click',()=>runDeliveryTest('outlook'));

for(const id of ['tf-test-form','mail-test-form'])$(id).addEventListener('submit',e=>e.preventDefault());

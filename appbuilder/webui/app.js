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
  const box=$('active-summary');box.replaceChildren();const icon=create('div','icon-circle',row.status==='SUCCESS'?'✓':'⌁');const info=create('div');info.append(create('strong','',row.stage+' · '+row.status),create('small','',row.note||'任务已创建'));box.append(icon,info);
  const links=$('jenkins-links');links.replaceChildren();
  for(const link of row.jenkins_links||[]){
    try{const target=new URL(link.url);if(target.protocol!=='https:'||target.username||target.password||target.search||target.hash)continue;
      const a=create('a','jenkins-link','↗ '+link.label);a.href=target.href;a.target='_blank';a.rel='noopener noreferrer';links.append(a);
    }catch(e){/* invalid stored URL is never rendered */}
  }
  const phases=['PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH','PLUGIN_JENKINS','GMX_CHECKOUT','GMX_EDIT','GMX_BOOTSTRAP','GMX_PUSH','GMX_JENKINS'];const idx=phases.indexOf(row.stage);
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
async function submitBuild(evt){evt.preventDefault();const button=$('submit-build'),form=$('build-form');const data=new FormData(form);
 const body={};for(const name of ['plugin_branch','gmx_branch','plugin_version','country','project_name','jenkins_user','jenkins_password','github_token'])body[name]=data.get(name)||'';
 body.visible=form.elements.visible.checked;body.verbose=form.elements.verbose.checked;
 button.disabled=true;button.textContent='Starting build...';$('form-message').textContent='正在检查环境并创建任务...';$('form-message').classList.remove('error');
 try{const res=await api('/api/tasks',{method:'POST',body:JSON.stringify(body)});form.elements.jenkins_password.value='';form.elements.github_token.value='';form.elements.jenkins_user.value='';$('form-message').textContent='任务已创建：'+res.task.id;toast('构建任务已启动 · '+res.task.id);await refreshDoctor();await loadTasks();setSelected(res.task.id);document.getElementById('activity').scrollIntoView({behavior:'smooth',block:'start'});}
 catch(e){$('form-message').textContent=e.message;$('form-message').classList.add('error');toast(e.message,true)}finally{button.disabled=false;button.textContent='▶   Start build  ↗';}}
async function resumeTask(){const id=state.selected;if(!id)return;if(!confirm(`恢复任务 ${id}？\n将复用既有分支和 Jenkins Build，不重新创建任务。`))return;
 try{await api(`/api/tasks/${encodeURIComponent(id)}/resume`,{method:'POST',body:JSON.stringify({visible:true,verbose:true})});toast('已请求恢复任务');state.offset=0;await loadTasks();await loadLog(true);}catch(e){toast(e.message,true);}}
async function refreshDoctor(){try{const r=await api('/api/doctor');const badge=$('doctor-badge');badge.className=r.ready?'dot-label':'dot-label bad';badge.textContent=r.ready?'● Environment ready':'● Setup required';badge.title=(r.messages||[]).join('\n');if(!r.ready)toast('环境检查：'+(r.messages||[]).slice(-1)[0],true);}catch(e){toast(e.message,true);}}
function nav(target){document.getElementById(target).scrollIntoView({behavior:'smooth',block:'start'});document.querySelectorAll('.nav').forEach(e=>e.classList.toggle('active',e.dataset.target===target));}
async function init(){const serverAddress=document.querySelector('.connection strong');if(serverAddress)serverAddress.textContent=window.location.host;try{const boot=await api('/api/bootstrap');state.csrf=boot.csrf;$('doctor-badge').className=boot.doctor.ready?'dot-label':'dot-label bad';$('doctor-badge').textContent=boot.doctor.ready?'● Environment ready':'● Setup required';$('doctor-badge').title=boot.doctor.messages.join('\n');await loadTasks();await loadLog(true);await loadScreens();}catch(e){toast('本地服务连接失败：'+e.message,true);} }
document.querySelectorAll('.nav').forEach(e=>e.addEventListener('click',()=>nav(e.dataset.target)));
$('hero-start').addEventListener('click',()=>nav('new-build'));
$('build-form').addEventListener('submit',submitBuild);
$('resume-build').addEventListener('click',resumeTask);
$('refresh-tasks').addEventListener('click',loadTasks);
$('doctor-refresh').addEventListener('click',refreshDoctor);
$('clear-log').addEventListener('click',()=>loadLog(true));
setInterval(async()=>{if(!state.csrf)return;await loadTasks();await loadLog();if(state.selected)await loadScreens();},3000);
init();

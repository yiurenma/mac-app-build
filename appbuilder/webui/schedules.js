'use strict';
// Schedules = the existing "New build" form + a cron. Reuses globals from app.js:
// api, create, toast, state, nav, setSelected, collectBuildBody, syncSummary.
(function(){
const el=id=>document.getElementById(id);
const S={list:[],editing:null,runsFor:null,previewTimer:null,visibleBefore:true};
const SECRETS=['jenkins_user','jenkins_password','github_token'];
const START_LABEL='▶   Start build  ↗';
const form=()=>el('build-form');
const scheduling=()=>el('schedule-toggle').checked;
function fmt(iso){return iso?String(iso).replace('T',' ').slice(0,16):'—';}
function pill(status){return create('span','pill '+String(status||'').toLowerCase().replace(/_/g,'-'),String(status||'—').replace(/_/g,' '));}
function button(label,handler){const b=create('button','subtle-btn',label);b.type='button';b.addEventListener('click',handler);return b;}
function openTask(taskId){if(!taskId)return;setSelected(taskId);nav('activity');}

// ---- New build form: schedule mode -------------------------------------------------
function submitLabel(){return S.editing?'✓ 保存定时任务修改':'⏱ Save schedule';}
function setMode(on){
  const f=form().elements;
  el('schedule-fields').hidden=!on;
  for(const n of ['schedule_name','schedule_cron','schedule_enabled','schedule_edit_id'])f[n].disabled=!on;
  f.schedule_name.required=on;f.schedule_cron.required=on;
  el('submit-build').textContent=on?submitLabel():START_LABEL;
}
function onToggle(){
  const f=form().elements,on=scheduling();
  if(on){S.visibleBefore=f.visible.checked;f.visible.checked=false;}   // unattended runs: no browser window by default
  else{f.visible.checked=S.visibleBefore;S.editing=null;f.schedule_edit_id.value='';el('schedule-cancel').hidden=true;}
  setMode(on);
}
function fillBuildForm(template){
  const f=form().elements;
  for(const [key,value] of Object.entries(template||{})){
    const field=f[key];if(!field||SECRETS.includes(key))continue;
    if(field.type==='checkbox')field.checked=Boolean(value);else field.value=String(value);
  }
  // app.js derives CUSTOM_FOLDER from country/project unless the user edited it.
  const computed=`0_A_${(template.country||'UK').toUpperCase()}_${template.project_name||''}`;
  customFolderEdited=Boolean(template.gmx_custom_folder)&&template.gmx_custom_folder!==computed;
  syncSummary();
}
function edit(row){
  const f=form().elements;
  S.visibleBefore=true;el('schedule-toggle').checked=true;S.editing=row.id;
  fillBuildForm(row.template);
  f.schedule_edit_id.value=row.id;setMode(true);
  f.schedule_name.value=row.name;f.schedule_cron.value=row.cron;f.schedule_enabled.checked=row.enabled;
  f.visible.checked=Boolean(row.template.visible);
  el('schedule-cancel').hidden=false;previewCron();nav('new-build');
}
function cancelEdit(){el('schedule-toggle').checked=false;onToggle();el('form-message').textContent='';}
async function save(){
  const f=form().elements,message=el('form-message'),btn=el('submit-build'),editing=S.editing;
  const template=collectBuildBody();for(const k of SECRETS)delete template[k];
  const payload={name:f.schedule_name.value.trim(),cron:f.schedule_cron.value.trim(),enabled:f.schedule_enabled.checked,template};
  btn.disabled=true;message.textContent='正在保存定时任务…';message.classList.remove('error');
  try{
    if(editing)await api(`/api/schedules/${encodeURIComponent(editing)}/update`,{method:'POST',body:JSON.stringify(payload)});
    else await api('/api/schedules',{method:'POST',body:JSON.stringify(payload)});
    toast(editing?'定时任务已更新':'定时任务已创建');message.textContent='';
    f.schedule_name.value='';f.schedule_cron.value='';cancelEdit();await load();nav('schedules');
  }catch(e){message.textContent=e.message;message.classList.add('error');toast(e.message,true);}
  finally{btn.disabled=false;}
}
// app.js calls this at the top of submitBuild(); returning true means "handled, don't start a build".
window.scheduleSubmitHook=function(evt){if(!scheduling())return false;evt.preventDefault();save();return true;};

async function previewCron(){
  const text=form().elements.schedule_cron.value.trim(),out=el('cron-preview');
  if(!text){out.textContent='输入 cron 后在这里预览接下来 5 次运行时间。';out.classList.remove('error');return;}
  try{
    const data=await api('/api/schedules/preview?cron='+encodeURIComponent(text));
    out.textContent='接下来 5 次：'+data.next.map(fmt).join('  ·  ');out.classList.remove('error');
  }catch(e){out.textContent='✕ '+e.message;out.classList.add('error');}
}

// ---- Schedules list -----------------------------------------------------------------
function renderStatus(info,maxParallel,minInterval){
  const box=el('scheduler-status'),s=info||{};
  box.className='diagnose-verdict '+(s.online?'pass':'fail');
  box.textContent=s.online
    ?`调度器在线 · pid ${s.pid} · 并发上限 ${maxParallel}（与手动构建共用）· 最小运行间隔 ${minInterval} 分钟`
    :'调度器离线 · 定时任务不会触发。请在终端运行 ./app-build scheduler install（或临时用 ./app-build scheduler run）';
}
function renderRows(){
  const body=el('schedules-body');body.replaceChildren();
  if(!S.list.length){const tr=create('tr'),td=create('td','empty-row','还没有定时任务。在 New build 表单底部打开「保存为定时任务」后创建。');td.colSpan=6;tr.append(td);body.append(tr);return;}
  for(const row of S.list){
    const tr=create('tr',row.enabled?'':'sched-off');
    const name=create('td');name.append(create('strong','',row.name),create('small','',`${row.template.country||''} · ${row.template.project_name||''} · ${row.template.plugin_version||''}`));
    const cronCell=create('td');cronCell.append(create('span','sched-cron',row.cron));
    const last=create('td');
    if(row.last_run){
      const p=pill(row.last_run.task_status||row.last_run.status);
      if(row.last_run.task_id){p.classList.add('sched-link');p.title='查看这次构建';p.addEventListener('click',()=>openTask(row.last_run.task_id));}
      last.append(p,create('small','',' '+fmt(row.last_run.planned)));
      if(row.last_run.note)last.append(create('small','',' · '+row.last_run.note));
    }else last.textContent='—';
    const actions=create('td'),box=create('div','sched-actions');
    box.append(button(row.enabled?'停用':'启用',()=>toggle(row)),button('立即运行',()=>runNow(row)),
      button('编辑',()=>edit(row)),button('记录',()=>showRuns(row)),button('删除',()=>remove(row)));
    actions.append(box);
    tr.append(name,cronCell,create('td','',row.enabled?fmt(row.next_run):'已停用'),last,create('td','',row.enabled?'ON':'OFF'),actions);body.append(tr);
  }
}
async function load(){
  try{
    const data=await api('/api/schedules');
    S.list=data.schedules||[];renderStatus(data.scheduler,data.max_parallel,data.min_interval_minutes);renderRows();
    if(S.runsFor)await showRuns(S.list.find(r=>r.id===S.runsFor),true);
  }catch(e){el('scheduler-status').className='diagnose-verdict fail';el('scheduler-status').textContent='读取定时任务失败：'+e.message;}
}
async function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body||{})});}
async function toggle(row){try{await post(`/api/schedules/${encodeURIComponent(row.id)}/toggle`,{enabled:!row.enabled});toast(row.enabled?'已停用':'已启用');await load();}catch(e){toast(e.message,true);}}
async function runNow(row){
  if(!confirm(`立即运行「${row.name}」？\n将创建新的分支和 Jenkins 构建；并发已满时会排队等待。`))return;
  try{await post(`/api/schedules/${encodeURIComponent(row.id)}/run-now`);toast('已加入队列，调度器会在下一轮（约 15 秒内）启动');setTimeout(load,1500);}catch(e){toast(e.message,true);}
}
async function remove(row){
  if(!confirm(`删除定时任务「${row.name}」及其运行记录？\n已经创建的构建任务不受影响。`))return;
  try{await post(`/api/schedules/${encodeURIComponent(row.id)}/delete`);if(S.editing===row.id)cancelEdit();if(S.runsFor===row.id){S.runsFor=null;el('schedule-runs').hidden=true;}toast('已删除');await load();}catch(e){toast(e.message,true);}
}
async function showRuns(row,silent){
  if(!row)return;S.runsFor=row.id;
  try{
    const data=await api(`/api/schedules/${encodeURIComponent(row.id)}/runs`);
    el('schedule-runs-title').textContent=`RUN HISTORY · ${row.name}`;
    const body=el('schedule-runs-body');body.replaceChildren();
    if(!data.runs.length){const tr=create('tr'),td=create('td','empty-row','还没有运行记录。');td.colSpan=4;tr.append(td);body.append(tr);}
    for(const run of data.runs){
      const tr=create('tr'),status=create('td'),build=create('td');status.append(pill(run.task_status||run.status));
      if(run.task_id){const a=create('span','sched-link',run.task_id);a.addEventListener('click',()=>openTask(run.task_id));build.append(a);}else build.textContent='—';
      tr.append(create('td','',fmt(run.planned)),status,build,create('td','',run.note||''));body.append(tr);
    }
    el('schedule-runs').hidden=false;
    if(!silent)el('schedule-runs').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){if(!silent)toast(e.message,true);}
}

el('schedule-toggle').addEventListener('change',onToggle);
el('schedule-cancel').addEventListener('click',cancelEdit);
el('schedule-runs-close').addEventListener('click',()=>{S.runsFor=null;el('schedule-runs').hidden=true;});
form().elements.schedule_cron.addEventListener('input',()=>{clearTimeout(S.previewTimer);S.previewTimer=setTimeout(previewCron,400);});
// app.js fetches the CSRF token asynchronously; start once it is available.
const ready=setInterval(()=>{if(state.csrf){clearInterval(ready);load();setInterval(load,5000);}},300);
})();

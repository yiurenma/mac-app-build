'use strict';
// Schedules page. Reuses globals from app.js (api, create, toast, state, nav, setSelected).
(function(){
const el=id=>document.getElementById(id);
const store={list:[],editing:null,runsFor:null,previewTimer:null,previewGeneration:0,editorGeneration:0,saving:false,revision:null,copyName:null};
function fmt(iso){return iso?String(iso).replace('T',' ').slice(0,16):'—';}
function pill(status){return create('span','pill '+String(status||'').toLowerCase().replace(/_/g,'-'),String(status||'—').replace(/_/g,' '));}
function button(label,handler,cls){const b=create('button',cls||'subtle-btn',label);b.type='button';b.addEventListener('click',handler);return b;}
async function openTask(taskId){if(!taskId)return;await loadTasks();setSelected(taskId);await loadLog(true);nav('activity');}

function renderStatus(info,maxParallel,minInterval){
  const box=el('scheduler-status'),s=info||{};
  box.className='diagnose-verdict '+(s.online?'pass':'fail');
  box.textContent=s.online
    ?`调度器在线 · pid ${s.pid} · 并发上限 ${maxParallel}（与手动构建共用）· 最小运行间隔 ${minInterval} 分钟`
    :`调度器离线 · 并发上限 ${maxParallel} · 最小间隔 ${minInterval} 分钟。${s.startup||s.reason||'Web 正在自动检查'}。日志：${s.log_path||'.appbuilder/scheduler.log'}；修复后 Web 自动重试。`;
}
function renderRows(){
  const body=el('schedules-body');body.replaceChildren();
  if(!store.list.length){const tr=create('tr'),td=create('td','empty-row','还没有定时任务。点击上方按钮，在 New build 共用表单创建。');td.colSpan=6;tr.append(td);body.append(tr);return;}
  for(const row of store.list){
    const tr=create('tr',row.enabled?'':'sched-off');
    const name=create('td');name.append(create('strong','',row.name),create('small','',`${row.template.country} · ${row.template.project_name} · ${row.template.plugin_version} · Chrome ${row.template.visible?'显示':'隐藏'}`));
    const cronCell=create('td');cronCell.append(create('span','sched-cron',row.cron));
    const next=create('td','',row.success_task_id?'本轮成功后已停止':row.enabled?fmt(row.next_run):'已停用');
    if(row.stop_after_success)name.append(create('small','','成功一次后停止 · 同时最多一个任务'));
    if(row.success_task_id)next.append(button('成功构建',()=>openTask(row.success_task_id),'subtle-btn sched-link'));
    const last=create('td');
    if(row.last_run){
      const shown=row.last_run.task_status||row.last_run.status;
      const p=row.last_run.task_id?button(shown,()=>openTask(row.last_run.task_id),'pill sched-link'):pill(shown);p.title='查看这次构建与日志';
      last.append(p,create('small','',' '+fmt(row.last_run.planned)));
      if(row.last_run.note)last.append(create('small','',' · '+row.last_run.note));
    }else last.textContent='—';
    const enabled=create('td','',row.enabled?'ON':'OFF');
    const actions=create('td');const box=create('div','sched-actions');
    box.append(
      button(row.enabled?'停用':row.success_task_id?'重新启用':'启用',()=>toggle(row)),
      button('立即运行',()=>runNow(row)),
      button('编辑',()=>edit(row)),
      button('拷贝',()=>edit(row,true)),
      button('记录',()=>showRuns(row)),
      button('删除',()=>remove(row)));
    actions.append(box);
    tr.append(name,cronCell,next,last,enabled,actions);body.append(tr);
  }
}
async function load(){
  try{
    const data=await api('/api/schedules');
    store.list=data.schedules||[];renderStatus(data.scheduler,data.max_parallel,data.min_interval_minutes);renderRows();
    if(store.runsFor)await showRuns(store.list.find(r=>r.id===store.runsFor),true);
  }catch(e){el('scheduler-status').className='diagnose-verdict fail';el('scheduler-status').textContent='读取定时任务失败：'+e.message;}
}
function updateMode(){
  const active=el('save-as-schedule').checked;
  el('schedule-settings').hidden=!active;el('schedule-settings').disabled=!active;
  el('submit-build').textContent=active?(store.editing?'✓ 保存定时修改':'＋ 保存定时任务'):'▶ Start build';
  el('schedule-cancel').hidden=!active;
  if(!active){store.editorGeneration++;store.editing=null;store.copyName=null;store.revision=null;el('form-message').textContent='';}
}
function resetForm(){
  store.editorGeneration++;store.editing=null;store.revision=null;store.copyName=null;resetBuildForm();el('save-as-schedule').checked=false;updateMode();
  el('cron-preview').textContent='输入 cron 后预览接下来 5 次运行。';
  el('cron-preview').classList.remove('error');store.previewGeneration++;
}
async function submit(){
  const f=el('build-form').elements, message=el('form-message'),btn=el('submit-build'),editing=store.editing,generation=store.editorGeneration;
  if(store.saving)return;
  if(store.copyName!==null && (!f.schedule_name.value.trim() || f.schedule_name.value.trim()===store.copyName)){message.textContent='请为拷贝的计划填写不同名称后保存';message.classList.add('error');return;}
  const template=buildBody();
  for(const key of ['github_token','jenkins_user','jenkins_password','testfairy_user','testfairy_password','jira_token','jenkins_api_token']){
    if(template[key]){message.textContent='定时模板不保存凭据。请先用手动构建保存到 Mac Keychain，再清空凭据字段后保存计划。';message.classList.add('error');return;}
    delete template[key];
  }
  const body={name:f.schedule_name.value.trim(),cron:f.schedule_cron.value.trim(),enabled:f.schedule_enabled.checked,stop_after_success:f.schedule_stop_after_success.checked,template};
  if(editing)body.expected_revision=store.revision;
  store.saving=true;btn.disabled=true;message.textContent='正在保存…';message.classList.remove('error');
  try{
    await api(editing?`/api/schedules/${encodeURIComponent(editing)}/update`:'/api/schedules',{method:'POST',body:JSON.stringify(body)});
    toast(editing?'定时任务已更新':'定时任务已创建');
    if(generation===store.editorGeneration){resetForm();message.textContent='计划已保存';nav('schedules',true);}
    await load();
  }catch(e){if(generation===store.editorGeneration){message.textContent=e.message;message.classList.add('error');}toast(e.message,true);}
  finally{store.saving=false;btn.disabled=false;}
}
function edit(row,copy=false){
  store.editorGeneration++;resetBuildForm();store.editing=copy?null:row.id;store.revision=copy?null:row.revision;store.copyName=copy?row.name:null;
  // Defaults only fill legacy omissions; every saved advanced value wins.
  applyBuildBody({...buildDefaults,...row.template});reloadBranches();
  const f=el('build-form').elements;
  f.schedule_name.value=row.name;f.schedule_cron.value=row.cron;f.schedule_enabled.checked=copy?false:row.enabled;
  f.schedule_stop_after_success.checked=copy?true:!!row.stop_after_success;
  el('form-message').textContent=copy?'已拷贝非秘密配置，请改名并核对 cron 后保存；默认停用，不复制运行记录。凭据继续使用既有 Keychain／租户会话。':'';
  el('save-as-schedule').checked=true;updateMode();previewCron();nav('new-build');
}

async function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body||{})});}
async function toggle(row){try{await post(`/api/schedules/${encodeURIComponent(row.id)}/toggle`,{enabled:!row.enabled,expected_revision:row.revision});toast(row.enabled?'已停用':'已启用');await load();}catch(e){toast(e.message,true);}}
async function runNow(row){
  if(!confirm(`立即运行「${row.name}」？\n将创建新的分支和 Jenkins 构建；并发已满时会排队等待。`))return;
  try{const queued=await post(`/api/schedules/${encodeURIComponent(row.id)}/run-now`);toast(`运行记录 #${queued.run_id||'—'} 已排队；调度器在线且有并发名额后才会启动。请在运行记录查看对应的新构建日志`);await load();await showRuns(row);}catch(e){toast(e.message,true);}
}
async function remove(row){
  if(!confirm(`删除定时任务「${row.name}」及其运行记录？\n已经创建的构建任务不受影响。`))return;
  try{await post(`/api/schedules/${encodeURIComponent(row.id)}/delete`);if(store.editing===row.id)resetForm();if(store.runsFor===row.id){store.runsFor=null;el('schedule-runs').hidden=true;}toast('已删除');await load();}catch(e){toast(e.message,true);}
}
async function showRuns(row,silent){
  if(!row)return;store.runsFor=row.id;
  try{
    const data=await api(`/api/schedules/${encodeURIComponent(row.id)}/runs`);
    el('schedule-runs-title').textContent=`RUN HISTORY · ${row.name}`;
    const body=el('schedule-runs-body');body.replaceChildren();
    if(!data.runs.length){const tr=create('tr'),td=create('td','empty-row','还没有运行记录。');td.colSpan=4;tr.append(td);body.append(tr);}
    for(const run of data.runs){
      const tr=create('tr');const status=create('td');status.append(pill(run.task_status||run.status));
      const build=create('td');
      if(run.task_id){build.append(button(run.task_id,()=>openTask(run.task_id),'subtle-btn sched-link'));}else build.textContent=run.status==='PENDING'?'尚未启动：等待调度器或并发名额':'—';
      tr.append(create('td','',fmt(run.planned)),status,build,create('td','',run.note||''));body.append(tr);
    }
    el('schedule-runs').hidden=false;
    if(!silent)el('schedule-runs').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){if(!silent)toast(e.message,true);}
}
async function previewCron(){
  const text=el('build-form').elements.schedule_cron.value.trim(),out=el('cron-preview');
  const generation=++store.previewGeneration;
  if(!text){out.textContent='输入 cron 后在这里预览接下来 5 次运行时间。';out.classList.remove('error');return;}
  try{
    const data=await api('/api/schedules/preview?cron='+encodeURIComponent(text));
    if(generation!==store.previewGeneration)return;
    out.textContent='本机时区 · 接下来 5 次：'+data.next.map(fmt).join('  ·  ');out.classList.remove('error');
  }catch(e){if(generation!==store.previewGeneration)return;out.textContent='✕ '+e.message;out.classList.add('error');}
}
window.scheduleEditor={submit};
el('save-as-schedule').addEventListener('change',updateMode);
el('new-schedule').addEventListener('click',()=>{resetForm();el('save-as-schedule').checked=true;updateMode();nav('new-build');});
el('schedule-cancel').addEventListener('click',resetForm);
el('schedule-runs-close').addEventListener('click',()=>{store.runsFor=null;el('schedule-runs').hidden=true;});
el('build-form').elements.schedule_cron.addEventListener('input',()=>{clearTimeout(store.previewTimer);store.previewTimer=setTimeout(previewCron,400);});
const ready=setInterval(()=>{if(state.csrf){clearInterval(ready);load();setInterval(load,5000);}},300);
})();

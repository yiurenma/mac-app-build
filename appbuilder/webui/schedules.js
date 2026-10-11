'use strict';
// Schedules page. Reuses globals from app.js (api, create, toast, state, nav, setSelected).
(function(){
const el=id=>document.getElementById(id);
const store={list:[],editing:null,runsFor:null,previewTimer:null,seeded:false};
const BASIC=['plugin_branch','gmx_branch','plugin_version','country','project_name'];
function fmt(iso){return iso?String(iso).replace('T',' ').slice(0,16):'—';}
function pill(status){return create('span','pill '+String(status||'').toLowerCase().replace(/_/g,'-'),String(status||'—').replace(/_/g,' '));}
function button(label,handler,cls){const b=create('button',cls||'subtle-btn',label);b.type='button';b.addEventListener('click',handler);return b;}
function openTask(taskId){if(!taskId)return;setSelected(taskId);nav('activity');}

function renderStatus(info,maxParallel,minInterval){
  const box=el('scheduler-status'),s=info||{};
  box.className='diagnose-verdict '+(s.online?'pass':'fail');
  box.textContent=s.online
    ?`调度器在线 · pid ${s.pid} · 并发上限 ${maxParallel}（与手动构建共用）· 最小运行间隔 ${minInterval} 分钟`
    :'调度器离线 · 定时任务不会触发。请在终端运行 ./app-build scheduler install（或临时用 ./app-build scheduler run）';
}
function renderRows(){
  const body=el('schedules-body');body.replaceChildren();
  if(!store.list.length){const tr=create('tr'),td=create('td','empty-row','还没有定时任务。在上方填写 cron 和构建参数后创建。');td.colSpan=6;tr.append(td);body.append(tr);return;}
  for(const row of store.list){
    const tr=create('tr',row.enabled?'':'sched-off');
    const name=create('td');name.append(create('strong','',row.name),create('small','',`${row.template.country} · ${row.template.project_name} · ${row.template.plugin_version}`));
    const cronCell=create('td');cronCell.append(create('span','sched-cron',row.cron));
    const next=create('td','',row.enabled?fmt(row.next_run):'已停用');
    const last=create('td');
    if(row.last_run){
      const shown=row.last_run.task_status||row.last_run.status;
      const p=pill(shown);if(row.last_run.task_id){p.classList.add('sched-link');p.title='查看这次构建';p.addEventListener('click',()=>openTask(row.last_run.task_id));}
      last.append(p,create('small','',' '+fmt(row.last_run.planned)));
      if(row.last_run.note)last.append(create('small','',' · '+row.last_run.note));
    }else last.textContent='—';
    const enabled=create('td','',row.enabled?'ON':'OFF');
    const actions=create('td');const box=create('div','sched-actions');
    box.append(
      button(row.enabled?'停用':'启用',()=>toggle(row)),
      button('立即运行',()=>runNow(row)),
      button('编辑',()=>edit(row)),
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
function formBody(){
  const f=el('schedule-form').elements,template={};
  for(const k of BASIC)template[k]=f[k].value.trim();
  template.visible=f.visible.checked;
  return {name:f.name.value.trim(),cron:f.cron.value.trim(),enabled:f.enabled.checked,template};
}
function resetForm(){
  const f=el('schedule-form');f.reset();f.elements.edit_id.value='';store.editing=null;
  el('schedule-submit').textContent='＋ 创建定时任务';el('schedule-cancel').hidden=true;
  seedFromBuildForm(true);el('cron-preview').textContent='输入 cron 后在这里预览接下来 5 次运行时间。';el('cron-preview').classList.remove('error');
}
function seedFromBuildForm(force){
  if(store.seeded&&!force)return;const source=document.getElementById('build-form');if(!source)return;
  for(const k of BASIC){const v=source.elements[k]&&source.elements[k].value;if(v)el('schedule-form').elements[k].value=v;}
  store.seeded=true;
}
async function submit(evt){
  evt.preventDefault();const message=el('schedule-message'),btn=el('schedule-submit'),editing=store.editing;
  const body=formBody();btn.disabled=true;message.textContent='正在保存…';message.classList.remove('error');
  try{
    if(editing){await api(`/api/schedules/${encodeURIComponent(editing)}/update`,{method:'POST',body:JSON.stringify(body)});}
    else{await api('/api/schedules',{method:'POST',body:JSON.stringify(body)});}
    toast(editing?'定时任务已更新':'定时任务已创建');message.textContent='';resetForm();await load();
  }catch(e){message.textContent=e.message;message.classList.add('error');toast(e.message,true);}
  finally{btn.disabled=false;}
}
function edit(row){
  const f=el('schedule-form').elements;store.editing=row.id;f.edit_id.value=row.id;
  f.name.value=row.name;f.cron.value=row.cron;for(const k of BASIC)f[k].value=row.template[k]||'';
  f.visible.checked=Boolean(row.template.visible);f.enabled.checked=row.enabled;
  el('schedule-submit').textContent='✓ 保存修改';el('schedule-cancel').hidden=false;previewCron();
  document.getElementById('schedules').scrollIntoView({behavior:'smooth',block:'start'});
}
async function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body||{})});}
async function toggle(row){try{await post(`/api/schedules/${encodeURIComponent(row.id)}/toggle`,{enabled:!row.enabled});toast(row.enabled?'已停用':'已启用');await load();}catch(e){toast(e.message,true);}}
async function runNow(row){
  if(!confirm(`立即运行「${row.name}」？\n将创建新的分支和 Jenkins 构建；并发已满时会排队等待。`))return;
  try{await post(`/api/schedules/${encodeURIComponent(row.id)}/run-now`);toast('已加入队列，调度器会在下一轮（约 15 秒内）启动');setTimeout(load,1500);}catch(e){toast(e.message,true);}
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
      if(run.task_id){const a=create('span','sched-link',run.task_id);a.addEventListener('click',()=>openTask(run.task_id));build.append(a);}else build.textContent='—';
      tr.append(create('td','',fmt(run.planned)),status,build,create('td','',run.note||''));body.append(tr);
    }
    el('schedule-runs').hidden=false;
    if(!silent)el('schedule-runs').scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(e){if(!silent)toast(e.message,true);}
}
async function previewCron(){
  const text=el('schedule-form').elements.cron.value.trim(),out=el('cron-preview');
  if(!text){out.textContent='输入 cron 后在这里预览接下来 5 次运行时间。';out.classList.remove('error');return;}
  try{
    const data=await api('/api/schedules/preview?cron='+encodeURIComponent(text));
    out.textContent='接下来 5 次：'+data.next.map(fmt).join('  ·  ');out.classList.remove('error');
  }catch(e){out.textContent='✕ '+e.message;out.classList.add('error');}
}
el('schedule-form').addEventListener('submit',submit);
el('schedule-cancel').addEventListener('click',resetForm);
el('schedule-runs-close').addEventListener('click',()=>{store.runsFor=null;el('schedule-runs').hidden=true;});
el('schedule-form').elements.cron.addEventListener('input',()=>{clearTimeout(store.previewTimer);store.previewTimer=setTimeout(previewCron,400);});
// app.js fetches the CSRF token asynchronously; start once it is available.
const ready=setInterval(()=>{if(state.csrf){clearInterval(ready);seedFromBuildForm(false);load();setInterval(load,5000);}},300);
})();

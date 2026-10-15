'use strict';
// 概览: running build, last delivery, things needing attention, schedules, automation.
// Reuses api/create/formatDate/nav/state from app.js. Text via textContent; links only https.
(function(){
const el=id=>document.getElementById(id);
const STATE={done:['✓','done'],current:['●','current'],waiting:['○','waiting']};
const STATUS={FAILED:'失败',NEEDS_ATTENTION:'需要处理',TIMEOUT:'超时',INTERRUPTED:'中断'};
const STAGE={INIT:'启动',PLUGIN_PREPARE:'Plugin 代码准备',PLUGIN_CHECKOUT:'Plugin 代码准备',PLUGIN_EDIT:'Plugin 代码准备',PLUGIN_PUSH:'Plugin 推送',
  PLUGIN_JENKINS:'Plugin 构建',GMX_CHECKOUT:'GMX 代码准备',GMX_EDIT:'GMX 代码准备',GMX_LOCK:'GMX 代码准备',GMX_PUSH:'GMX 推送',GMX_JENKINS:'GMX 构建',TESTFAIRY_LOOKUP:'取 App 链接'};

function line(...parts){const d=create('div','ov-line');for(const p of parts)if(p!=null)d.append(typeof p==='string'?document.createTextNode(p):p);return d;}
function link(url,text){
  if(typeof url!=='string'||!url.startsWith('https://'))return document.createTextNode(text||'—');
  const a=create('a','',text||url);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;
}
function go(target,text){const b=create('button','text-btn',text);b.type='button';b.addEventListener('click',()=>nav(target));return b;}

function renderRunning(r){
  const box=el('ov-running');
  if(!r){box.replaceChildren(document.createTextNode('现在没有在跑的构建。'));return;}
  const list=create('ol','ov-steps');
  for(const s of r.steps){const [mark,cls]=STATE[s.state]||STATE.waiting;const li=create('li',cls);li.append(create('span','ov-mark',mark),document.createTextNode(s.label));list.append(li);}
  box.replaceChildren(line(r.task_id+' · 开始于 '+formatDate(r.created)),list,go('activity','看实时日志 →'));
}

function renderLast(l){
  const box=el('ov-last');
  if(!l){box.replaceChildren(document.createTextNode('还没有成功的构建。'));return;}
  const jira=l.jira_status==='BASELINE'?'首次运行，只记录起点':l.jira_status==='DRY_RUN'?'演练：'+(l.tickets.join('、')||'没有单'):
    l.tickets.length?l.tickets.join('、')+' → 已更新':l.jira_status?'没有需要更新的单':'未回填';
  const mail=l.mail_status==='SENT'?'已发 '+l.mail_to+' 人':l.mail_status?'未发送':'未开启';
  box.replaceChildren(line('成功 · '+formatDate(l.created)+' · '+l.task_id),
    line('App 链接：',link(l.link,l.link?'打开':'没有')),line('Jira：'+jira),line('邮件：'+mail));
}

function renderAttention(items){
  const box=el('ov-attention'),list=el('ov-attention-list');
  box.hidden=!items.length;
  list.replaceChildren(...items.map(a=>{
    const li=create('li');
    if(a.kind==='task')li.append(document.createTextNode(a.task_id+' '+(STATUS[a.status]||a.status)+'（'+(STAGE[a.stage]||a.stage)+'）：'+(a.text||'没有记录原因')+' '),go('history','查看并处理 →'));
    else if(a.kind==='jira')li.append(document.createTextNode(a.key+' 状态需要手动改：'+a.text+' '),go('delivery','查看 →'));
    else li.append(document.createTextNode('定时任务「'+a.name+'」错过 '+a.count+' 次（Mac 当时可能在睡眠），已补跑最近一次 '),go('schedules','查看 →'));
    return li;}));
}

function renderSchedules(s){
  const box=el('ov-schedules'),on=s.items.filter(i=>i.enabled);
  box.replaceChildren(line(s.online?'● 调度器运行中':'● 调度器离线'),
    ...(on.length?on.map(i=>line(i.name+' · '+i.cron+' · 下次 '+(i.next_run?formatDate(i.next_run):'—'))):[line(s.items.length?'所有定时任务都已暂停':'还没有定时任务')]));
  box.firstChild.className='ov-line '+(s.online?'ok':'bad');
}

function renderAuto(a){
  const yes=v=>v?'开':'关';
  el('ov-auto').replaceChildren(
    line('Jira 回填：'+(a.jira_enabled?(a.jira_dry_run?'开（演练模式，不真改）':'开'):'关')),
    line('邮件：'+(a.mail_enabled?'开，成功和失败都发'+(a.mail_failure_once?'（同一原因只发一次）':''):'关')));
}

async function load(){
  try{
    const o=await api('/api/overview');
    renderRunning(o.running);renderLast(o.last);renderAttention(o.attention||[]);
    renderSchedules(o.schedules||{items:[],online:false});renderAuto(o.automation||{});
  }catch(e){el('ov-running').textContent='读取概览失败：'+e.message;}
}

function init(){
  if(!el('overview'))return;
  document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>nav(b.dataset.go)));
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);load();setInterval(load,10000);}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

'use strict';
// 交付记录: Jira tickets with their App link, and every email. Reuses api/create/formatDate/state from app.js.
// All text goes in through textContent; links only when they are https.
(function(){
const el=id=>document.getElementById(id);
let kind='tickets',timer=null,generation=0;
const RESULT={DONE:['已完成','success'],MANUAL:['状态需手动改','manual'],DRY_RUN:['演练','dry'],ERROR:['失败','error']};
const MAIL_KIND={success:'新包',failure:'构建失败',test:'测试'};
const MAIL_STATUS={SENT:['已发送','sent'],FAILED:['失败','error']};

function link(url,text){
  if(typeof url!=='string'||!url.startsWith('https://'))return create('span','',text||'—');
  const a=create('a','',text||url);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;
}
function cell(...parts){const td=create('td');for(const p of parts)if(p!=null)td.append(typeof p==='string'?document.createTextNode(p):p);return td;}
function small(text){return text?create('small','',text):null;}
function pill(pair){return create('span','pill '+pair[1],pair[0]);}
function when(at){try{return formatDate(at);}catch(e){return at||'';}}

// Jira report steps are short English words; show them in plain Chinese.
function step(s){s=s.replace('[dry-run] ','');
  if(s.startsWith('comment'))return '评论';if(s.startsWith('assign'))return '转给 Reporter';
  if(s.startsWith('transition'))return s.replace(/^transition( -> )?/,'改状态 ').replace('自动选:','自动选');return s;}
function head(cols){const tr=create('tr');for(const c of cols)tr.append(create('th','',c));el('delivery-head').replaceChildren(tr);}

function renderTickets(data){
  head(['时间','Jira','做了什么','App 链接','来自构建','结果']);
  const base=(data.jira_base||'').replace(/\/$/,'');
  const rows=data.items.map(t=>{
    const tr=create('tr');
    tr.append(cell(when(t.at)),
      cell(base?link(base+'/browse/'+encodeURIComponent(t.key),t.key):t.key,small(t.summary)),
      cell((t.steps||[]).map(step).join(' · ')||'—',small(t.note)),
      cell(link(t.link,t.link?'打开':'—')),
      cell(t.task_id,small(t.build?'GMX #'+t.build:'')),
      cell(pill(RESULT[t.result]||[t.result,'dry'])));
    return tr;});
  el('delivery-body').replaceChildren(...rows);
  return rows.length;
}

function renderMails(data){
  head(['时间','类型','主题','收件人','来自构建','结果']);
  const rows=data.items.map(m=>{
    const tr=create('tr'),body=create('details');
    body.append(create('summary','','查看正文'),create('pre','',m.body||''));
    tr.append(cell(when(m.at)),cell(MAIL_KIND[m.kind]||m.kind||''),cell(m.subject||'',m.body?body:null),
      cell((m.to||[]).join('、')),cell(m.task_id||'—'),
      cell(pill(MAIL_STATUS[m.status]||[m.status||'',''] ),small(m.reason)));
    return tr;});
  el('delivery-body').replaceChildren(...rows);
  return rows.length;
}

async function load(){
  const mine=++generation,q=el('delivery-search').value.trim();
  el('delivery-status').textContent='读取中…';
  try{
    const data=await api('/api/delivery/'+kind+'?q='+encodeURIComponent(q));
    if(mine!==generation)return;
    const n=kind==='tickets'?renderTickets(data):renderMails(data);
    el('delivery-status').textContent=n?('共 '+n+' 条'+(n>=300?'（只显示最近 300 条）':'')):
      (q?'没有匹配的记录':(kind==='tickets'?'还没有 Jira 更新记录。构建成功并回填 Jira 后会出现在这里。':'还没有邮件记录。从这个版本开始发出的邮件都会记在这里。'));
  }catch(e){if(mine===generation)el('delivery-status').textContent='读取失败：'+e.message;}
}

function pick(next){
  kind=next;
  document.querySelectorAll('[data-delivery]').forEach(b=>{const on=b.dataset.delivery===next;b.classList.toggle('active',on);b.setAttribute('aria-selected',String(on));});
  load();
}

function init(){
  if(!el('delivery'))return;
  document.querySelectorAll('[data-delivery]').forEach(b=>b.addEventListener('click',()=>pick(b.dataset.delivery)));
  el('delivery-search').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,300);});
  // Load when the panel is opened (it starts collapsed), and keep it fresh while open.
  new MutationObserver(()=>{if(!el('delivery').classList.contains('collapsed'))load();})
    .observe(el('delivery'),{attributes:true,attributeFilter:['class']});
  setInterval(()=>{if(typeof state!=='undefined'&&state.csrf&&!el('delivery').classList.contains('collapsed'))load();},30000);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

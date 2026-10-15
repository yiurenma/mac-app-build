'use strict';
// Jira panel. Reuses globals from app.js (api, create, toast, formatDate).
(function(){
const el=id=>document.getElementById(id);
const form=()=>el('jira-form').elements;
const out=text=>{el('jira-result').textContent=text;};
const TOKEN_TEXT={keychain:'已保存在 Mac 钥匙串',env:'使用环境变量 JIRA_TOKEN','':'尚未保存 Token'};

function fill(data){
  const f=form(),s=data.settings;
  f.enabled.checked=!!s.enabled;f.dry_run.checked=!!s.dry_run;f.base_url.value=s.base_url||'';
  f.transition_name.value=s.transition_name||'';f.assign_to_reporter.checked=!!s.assign_to_reporter;
  f.use_pr_lookup.checked=!!s.use_pr_lookup;f.key_pattern.value=s.key_pattern||'';
  f.first_run_commits.value=s.first_run_commits??0;f.max_tickets_per_run.value=s.max_tickets_per_run??40;
  f.flavor.value=s.flavor||'server';f.transition_fields.value=s.transition_fields||'';f.token.value='';
  f.mail_enabled.checked=!!s.mail_enabled;f.mail_issue.value=s.mail_issue||'';f.mail_users.value=s.mail_users||'';
  el('jira-token-status').textContent=TOKEN_TEXT[data.token_source]??'';
  const b=el('build-jira-status');
  if(b)b.textContent='Jira 回填：'+(s.enabled?(s.dry_run?'已启用（演练模式，只记录不修改 Jira）':'已启用'):'未启用')+' · Token：'+(TOKEN_TEXT[data.token_source]??'未知')+
    ' · 邮件通知：'+(s.mail_enabled?'已开启':'未开启');
}

async function loadSettings(){
  try{fill(await api('/api/jira/settings'));}catch(e){out('读取 Jira 设置失败：'+e.message);}
}

async function loadTasks(){
  const select=el('jira-preview-form').elements.task;
  try{
    const res=await api('/api/tasks');
    const keep=select.value;
    select.replaceChildren(create('option','','请选择已成功的构建'));
    for(const t of (res.tasks||[]).filter(t=>t.status==='SUCCESS')){
      const o=create('option','',`${t.id} · ${t.project||''} ${t.plugin_branch||''} · ${formatDate(t.created)}`);
      o.value=t.id;select.append(o);
    }
    if(keep)select.value=keep;
  }catch(e){/* task list is optional here */}
}

async function save(){
  const f=form(),body={
    enabled:f.enabled.checked,dry_run:f.dry_run.checked,base_url:f.base_url.value.trim(),
    transition_name:f.transition_name.value.trim(),assign_to_reporter:f.assign_to_reporter.checked,
    use_pr_lookup:f.use_pr_lookup.checked,key_pattern:f.key_pattern.value.trim(),
    first_run_commits:parseInt(f.first_run_commits.value,10),max_tickets_per_run:parseInt(f.max_tickets_per_run.value,10),
    flavor:f.flavor.value,transition_fields:f.transition_fields.value,
    mail_enabled:f.mail_enabled.checked,mail_issue:f.mail_issue.value.trim(),mail_users:f.mail_users.value};
  if(Number.isNaN(body.first_run_commits))delete body.first_run_commits;
  if(Number.isNaN(body.max_tickets_per_run))delete body.max_tickets_per_run;
  if(f.token.value)body.token=f.token.value;
  const button=el('jira-save');button.disabled=true;
  try{
    fill(await api('/api/jira/settings',{method:'POST',body:JSON.stringify(body)}));
    toast('Jira 设置已保存');out('设置已保存'+(body.token?'，Token 已存入 Mac 钥匙串':'')+'。建议点“测试连接”确认。');
  }catch(e){toast(e.message,true);out('保存失败：'+e.message);}
  finally{f.token.value='';button.disabled=false;}
}

async function testConnection(){
  const button=el('jira-test-connection');button.disabled=true;out('正在连接 Jira…（只读）');
  try{
    const r=await api('/api/jira/test-connection',{method:'POST',body:'{}'});
    out('连接成功\n当前身份：'+r.user+(r.login?' ('+r.login+')':''));
  }catch(e){out('连接失败：'+e.message);}
  finally{button.disabled=false;}
}

async function lookup(){
  const key=el('jira-ticket-form').elements.key.value.trim().toUpperCase();
  const button=el('jira-lookup');button.disabled=true;out('正在查询 '+key+'…（只读）');
  try{
    const r=await api('/api/jira/ticket',{method:'POST',body:JSON.stringify({key})});
    if(!r.found){out(r.key+'\n'+r.message);return;}
    out(r.key+' · '+r.summary+'\n当前状态：'+r.status+'\nReporter：'+r.reporter+
      '\n当前可流转的状态：'+(r.transitions.length?r.transitions.join('、'):'无')+
      '\n问题类型：'+(r.issue_type||'未知')+
      '\n能否流转到“'+r.transition_name+'”：'+(r.can_transition?'可以':'当前状态下不可以（请核对流转名称，或该 ticket 状态不对）')+
      ((r.required_fields||[]).length?'\n流转时必填字段：\n'+r.required_fields.map(x=>'  '+x.name+(x.configured?'（已配置值）':x.auto?'（未配置，将自动选第一个：'+x.auto+'）':'（还没配置值，需手动）')+(x.allowed.length?'  可选：'+x.allowed.join('、'):'')).join('\n'):'')+
      ((r.problems||[]).length?'\n需要处理：'+r.problems.join('；'):''));
  }catch(e){out('查询失败：'+e.message);}
  finally{button.disabled=false;}
}

async function mailTest(){
  const f=form(),button=el('jira-mail-test');button.disabled=true;out('正在通过 Jira 发送测试邮件…');
  try{
    const r=await api('/api/jira/mail-test',{method:'POST',body:JSON.stringify({mail_issue:f.mail_issue.value.trim(),mail_users:f.mail_users.value})});
    out('已提交给 Jira，请收件人查收邮箱（通常 1–5 分钟）\n公告单：'+r.issue+'\n收件人：'+r.to.join('、')+'\n主题：'+r.subject+
      (r.from_task?'\n内容取自构建：'+r.from_task:'')+
      '\n\n本次请求（不含 Token）：\n'+(r.request||''));
  }catch(e){out('测试邮件失败：'+e.message);}
  finally{button.disabled=false;}
}

function describe(r){
  if(r.status==='SKIPPED')return '无法演练：'+r.reason;
  const lines=['演练结果（只读，未修改 Jira）','查看的提交数：'+r.commits_scanned,'PR 查询：'+r.pr_lookup,''];
  if(!r.tickets.length&&!(r.ignored_keys||[]).length)lines.push('没有找到任何 Jira 号。请确认这些提交/PR 的标题、分支名或描述里写了 Jira 号。');
  for(const t of r.tickets){
    lines.push(t.key+'（来源：'+((r.ticket_sources||{})[t.key]||'未知')+'）');
    for(const step of t.steps)lines.push('  '+step.replace('[dry-run] ',''));
  }
  if((r.ignored_keys||[]).length)lines.push('','在 Jira 里查不到、已忽略：'+r.ignored_keys.join('、'));
  if((r.ambiguous_commits||[]).length)lines.push('','一个提交里有多个 Jira 号、无法确定归属，已跳过：\n  '+r.ambiguous_commits.join('\n  '));
  if((r.needs_manual||[]).length)lines.push('','需要手动改状态（评论和转派已完成）：\n  '+r.needs_manual.map(e=>e.key+'：'+e.reason).join('\n  '));
  if((r.errors||[]).length)lines.push('','处理出错：\n  '+r.errors.map(e=>e.key+'：'+e.error).join('\n  '));
  return lines.join('\n');
}

async function preview(){
  const f=el('jira-preview-form').elements,button=el('jira-preview');
  if(!f.task.value){out('请先选择一个已成功的历史构建');return;}
  button.disabled=true;out('正在演练…（只读）');
  try{
    const r=await api('/api/jira/preview',{method:'POST',body:JSON.stringify({task_id:f.task.value,recent:parseInt(f.recent.value,10)})});
    out(describe(r));
  }catch(e){out('演练失败：'+e.message);}
  finally{button.disabled=false;}
}

function init(){
  if(!el('jira-form'))return;
  el('jira-save').addEventListener('click',save);
  el('jira-test-connection').addEventListener('click',testConnection);
  el('jira-lookup').addEventListener('click',lookup);
  el('jira-preview').addEventListener('click',preview);
  el('jira-mail-test').addEventListener('click',mailTest);
  // app.js obtains the CSRF token during init(); wait for it before the first request.
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);loadSettings();loadTasks();setInterval(()=>{loadTasks();if(!el('jira-form').contains(document.activeElement))loadSettings();},30000);}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

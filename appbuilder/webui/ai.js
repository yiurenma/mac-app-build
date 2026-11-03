'use strict';
// Settings page: offline AI card. Reuses api/toast/state from app.js.
(function(){
const el=id=>document.getElementById(id);
let timer=null;

function render(s){
  const sel=el('ai-model');
  if(!sel.options.length){
    for(const [k,m] of Object.entries(s.models)){const o=document.createElement('option');o.value=k;o.textContent=m.label+' · '+m.licence;sel.appendChild(o);}
  }
  if(s.model&&s.models[s.model])sel.value=s.model;
  const m=s.models[sel.value];
  el('ai-model-help').textContent='点“一键安装”会装运行库、下载模型、核对校验值并试运行。模型放在 '+s.model_dir+'，更新工具不会丢。模型说明：'+(m?m.source:'');
  const f=el('ai-form').elements;
  const cop=s.provider==='copilot';
  f.provider.value=cop?'copilot':'local';
  el('ai-local-block').hidden=cop;el('ai-copilot-block').hidden=!cop;
  el('ai-provider-help').textContent=cop?'VS Code 插件：'+(s.copilot&&s.copilot.note||'未知'):'模型在这台 Mac 上运行，不联网。';
  f.enabled.checked=!!s.enabled;f.tester_note.checked=s.features.tester_note!==false;f.failure_hint.checked=s.features.failure_hint!==false;f.pr_context.checked=s.features.pr_context!==false;f.auto_start_vscode.checked=s.features.auto_start_vscode!==false;
  el('ai-pr-context-row').hidden=!cop;el('ai-autostart-row').hidden=!cop;
  const parts=[];
  if(cop){
    parts.push('VS Code 插件：'+((s.copilot&&s.copilot.ok)?'在运行':'未检测到'));
    parts.push('状态：'+(s.enabled&&s.copilot&&s.copilot.ok?'已启用':'未启用'));
  }else{
    parts.push('运行库：'+(s.runtime?'已安装 '+s.runtime:'未安装'));
    parts.push('模型：'+(s.model_ok?s.model_path:(s.model_note||'未安装')));
    parts.push('状态：'+(s.enabled&&s.model_ok&&s.runtime?'已启用':'未启用'));
  }
  el('ai-status').textContent=parts.join(' · ');
  const j=s.job||{};
  el('ai-install').disabled=el('ai-use-file').disabled=!!j.running;
  const bar=el('ai-progress');
  bar.hidden=!(j.running&&j.progress!=null);if(j.progress!=null)bar.value=j.progress;
  if(j.log&&j.log.length){
    el('ai-log').textContent=(j.running?'进行中：'+j.step+(j.progress!=null?' '+j.progress+'%':'')+'\n':'')+j.log.join('\n')+(j.error?'\n\n'+j.error:'');
  }
  clearTimeout(timer);
  if(j.running)timer=setTimeout(load,2000);
}

async function load(){
  try{render(await api('/api/ai/status'));}catch(e){el('ai-status').textContent='读取失败：'+e.message;}
}
async function post(path,body){
  try{render(await api(path,{method:'POST',body:JSON.stringify(body)}));return true;}
  catch(e){toast(e.message,true);el('ai-log').textContent=e.message;return false;}
}

function init(){
  if(!el('ai-form'))return;
  el('ai-install').addEventListener('click',()=>post('/api/ai/install',{model:el('ai-model').value}));
  el('ai-use-file').addEventListener('click',()=>post('/api/ai/use-file',{path:el('ai-path').value}));
  for(const name of ['enabled','tester_note','failure_hint','pr_context','auto_start_vscode']){
    el('ai-form').elements[name].addEventListener('change',async e=>{
      if(!await post('/api/ai/settings',{[name]:e.target.checked}))e.target.checked=!e.target.checked;
      else toast('已保存');
    });
  }
  el('ai-try').addEventListener('click',async()=>{
    el('ai-log').textContent='试运行中（首次加载模型约 10–30 秒）…';
    try{const r=await api('/api/ai/try',{method:'POST',body:'{}'});
      const h=r.failure_hint;
      el('ai-log').textContent='示例 PR → For testers:\n  '+r.tester_note+'\n\n示例失败（签名证书找不到）→\n  '+(typeof h==='string'?h:'Likely cause: '+h.cause+'\n  Try: '+h.fix);
    }catch(e){el('ai-log').textContent=e.message;}
  });
  el('ai-provider').addEventListener('change',async e=>{
    if(await post('/api/ai/settings',{provider:e.target.value}))toast('已保存');
  });
  async function tryPr(send){
    const url=el('ai-pr-url').value.trim();
    if(!url){el('ai-log').textContent='先粘贴一个 PR 链接';return;}
    el('ai-log').textContent=send?'读取 PR 并交给 AI（Copilot 第一次可能要你在 VS Code 里点“允许”）…':'读取 PR…';
    try{
      const r=await api('/api/ai/try-pr',{method:'POST',body:JSON.stringify({url,send})});
      const st=r.stats||{};
      const scope=r.full_context?('标题、描述、'+(st.commits||0)+' 条提交、'+(st.files||0)+' 个文件、'+(st.patched||0)+' 段代码差异'):'标题和描述';
      el('ai-log').textContent=(send?'For testers:\n  '+r.tester_note+'\n\n':'')+'发给：'+r.to+'；内容：'+scope+'；共约 '+r.chars+' 字'+(r.cut?'（下面只显示前 6000 字）':'')+'\n\n'+r.sent;
    }catch(e){el('ai-log').textContent=e.message;}
  }
  el('ai-pr-try').addEventListener('click',()=>tryPr(true));
  el('ai-form').addEventListener('submit',e=>e.preventDefault());
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);load();}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

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
  f.enabled.checked=!!s.enabled;f.tester_note.checked=s.features.tester_note!==false;f.failure_hint.checked=s.features.failure_hint!==false;
  const parts=[];
  parts.push('运行库：'+(s.runtime?'已安装 '+s.runtime:'未安装'));
  parts.push('模型：'+(s.model_ok?s.model_path:(s.model_note||'未安装')));
  parts.push('状态：'+(s.enabled&&s.model_ok&&s.runtime?'已启用':'未启用'));
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
  for(const name of ['enabled','tester_note','failure_hint']){
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
  el('ai-form').addEventListener('submit',e=>e.preventDefault());
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);load();}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

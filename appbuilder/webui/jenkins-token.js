'use strict';
// "00 Jenkins 登录": staff ID + one API token per Jenkins, saved to the Mac Keychain.
(function(){
const el=id=>document.getElementById(id);
const status=text=>{el('jenkins-token-status').textContent=text;};

function describe(s){
  const t=s.tokens||{};
  return '工号：'+(s.user||'未保存')+' · Plugin Token：'+(t.plugin?'已保存':'未保存')+' · Platform Token：'+(t.gmx?'已保存':'未保存');
}

async function load(){
  try{const s=await api('/api/jenkins-token/status');el('jenkins-token-user').placeholder=s.user||'例如 45012345';status(describe(s));}
  catch(e){status('读取 Jenkins Token 状态失败：'+e.message);}
}

async function save(){
  const body={user:el('jenkins-token-user').value.trim(),plugin_token:el('jenkins-token-plugin').value,gmx_token:el('jenkins-token-gmx').value};
  const button=el('jenkins-token-save');button.disabled=true;
  try{const s=await api('/api/jenkins-token/save',{method:'POST',body:JSON.stringify(body)});
    el('jenkins-token-user').value='';el('jenkins-token-user').placeholder=s.user||'';
    status('已保存到 Mac 钥匙串。'+describe(s));toast('Jenkins Token 已保存');}
  catch(e){status('保存失败：'+e.message);toast(e.message,true);}
  finally{el('jenkins-token-plugin').value='';el('jenkins-token-gmx').value='';button.disabled=false;}
}

async function test(){
  const button=el('jenkins-token-test');button.disabled=true;status('正在用 Token 询问两台 Jenkins…（只读）');
  try{const r=await api('/api/jenkins-token/test',{method:'POST',body:'{}'});
    status(Object.values(r).map(x=>x.label+'：'+(x.ok?'通过，':'不可用，')+x.message).join('；'));}
  catch(e){status('测试失败：'+e.message);}
  finally{button.disabled=false;}
}

function init(){
  if(!el('jenkins-token-box'))return;
  el('jenkins-token-save').addEventListener('click',save);
  el('jenkins-token-test').addEventListener('click',test);
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);load();}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

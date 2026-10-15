'use strict';
// Settings page: GitHub token and Jenkins browser sign-in, saved once to the Keychain.
// Reuses globals from app.js (api, toast, state). Secrets are never read back.
(function(){
const el=id=>document.getElementById(id);

function show(r){
  el('credentials-status').textContent='GitHub Token：'+(r.github?'已保存':'未保存')+
    ' · 浏览器登录：'+(r.jenkins_user?'已保存（'+r.jenkins_user+'）':'未保存');
}

async function load(){
  try{show(await api('/api/credentials/status'));}catch(e){el('credentials-status').textContent='读取失败：'+e.message;}
}

async function save(){
  const f=el('credentials-form').elements,button=el('credentials-save');
  const body={github_token:f.github_token.value,jenkins_user:f.jenkins_user.value,jenkins_password:f.jenkins_password.value};
  button.disabled=true;
  try{
    show(await api('/api/credentials/save',{method:'POST',body:JSON.stringify(body)}));
    toast('已保存到 Mac 钥匙串');
  }catch(e){toast(e.message,true);el('credentials-status').textContent='保存失败：'+e.message;}
  finally{f.github_token.value='';f.jenkins_password.value='';button.disabled=false;}
}

function init(){
  if(!el('credentials-form'))return;
  el('credentials-save').addEventListener('click',save);
  el('credentials-form').addEventListener('submit',e=>e.preventDefault());
  // app.js fetches the CSRF token during init(); wait for it before the first request.
  const wait=setInterval(()=>{if(typeof state!=='undefined'&&state.csrf){clearInterval(wait);load();}},300);
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

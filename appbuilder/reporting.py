"""Whitelisted per-step result summaries; never serialize raw params/runtime."""
import re
from .telemetry import safe_text
from .edits import XCCONFIGS, ARTIFACTS

STEPS=('PLUGIN_PREPARE','PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH','PLUGIN_JENKINS',
       'GMX_CHECKOUT','GMX_EDIT','GMX_LOCK','GMX_PUSH','GMX_JENKINS','TESTFAIRY_LOOKUP')


def _value(value):
    if value is None or value=='':return '未记录'
    if isinstance(value,bool):return '开启' if value else '关闭'
    if isinstance(value,(str,int,float)):return safe_text(value)
    return '未记录'


def _pairs(values):return {key:_value(value) for key,value in values.items()}


def step_summaries(params,state,row):
    rt=state.get('runtime',{})
    from .reuse_flow import effective_params
    params=effective_params(params,rt);gmx=params.get('gmx_parameters',{})
    inputs={
        'PLUGIN_PREPARE':{'仓库':params.get('plugin_url'),'来源分支':params.get('plugin_source'),'目标分支':params.get('plugin_branch'),'目标版本':params.get('plugin_version')},
        'PLUGIN_CHECKOUT':{'仓库':params.get('plugin_url'),'来源分支':params.get('plugin_source'),'目标分支':params.get('plugin_branch')},
        'PLUGIN_EDIT':{'目标版本':params.get('plugin_version')},
        'PLUGIN_PUSH':{'目标分支':params.get('plugin_branch'),'基线 SHA':rt.get('plugin_base_sha')},
        'PLUGIN_JENKINS':{'Job URL':params.get('plugin_job'),'目标分支':params.get('plugin_branch')},
        'GMX_CHECKOUT':{'仓库':params.get('gmx_url'),'来源分支':params.get('gmx_source'),'目标分支':params.get('gmx_branch')},
        'GMX_EDIT':{'文件':'mobile-ci.json','Plugin 完整产物版本':rt.get('artifact_version')},
        'GMX_LOCK':{'文件':'mobile-ci.lock','Plugin 完整产物版本':rt.get('artifact_version'),'产物 Build 编号':rt.get('plugin_artifact_number')},
        'GMX_PUSH':{'目标分支':params.get('gmx_branch'),'基线 SHA':rt.get('gmx_base_sha')},
        'GMX_JENKINS':{'Job URL':params.get('gmx_job'),'国家/地区':params.get('country'),'项目':params.get('project'),
                        **{key:gmx.get(key) for key in ('BUILD_TYPE','CUSTOM_FOLDER','SIGNING_REPO_BRANCH','SYNC_UPLOAD','SKIP_TEST','FORCE','KEEP_ALIVE')}},
        'TESTFAIRY_LOOKUP':{'登录入口':params.get('testfairy',{}).get('login_url'),'GMX Job URL':params.get('gmx_job'),
                            'GMX Build 编号':rt.get('gmx_build_number',rt.get('gmx_formal_number')),'GMX SHA':rt.get('gmx_sha'),
                            '目录覆盖':params.get('testfairy',{}).get('folder_path'),'实际 CUSTOM_FOLDER':gmx.get('CUSTOM_FOLDER')},
    }
    outputs={
        'PLUGIN_PREPARE':{'来源 SHA':rt.get('plugin_source_sha'),'构建 SHA':rt.get('plugin_sha'),'原目标 SHA':rt.get('plugin_target_before')},
        'PLUGIN_CHECKOUT':{'基线 SHA':rt.get('plugin_base_sha')},
        'PLUGIN_EDIT':{'逐文件版本修改记录':'未记录（不从目标版本反推文件内容）'},
        'PLUGIN_PUSH':{'提交 SHA':rt.get('plugin_sha')},
        'PLUGIN_JENKINS':{'正式 Build 编号':rt.get('plugin_formal_number'),'产物 Build 编号':rt.get('plugin_artifact_number'),'完整产物版本':rt.get('artifact_version')},
        'GMX_CHECKOUT':{'基线 SHA':rt.get('gmx_base_sha')},
        'GMX_EDIT':{'修改后文件快照':'未记录（已完成状态仅表示该步骤执行完成）'},
        'GMX_LOCK':{'依赖摘要校验':'已记录 SHA256' if rt.get('lock_hashes') else None},
        'GMX_PUSH':{'提交 SHA':rt.get('gmx_sha')},
        'GMX_JENKINS':{'Build 编号':rt.get('gmx_build_number',rt.get('gmx_formal_number')),'构建结果':rt.get('gmx_formal_result'),'上传校验':rt.get('upload_validation')},
        'TESTFAIRY_LOOKUP':{'App 版本':None,'App 构建号':None,'分享链接':None},
    }
    edits=[]
    for change in rt.get('plugin_edits',[]):
        if (isinstance(change,(list,tuple)) and len(change)==3 and change[0] in XCCONFIGS
                and all(isinstance(v,str) and re.fullmatch(r'[0-9][A-Za-z0-9_.-]*',v) for v in change[1:])):
            edits.append(f'{change[0]}: {change[1]} → {change[2]}')
    if edits:outputs['PLUGIN_EDIT']={'实际版本修改':'；'.join(edits)}
    for name in ARTIFACTS:
        old=rt.get('gmx_previous_versions',{}).get(name)
        new=rt.get('gmx_updated_versions',{}).get(name)
        digest=rt.get('lock_hashes',{}).get(name)
        if old is not None:inputs['GMX_EDIT'][name+' 原版本']=old
        if new is not None:outputs['GMX_EDIT'][name+' 已写入版本']=new
        if isinstance(digest,str) and re.fullmatch(r'[a-fA-F0-9]{64}',digest):
            outputs['GMX_LOCK'][name+' SHA256']=digest
    for step,job,numbers in (
        ('PLUGIN_JENKINS',params.get('plugin_job'),{'正式 Build URL':rt.get('plugin_formal_number'),'产物 Build URL':rt.get('plugin_artifact_number')}),
        ('GMX_JENKINS',params.get('gmx_job'),{'Build URL':rt.get('gmx_build_number',rt.get('gmx_formal_number'))})):
        for label,number in numbers.items():
            if isinstance(job,str) and isinstance(number,int) and not isinstance(number,bool) and number>0:
                outputs[step][label]=job.rstrip('/')+'/'+str(number)+'/'
    app=rt.get('testfairy_app')
    if app and row['status']=='SUCCESS':
        from .testfairy import expected_build
        try:
            expected=expected_build(params,rt)
            if all(app.get(key)==expected[key] for key in ('build_url','number','sha','folder')):
                outputs['TESTFAIRY_LOOKUP']={'App 版本':app.get('app_version'),'App 构建号':app.get('app_build'),'分享链接':app.get('share_url')}
        except (ValueError,RuntimeError,KeyError,TypeError):pass
    result=[]
    for kind,label in (('plugin_reuse','PLUGIN_REUSE'),('app_reuse','GMX_REUSE')):
        origin=rt.get(kind)
        if isinstance(origin,dict):
            result.append({'step':label,'status':'已复用','inputs':{},'outputs':_pairs({'来源任务':origin.get('source_task_id'),'原 Job':origin.get('job_url'),'原 Build':origin.get('build_url'),'原 SHA':origin.get('build_sha')}),'error':'未记录','error_label':'错误摘要'})
    for step in STEPS:
        if rt.get('plugin_reuse') and step.startswith('PLUGIN_'):continue
        if rt.get('app_reuse') and step in ('GMX_EDIT','GMX_LOCK','GMX_PUSH','GMX_JENKINS','TESTFAIRY_LOOKUP'):continue
        if params.get('plugin_flow_version') == 2 and step in ('PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH'):continue
        if params.get('plugin_flow_version') != 2 and step == 'PLUGIN_PREPARE':continue
        done=step in state.get('completed',[])
        current=row['stage']==step and row['status']!='SUCCESS'
        status='已完成' if done else row['status'] if current else '未执行'
        if step=='TESTFAIRY_LOOKUP' and not params.get('testfairy',{}).get('enabled'):status='未启用'
        error=rt.get('step_errors',{}).get(step)
        if current:error=row.get('note') or error
        result.append({'step':step,'status':status,'inputs':_pairs(inputs[step]),
                       'outputs':_pairs(outputs[step]),'error':_value(error),
                       'error_label':'历史错误（已恢复）' if done and error else '错误摘要'})
    return result


def render_steps(params,state,row):
    lines=['逐步摘要（白名单字段；密码、Token、会话均不列入）']
    for item in step_summaries(params,state,row):
        lines.append(f"\n{item['step']} · {item['status']}")
        lines.append('输入: '+'；'.join(f'{key}={value}' for key,value in item['inputs'].items()))
        lines.append('输出: '+'；'.join(f'{key}={value}' for key,value in item['outputs'].items()))
        lines.append(item['error_label']+': '+item['error'])
    return '\n'.join(lines)

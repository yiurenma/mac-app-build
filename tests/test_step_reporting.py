import unittest
from unittest.mock import patch
from appbuilder.reporting import step_summaries,render_steps
from appbuilder.telemetry import safe_text

class StepReportingTests(unittest.TestCase):
    def test_success_and_actual_recorded_outputs(self):
        row={'status':'SUCCESS','stage':'FINISHED'}
        state={'completed':['PLUGIN_CHECKOUT','PLUGIN_PUSH'],'runtime':{'plugin_base_sha':'base','plugin_sha':'commit'}}
        items={x['step']:x for x in step_summaries({'plugin_source':'develop','plugin_branch':'release/example'},state,row)}
        self.assertEqual(items['PLUGIN_PUSH']['status'],'已完成')
        self.assertEqual(items['PLUGIN_PUSH']['outputs']['提交 SHA'],'commit')
        self.assertEqual(items['GMX_JENKINS']['status'],'未执行')
    def test_current_failure_and_unexecuted_steps(self):
        row={'status':'NEEDS_ATTENTION','stage':'GMX_JENKINS','note':'HTTP 403'}
        items={x['step']:x for x in step_summaries({'testfairy':{'enabled':True}},{'completed':['PLUGIN_JENKINS'],'runtime':{}},row)}
        self.assertEqual(items['GMX_JENKINS']['error'],'HTTP 403')
        self.assertEqual(items['TESTFAIRY_LOOKUP']['status'],'未执行')
        self.assertEqual(items['GMX_JENKINS']['outputs']['Build 编号'],'未记录')
    def test_missing_values_are_not_invented_from_inputs(self):
        items={x['step']:x for x in step_summaries({'plugin_version':'1.3.999'},{'completed':['PLUGIN_EDIT'],'runtime':{}},{'status':'SUCCESS','stage':'FINISHED'})}
        self.assertIn('未记录',items['PLUGIN_EDIT']['outputs']['逐文件版本修改记录'])
        self.assertNotIn('1.3.999',str(items['PLUGIN_EDIT']['outputs']))
    def test_whitelist_and_errors_remove_secrets_headers_and_query(self):
        params={'password':'PASSWORD_VALUE','github_token':'TOKEN_VALUE','plugin_url':'https://example.com/repo?token=QUERY_SECRET'}
        row={'status':'FAILED','stage':'PLUGIN_CHECKOUT','note':'Authorization: Bearer AUTH_SECRET\nCookie: sid=COOKIE1; other=COOKIE2\npassword=PW_SECRET'}
        state={'completed':[],'runtime':{'credentials':{'password':'RUNTIME_SECRET'}}}
        text=render_steps(params,state,row)
        for secret in ('PASSWORD_VALUE','TOKEN_VALUE','QUERY_SECRET','AUTH_SECRET','COOKIE1','COOKIE2','PW_SECRET','RUNTIME_SECRET'):
            self.assertNotIn(secret,text)
    def test_recovered_historical_error_is_labeled(self):
        state={'completed':['GMX_JENKINS'],'runtime':{'step_errors':{'GMX_JENKINS':'old failure'}}}
        text=render_steps({},state,{'status':'SUCCESS','stage':'FINISHED'})
        self.assertIn('历史错误（已恢复）: old failure',text)
    def test_step_records_sanitized_failure_without_marking_complete(self):
        from appbuilder.runner import step
        state={'runtime':{},'completed':[]}
        with patch('appbuilder.runner.read_state',return_value=state),patch('appbuilder.runner.update_task'),patch('appbuilder.runner.patch_runtime',side_effect=lambda _,**v:state['runtime'].update(v)),patch('appbuilder.runner.complete') as complete:
            with self.assertRaises(ValueError):step('task','GMX_JENKINS',lambda:(_ for _ in ()).throw(ValueError('Cookie: sid=PRIVATE; auth=OTHER')))
        complete.assert_not_called();self.assertNotIn('PRIVATE',state['runtime']['step_errors']['GMX_JENKINS'])
    def test_recorded_file_edits_and_dependency_hashes_are_reported(self):
        from appbuilder.edits import XCCONFIGS,ARTIFACTS
        name=next(iter(ARTIFACTS));file=next(iter(XCCONFIGS))
        state={'completed':['PLUGIN_EDIT','GMX_EDIT','GMX_LOCK'],'runtime':{
            'plugin_edits':[[file,'1.2','1.3']],
            'gmx_previous_versions':{name:'1.2.3'},'gmx_updated_versions':{name:'1.3.2026.7'},
            'lock_hashes':{name:'a'*64}}}
        text=render_steps({},state,{'status':'SUCCESS','stage':'FINISHED'})
        self.assertIn('1.2 → 1.3',text);self.assertIn('已写入版本=1.3.2026.7',text);self.assertIn('a'*64,text)

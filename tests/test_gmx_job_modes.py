"""GMX Jenkins screenshot regression: disabled #1, direct successful #2, and params."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder import store, web
from appbuilder.config import load_config
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention, ensure_bootstrap, formal_build


class FakeField:
    def __init__(self, tag='input', kind='text', value='', options=()):
        self.tag, self.kind, self.value = tag,kind,value
        self.options = options
        self.checked = False
    def evaluate(self, code): return self.tag
    def get_attribute(self, key): return self.kind if key == 'type' else None
    def locator(self, selector):
        if selector!='option': raise AssertionError(selector)
        return self
    def evaluate_all(self, expression):
        return [{'value':t, 'text':t} for t in self.options]
    def select_option(self, value): self.value = value
    def fill(self, value): self.value = value
    def check(self): self.checked = True
    def uncheck(self): self.checked = False
    def is_checked(self): return self.checked
    def input_value(self): return self.value


class ParamChecks(unittest.TestCase):
    def setUp(self):
        self.fields={
            'ENTITIES':FakeField(),
            'SKIP_TEST':FakeField(kind='checkbox'),
            'FORCE':FakeField(kind='checkbox'),
            'KEEP_ALIVE':FakeField(kind='checkbox'),
            'SYNC_UPLOAD':FakeField(kind='checkbox'),
            'BUILD_TYPE':FakeField(tag='select',options=('Develop','Release')),
            'CUSTOM_FOLDER':FakeField(),
            'SIGNING_REPO_BRANCH':FakeField(),
        }
        self.browser=SimpleNamespace(conf={'gmx_fields':{}},
                    _find_control=lambda field, css='': self.fields[field])

    def test_gmx_eight_fields_include_required_flags_and_sync_overrides_ui_default(self):
        cfg=load_config()
        default=web.form_defaults(cfg)
        self.assertEqual((default['gmx_skip_test'],default['gmx_force'],default['gmx_keep_alive']), (True,True,False))
        _,_,_,gmx=web.build_preferences({},cfg,'UK','PayByBank')
        JenkinsBrowser.fill_gmx_parameters(self.browser,'UK','PayByBank',gmx)
        self.assertEqual(self.fields['ENTITIES'].value,'UK')
        self.assertEqual(self.fields['BUILD_TYPE'].value,'Develop')
        self.assertEqual(self.fields['SIGNING_REPO_BRANCH'].value,'master')
        self.assertTrue(self.fields['SYNC_UPLOAD'].checked)  # Jenkins native default False
        self.assertTrue(self.fields['SKIP_TEST'].checked)
        self.assertTrue(self.fields['FORCE'].checked)
        self.assertFalse(self.fields['KEEP_ALIVE'].checked)
        self.assertEqual(self.fields['CUSTOM_FOLDER'].value,'0_A_UK_PayByBank')

    def test_gmx_can_override_javascript_task_flags(self):
        JenkinsBrowser.fill_gmx_parameters(self.browser,'UK','PayByBank',{
            'SYNC_UPLOAD': False, 'SKIP_TEST': False,'FORCE':False,'KEEP_ALIVE':True,
            'BUILD_TYPE':'Release','SIGNING_REPO_BRANCH':'master'})
        self.assertFalse(self.fields['SYNC_UPLOAD'].checked)
        self.assertFalse(self.fields['SKIP_TEST'].checked)
        self.assertFalse(self.fields['FORCE'].checked)
        self.assertTrue(self.fields['KEEP_ALIVE'].checked)

    def test_reject_wrong_type_checkbox_before_submit(self):
        self.fields['FORCE']=FakeField(kind='text')
        with self.assertRaisesRegex(NeedsAttention,'FORCE'):
            JenkinsBrowser.fill_gmx_parameters(self.browser,'UK','PayByBank',{'FORCE':True})

    def test_cli_does_not_change_optional_jenkins_defaults(self):
        self.fields['SKIP_TEST'].checked=True
        self.fields['FORCE'].checked=True
        JenkinsBrowser.fill_gmx_parameters(self.browser,'UK','PayByBank')
        self.assertTrue(self.fields['SKIP_TEST'].checked)
        self.assertTrue(self.fields['FORCE'].checked)

    def test_invalid_extra_flags_web_rejected(self):
        for key in ('gmx_skip_test','gmx_force','gmx_keep_alive'):
            with self.subTest(key=key),self.assertRaises(ValueError):
                web.build_preferences({key:'true'},load_config(),'UK','PayByBank')


class DisabledGMXBootstrap:
    def __init__(self):
        self.stage='build'
        self.numbers=set()
        self.actions=[]
    def wait_job(self,url):pass
    def mode(self,url):return self.stage
    def visit(self,url):return 200
    def build_numbers(self,url):return self.numbers.copy()
    def request_build(self,url,*,with_parameters,country='',project=''):
        self.actions.append(('click',with_parameters,country,project))
        if not with_parameters:
            self.numbers.add(1)
            self.stage='parameters'  # Jenkins first #1 is disabled; exposes form
        else:self.numbers.add(2)
    def detect_new_build(self,url,baseline,minutes=12):
        new=self.numbers-set(baseline)
        assert len(new)==1,new
        return next(iter(new))
    def wait_parameter_mode(self,url):
        assert self.stage=='parameters'
        self.actions.append(('param_form_ready',))
    def wait_result(self,url,num,*,watch_callback=False):
        if num==1:
            raise AssertionError('GMX bootstrap #1 is disabled; no long polling')
        self.actions.append(('watch_formal',num,watch_callback))
        return 'SUCCESS','Finished: SUCCESS'


class GMXBuildState(unittest.TestCase):
    def test_disabled_first_build_skip_long_poll_and_watch_formal_two(self):
        with tempfile.TemporaryDirectory() as root,patch.object(store,'DATA',Path(root)):
            store.save_state('gmx-test',{'completed':[],'runtime':{}})
            browser=DisabledGMXBootstrap()
            with patch('appbuilder.jenkins.read_state',store.read_state),patch('appbuilder.jenkins.patch_runtime',store.patch_runtime):
                ensure_bootstrap(browser,'https://gmx/job/branch/','gmx-test','gmx')
                r=store.read_state('gmx-test')['runtime']
                self.assertEqual(r['gmx_bootstrap_result'],'PARAMETER_FORM_READY')
                self.assertEqual(r['gmx_bootstrap_number'],1)
                num,console=formal_build(browser,'https://gmx/job/branch/','gmx-test','gmx',country='UK',project='PayByBank')
                self.assertEqual(num,2)
                self.assertEqual(console,'Finished: SUCCESS')
                self.assertIn(('watch_formal',2,False),browser.actions)
                self.assertNotIn(('watch_formal',2,True),browser.actions)
                # Re-entry should not create any new build.
                ensure_bootstrap(browser,'https://gmx/job/branch/','gmx-test','gmx')
                formal_build(browser,'https://gmx/job/branch/','gmx-test','gmx',country='UK',project='PayByBank')
                self.assertEqual(len([a for a in browser.actions if a[0]=='click']),2)

    def test_job_already_has_parameters_no_bootstrap_one(self):
        with tempfile.TemporaryDirectory() as root,patch.object(store,'DATA',Path(root)):
            store.save_state('gmx-test',{'completed':[],'runtime':{}})
            browser=DisabledGMXBootstrap();browser.stage='parameters';browser.numbers={1}
            with patch('appbuilder.jenkins.read_state',store.read_state),patch('appbuilder.jenkins.patch_runtime',store.patch_runtime):
                ensure_bootstrap(browser,'https://gmx/job/branch/','gmx-test','gmx')
                num,_=formal_build(browser,'https://gmx/job/branch/','gmx-test','gmx',country='UK',project='PayByBank')
                self.assertEqual(num,2)
                self.assertEqual(browser.actions[0][:2],('click',True))

if __name__=='__main__':unittest.main()

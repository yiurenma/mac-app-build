"""v0.6.3: all visible defaults have a genuine task-scoped backend effect."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appbuilder import store, web
from appbuilder.config import load_config
from appbuilder.runner import effective_task_config
from appbuilder.jenkins import JenkinsBrowser, NeedsAttention


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.body = {
            'plugin_branch': 'develop', 'gmx_branch': 'develop',
            'plugin_version': '1.3', 'country': 'UK', 'project_name': 'PayByBank',
        }

    def test_config_defaults_include_both_job_parents_and_existing_defaults(self):
        d = web.form_defaults(self.cfg)
        self.assertIn('paybybank-ios-plugin', d['plugin_url'])
        self.assertIn('jenkins', d['plugin_parent'])
        self.assertIn('global-ios-upload', d['gmx_parent'])
        self.assertEqual(d['discover_poll_seconds'], 10)
        self.assertEqual(d['poll_seconds'], 45)
        self.assertEqual(d['result_poll_seconds'], 5)
        self.assertEqual(d['gmx_build_type'], 'Develop')
        self.assertEqual(d['gmx_signing_repo_branch'], 'master')
        self.assertTrue(d['gmx_sync_upload'])
        self.assertNotIn('github_token', d)
        self.assertNotIn('jenkins_password', d)

    def test_task_scoped_url_and_jekins_build_parameters_from_web(self):
        body = dict(self.body, plugin_url='https://github.corp/mobile/p1',
                    gmx_url='https://github.corp/mobile/gmx',
                    plugin_parent='https://plugin-jenkins.corp/job/release/job/',
                    gmx_parent='https://gmx-jenkins.corp/job/upload/job/',
                    browser_channel='msedge', discover_poll_seconds='15',
                    poll_seconds='55', result_poll_seconds='3', console_settle_seconds='20',
                    gmx_build_type='Release', gmx_signing_repo_branch='release/1.3',
                    gmx_custom_folder='0_A_UK_Custom', gmx_sync_upload=False,
                    max_changed_lines='150', selector_build_type='input[name="buildType"]')
        with patch('appbuilder.cli.doctor_check', return_value=True), \
             patch.object(web, 'token_or_error', return_value='fake-token'), \
             patch.object(web, 'active_count', return_value=0), \
             patch('appbuilder.cli.spawn') as spawn, \
             tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)):
            response = web.create_build(body)
            row = store.task(response['id'])
            params = json.loads(row['params'])
            self.assertEqual(params['plugin_url'], body['plugin_url'])
            self.assertEqual(params['gmx_url'], body['gmx_url'])
            self.assertEqual(params['gmx_parameters'], {'BUILD_TYPE':'Release',
                'SIGNING_REPO_BRANCH':'release/1.3','SYNC_UPLOAD':False,
                'CUSTOM_FOLDER':'0_A_UK_Custom',
                'SKIP_TEST':True,'FORCE':True,'KEEP_ALIVE':False})
            self.assertIn('plugin-jenkins.corp', params['plugin_job'])
            self.assertIn('gmx-jenkins.corp', params['gmx_job'])
            self.assertEqual(params['jenkins_settings']['discover_poll_seconds'], 15)
            self.assertEqual(params['jenkins_settings']['poll_seconds'], 55)
            self.assertEqual(params['jenkins_settings']['result_poll_seconds'], 3)
            self.assertEqual(params['jenkins_settings']['gmx_fields']['BUILD_TYPE'], 'input[name="buildType"]')
            self.assertEqual(params['build_settings']['max_changed_lines'], 150)
            self.assertEqual(response['project'], 'PayByBank')
            self.assertNotIn('fake-token', row['params'])
            spawn.assert_called_once_with(response['id'], wait=False)

    def test_two_tasks_do_not_share_overrides_or_mutate_yaml(self):
        cfg = load_config()
        original = json.dumps(cfg, sort_keys=True)
        overrides_a = {'jenkins_settings': {'poll_seconds': 20, 'browser_channel': 'chrome',
                     'gmx_fields': {'CUSTOM_FOLDER':'#foo'}},
                     'build_settings': {'max_changed_lines':50}}
        overrides_b = {'jenkins_settings': {'poll_seconds': 60, 'browser_channel': 'msedge'}}
        a = effective_task_config(cfg, overrides_a)
        b = effective_task_config(cfg, overrides_b)
        self.assertEqual(a['jenkins']['poll_seconds'], 20)
        self.assertEqual(b['jenkins']['poll_seconds'], 60)
        self.assertEqual(a['jenkins']['gmx_fields']['CUSTOM_FOLDER'], '#foo')
        self.assertNotEqual(b['jenkins']['gmx_fields'].get('CUSTOM_FOLDER'), '#foo')
        self.assertEqual(a['build']['max_changed_lines'], 50)
        self.assertEqual(b['build']['max_changed_lines'], cfg['build']['max_changed_lines'])
        self.assertEqual(original, json.dumps(cfg, sort_keys=True))

    def test_default_body_keeps_expected_folder_and_max_parallel(self):
        github, jenkins, build, gmx = web.build_preferences(self.body, self.cfg, 'UK', 'PayByBank')
        self.assertEqual(gmx['CUSTOM_FOLDER'], '0_A_UK_PayByBank')
        self.assertEqual(gmx['BUILD_TYPE'], 'Develop')
        self.assertTrue(gmx['SYNC_UPLOAD'])
        self.assertEqual(jenkins['poll_seconds'], 45)
        self.assertEqual(jenkins['result_poll_seconds'], 5)
        self.assertEqual(build['max_changed_lines'], 100)

    def test_reject_credentials_in_urls_before_creating_tasks(self):
        for url in ['http://git.corp/x', 'https://user:secret@git.corp/x',
                    'https://git.corp/x?token=x', 'javascript:alert(1)']:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    web.build_preferences({**self.body,'plugin_url':url},self.cfg,'UK','PayByBank')

    def test_reject_invalid_poll_and_regexp_and_enum(self):
        for invalid in [
            {'poll_seconds':'2'}, {'result_poll_seconds':'0'}, {'discover_poll_seconds':'abc'},
            {'gmx_sync_upload':'false'}, {'browser_channel':'firefox'},
            {'upload_success_pattern':'[unclosed'},
            {'gmx_custom_folder':'../escape'},
            {'gmx_signing_repo_branch':'../../bad'},
            {'gmx_build_type':'$()'},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                web.build_preferences({**self.body,**invalid},self.cfg,'UK','PayByBank')

    def test_default_parameters_exposed_without_credentials(self):
        html = web.STATIC.joinpath('index.html').read_text()
        js = web.STATIC.joinpath('app.js').read_text()
        for field in ('plugin_url','gmx_url','plugin_parent','gmx_parent','gmx_build_type',
                      'gmx_signing_repo_branch','gmx_custom_folder','gmx_sync_upload',
                      'discover_poll_seconds','poll_seconds','build_timeout_minutes',
                      'callback_timeout_minutes','upload_success_pattern','browser_channel'):
            self.assertIn('name="'+field+'"', html)
            self.assertIn(field, js)
        self.assertIn('installDefaults(boot.config)', js)
        self.assertIn('if(!customFolderEdited)', js)
        self.assertNotIn('localStorage', js)


class FakeControl:
    def __init__(self, tag='input', typ='text', value='', values=()):
        self.tag,self.typ,self.value=tag,typ,value
        self._values=list(values)
        self.checked=False
    def evaluate(self, fn): return self.tag
    def get_attribute(self, key): return self.typ if key=='type' else None
    def locator(self, arg):
        if arg!='option':raise AssertionError(arg)
        return self
    def evaluate_all(self, fn):return [{'value':v,'text':v} for v in self._values]
    def select_option(self, value):self.value=value
    def fill(self, value):self.value=value
    def check(self):self.checked=True
    def uncheck(self):self.checked=False
    def is_checked(self):return self.checked
    def input_value(self):return self.value


class JenkinsFieldsTest(unittest.TestCase):
    def test_gmx_all_five_fields_written_and_verified(self):
        controls = {
            'ENTITIES':FakeControl('select',values=('UK','HK')),
            'SYNC_UPLOAD':FakeControl(typ='checkbox'),
            'CUSTOM_FOLDER':FakeControl(),
            'BUILD_TYPE':FakeControl('select',values=('Develop','Release')),
            'SIGNING_REPO_BRANCH':FakeControl(),
        }
        browser=SimpleNamespace(conf={'gmx_fields':{}},
                _find_control=lambda name,css='': controls[name])
        options = {'SYNC_UPLOAD':False, 'CUSTOM_FOLDER':'0_A_UK_Custom',
                   'BUILD_TYPE':'Release', 'SIGNING_REPO_BRANCH':'release/1.3'}
        JenkinsBrowser.fill_gmx_parameters(browser, 'UK','PayByBank',options)
        self.assertEqual(controls['ENTITIES'].value,'UK')
        self.assertFalse(controls['SYNC_UPLOAD'].checked)
        self.assertEqual(controls['CUSTOM_FOLDER'].value,'0_A_UK_Custom')
        self.assertEqual(controls['BUILD_TYPE'].value,'Release')
        self.assertEqual(controls['SIGNING_REPO_BRANCH'].value,'release/1.3')

    def test_missing_build_type_option_stops_unsafe_form_submit(self):
        controls={
            'ENTITIES':FakeControl('select',values=('UK',)),
            'SYNC_UPLOAD':FakeControl(typ='checkbox'),
            'CUSTOM_FOLDER':FakeControl(),
            'BUILD_TYPE':FakeControl('select',values=('Develop',)),
            'SIGNING_REPO_BRANCH':FakeControl(),
        }
        browser=SimpleNamespace(conf={'gmx_fields':{}},_find_control=lambda name,css='': controls[name])
        with self.assertRaises(NeedsAttention):
            JenkinsBrowser.fill_gmx_parameters(browser, 'UK','PayByBank',{'BUILD_TYPE':'Unknown'})


if __name__=='__main__': unittest.main()

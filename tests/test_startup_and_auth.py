"""Offline regression tests: once-only Keychain credentials and one-command startup."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout

from appbuilder.auth import (SERVICE, CredentialError, store_credentials,
                             read_credentials, approved_login_host,
                             submit_recognized_login)
from appbuilder.cli import args_parser, create_params, setup_credentials, doctor_check
from appbuilder.jenkins import JenkinsBrowser


class FakeLocator:
    def __init__(self, visible=True):
        self.visible = visible
        self.calls = []
    def count(self): return 1
    def is_visible(self): return self.visible
    def fill(self, value): self.calls.append(('fill', value))
    def click(self): self.calls.append(('click',))

class LoginPage:
    def __init__(self):
        self.fields = {
            'input[name="j_username"]': FakeLocator(),
            'input[type="password"]': FakeLocator(),
            'button[type="submit"]': FakeLocator(),
        }
    def locator(self, css):
        return self.fields.get(css, EmptyLocator())

class EmptyLocator:
    def count(self): return 0
    def is_visible(self): return False


class AuthAndStartupTests(unittest.TestCase):
    def test_cli_accepts_once_only_login_and_update(self):
        a = args_parser().parse_args(['run', '--plugin-version','1.3','--plugin-branch','develop',
            '--gmx-branch','develop','--project-name','Demo','--jenkins-user','example',
            '--jenkins-password','SECRET_PASSWORD','--update','--live'])
        self.assertEqual(a.jenkins_user, 'example')
        self.assertTrue(a.update)
        self.assertTrue(a.live)
        conf={'github':{'plugin_url':'https://git/plugin','gmx_url':'https://git/gmx'}}
        self.assertNotIn('SECRET_PASSWORD', str(create_params(a, conf)))
        self.assertNotIn('example', str(create_params(a, conf)))

    def test_mac_keychain_is_used_and_read_back(self):
        events = []
        def handler(command, **kwargs):
            events.append(command)
            if command[1] == 'find-generic-password':
                return SimpleNamespace(returncode=0, stdout='example\n' if SERVICE+'.account' in command else 'SECRET_PASSWORD\n')
            return SimpleNamespace(returncode=0, stdout='')
        with patch('appbuilder.auth.sys.platform', 'darwin'), patch('appbuilder.auth.subprocess.run', side_effect=handler):
            store_credentials('example', 'SECRET_PASSWORD')
            self.assertEqual(read_credentials(), ('example', 'SECRET_PASSWORD'))
        self.assertEqual(len(events), 4)
        self.assertIn('security', events[0]); self.assertIn('-U', events[0])

    def test_missing_username_password_pair_is_rejected(self):
        with self.assertRaises(CredentialError):
            setup_credentials(SimpleNamespace(jenkins_user='x',jenkins_password=None))

    def test_keychain_errors_do_not_expose_secret(self):
        with patch('appbuilder.auth.sys.platform','darwin'), patch('appbuilder.auth.subprocess.run',
                   return_value=SimpleNamespace(returncode=1,stdout='',stderr='bad SECRET_PASSWORD')):
            with self.assertRaises(CredentialError) as cm:
                store_credentials('x','SECRET_PASSWORD')
        self.assertNotIn('SECRET_PASSWORD',str(cm.exception))

    def test_login_host_allowlist_prevents_lookalike_and_http(self):
        expected='https://jenkins.corp.hsbc/job/X'
        self.assertTrue(approved_login_host('https://jenkins.corp.hsbc/login',expected,['*.hsbc']))
        self.assertTrue(approved_login_host('https://login.systems.uk.hsbc/',expected,['*.hsbc']))
        self.assertFalse(approved_login_host('https://login.hsbc.attacker.tld',expected,['*.hsbc']))
        self.assertFalse(approved_login_host('http://login.systems.uk.hsbc/',expected,['*.hsbc']))
        self.assertFalse(approved_login_host('https://sso.microsoftonline.com',expected,['*.hsbc']))

    def test_safe_login_form_submits_and_never_logs_password(self):
        page=LoginPage()
        logs=[]
        self.assertTrue(submit_recognized_login(page,'test-account','SECRET_PASSWORD',log=logs.append))
        self.assertEqual(page.fields['input[name="j_username"]'].calls,[('fill','test-account')])
        self.assertEqual(page.fields['input[type="password"]'].calls,[('fill','SECRET_PASSWORD')])
        self.assertEqual(page.fields['button[type="submit"]'].calls,[('click',)])
        self.assertNotIn('SECRET_PASSWORD',''.join(logs))

    def test_unrecognized_login_form_refuses_to_type(self):
        self.assertFalse(submit_recognized_login(SimpleNamespace(locator=lambda css: EmptyLocator()),'a','b'))

    def test_cached_state_not_required_when_keychain_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            manager=MagicMock()
            playwright=MagicMock()
            manager.start.return_value=playwright
            with patch('appbuilder.jenkins.read_credentials',return_value=('user','password')), \
                 patch('appbuilder.jenkins.sync_playwright',return_value=manager):
                b=JenkinsBrowser({'jenkins':{'browser_channel':'chrome'}},root,root,'plugin')
                try:
                    playwright.chromium.launch.return_value.new_context.assert_called_once_with(accept_downloads=False)
                finally:
                    b.close()


    def test_browser_reuses_valid_login_without_keychain_form_submission(self):
        class Page:
            url = 'https://jenkins.corp.hsbc/job/x'
            def locator(self, css): return EmptyLocator()
        fake=SimpleNamespace(page=Page(), credentials=('user', 'pw'))
        self.assertFalse(JenkinsBrowser._login_if_needed(fake, 'https://jenkins.corp.hsbc/job/x'))

    def test_browser_submits_login_and_refreshes_cached_state(self):
        class Page(LoginPage):
            def __init__(self):
                super().__init__()
                self.url='https://login.systems.uk.hsbc/authorize'
                self.ok=False
            def locator(self, css):
                if css == 'input[type="password"]' and self.ok:
                    return EmptyLocator()
                return super().locator(css)
            def wait_for_url(self, matcher, timeout):
                self.url='https://jenkins.corp.hsbc/job/X'
                self.ok=True
                if not matcher(self.url):
                    raise AssertionError('wrong URL wait')
        cache=[]
        obj=SimpleNamespace(page=Page(), credentials=('example', 'SECRET_PASSWORD'), site='plugin',
                             conf={'auth_allowed_hosts':['*.hsbc']},
                             _cache_login=lambda:cache.append('cached'), visible=False)
        self.assertTrue(JenkinsBrowser._login_if_needed(obj,'https://jenkins.corp.hsbc/job/X'))
        self.assertEqual(cache,['cached'])

    def test_one_command_startup_installs_only_if_missing(self):
        script=(Path(__file__).resolve().parents[1]/'app-build').read_text()
        self.assertIn('./install.sh',script)
        self.assertIn('import playwright, yaml',script)
        self.assertIn('git pull --ff-only',script)
        self.assertNotIn('pip install --upgrade',script)
        self.assertIn('doctor_check(c, strict=True)',(Path(__file__).resolve().parents[1]/'appbuilder/cli.py').read_text())

    def test_doctor_with_mocked_dependencies(self):
        conf={'jenkins':{'browser_channel':'chromium'},'github':{'token_env':'FAKE_GITHUB_PAT'}}
        with patch.dict(os.environ, {'FAKE_GITHUB_PAT':'dummy'}), \
             patch('appbuilder.cli.read_credentials',return_value=('user','password')), \
             patch('appbuilder.cli.shutil.which',return_value='/usr/bin/git'), \
             patch('appbuilder.cli.sys.platform','linux'), redirect_stdout(io.StringIO()):
            self.assertTrue(doctor_check(conf,strict=True))

if __name__=='__main__':
    unittest.main()

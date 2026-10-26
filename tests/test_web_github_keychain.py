"""PAT setup via Web Console and existing CLI/Worker without Shell export."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from appbuilder import auth, web, config, gitops, cli

class WebPATTests(unittest.TestCase):
    def test_token_stored_with_keychain_no_file(self):
        with patch.object(auth.sys,'platform','darwin'), patch.object(auth.subprocess,'run') as run:
            run.return_value=MagicMock(returncode=0,stdout='',stderr='')
            auth.store_github_token('fake-pat-not-real')
            args=run.call_args.args[0]
            self.assertEqual(args[0:2],['security','add-generic-password'])
            self.assertEqual(args[-2:],['-w','fake-pat-not-real'])
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_invalid_token_rejected_before_keychain(self):
        with patch.object(auth.subprocess,'run') as run:
            with self.assertRaises(auth.CredentialError):
                auth.store_github_token('bad\ntoken')
            run.assert_not_called()

    def test_read_token_keychain_macos(self):
        with patch.object(auth.sys,'platform','darwin'), patch.object(auth.subprocess,'run') as run:
            run.return_value=MagicMock(returncode=0,stdout='local_pat\n',stderr='')
            self.assertEqual(auth.read_github_token(),'local_pat')

    def test_config_token_prefers_env_then_keychain(self):
        c=config.load_config()
        with patch.dict(os.environ,{'GITHUB_TOKEN':''}),patch.object(auth,'read_github_token',return_value='keychain-example'):
            self.assertEqual(config.token_or_error(c),'keychain-example')
        with patch.dict(os.environ,{'GITHUB_TOKEN':'from-env'}),patch.object(auth,'read_github_token') as keychain:
            self.assertEqual(config.token_or_error(c),'from-env')
            keychain.assert_not_called()

    def test_git_worker_reads_keychain_token(self):
        with patch.dict(os.environ,{'GITHUB_TOKEN':''}),patch.object(auth,'read_github_token',return_value='fake-web-pat'):
            with gitops.credentials('GITHUB_TOKEN') as env:
                self.assertEqual(env['APP_BUILDER_GIT_TOKEN'],'fake-web-pat')
                from pathlib import Path
                self.assertNotIn('fake-web-pat',Path(env['GIT_ASKPASS']).read_text())

    def test_web_accepts_pat_not_saved_in_task_payload(self):
        payload={'plugin_branch':'develop','gmx_branch':'feature/UKPBB-203745-test','plugin_version':'1.3',
                 'project_name':'PayByBank','country':'UK','github_token':'WEB_TEST_SECRET',
                 'visible':True,'verbose':True}
        with patch('appbuilder.auth.store_github_token') as store,\
             patch.object(cli,'doctor_check',return_value=True),\
             patch.object(web,'token_or_error',return_value='WEB_TEST_SECRET'),\
             patch.object(web,'active_count',return_value=0),\
             patch.object(web,'create_task',return_value={'id':'202610101234-abcdef'}) as create,\
             patch.object(web,'task',return_value={'id':'202610101234-abcdef','created':'now',
                'params':json.dumps({'project':'PayByBank'}), 'status':'QUEUED','pid':None,'stage':'INIT','note':''}),\
             patch.object(cli,'spawn'):
            result=web.create_build(payload)
        store.assert_called_once_with('WEB_TEST_SECRET')
        self.assertNotIn('github_token',create.call_args.args[1])
        self.assertNotIn('WEB_TEST_SECRET',json.dumps(result))

    def test_web_password_input_in_browser_form(self):
        html=(web.STATIC/'index.html').read_text()
        js=(web.STATIC/'app.js').read_text()
        self.assertIn('name="github_token" type="password"',html)
        # Set once on the settings page; never part of a build body or schedule template.
        self.assertNotIn('data-build name="github_token"', html)
        self.assertIn('id="credentials-form"', html)
        settings_js=(web.STATIC/'settings.js').read_text()
        self.assertIn("f.github_token.value=''",settings_js)

if __name__=='__main__':unittest.main()

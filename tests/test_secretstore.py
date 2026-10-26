import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import auth, scheduler, secretstore


class SecretStoreTests(unittest.TestCase):
    def test_env_name_rule(self):
        self.assertEqual(secretstore.env_name('mac-app-builder.github-token', 'enterprise'),
                         'APPBUILDER_SECRET_MAC_APP_BUILDER_GITHUB_TOKEN_ENTERPRISE')

    def test_env_and_file_secrets_on_linux(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'jira'
            f.write_text('from-file\n')
            env = {'APPBUILDER_SECRET_MAC_APP_BUILDER_GITHUB_TOKEN_ENTERPRISE': 'from-env',
                   'APPBUILDER_SECRET_MAC_APP_BUILDER_JIRA_TOKEN_ENTERPRISE_FILE': str(f)}
            with patch.dict(os.environ, env), patch('appbuilder.auth.sys.platform', 'linux'):
                self.assertEqual(auth.read_github_token(), 'from-env')
                self.assertEqual(auth.read_jira_token(), 'from-file')

    def test_jenkins_credentials_from_env(self):
        env = {'APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_ACCOUNT_DEFAULT': 'alice',
               'APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_ALICE': 'pw'}
        with patch.dict(os.environ, env), patch('appbuilder.auth.sys.platform', 'linux'):
            self.assertEqual(auth.read_credentials(), ('alice', 'pw'))

    def test_write_without_keyring_names_the_variable(self):
        with patch('appbuilder.auth.sys.platform', 'linux'), patch.object(secretstore, '_keyring', return_value=None):
            with self.assertRaisesRegex(auth.CredentialError, 'APPBUILDER_SECRET_MAC_APP_BUILDER_GITHUB_TOKEN_ENTERPRISE'):
                auth.store_github_token('ghp_x')

    def test_write_uses_keyring_when_present(self):
        class KR:
            saved = None
            def set_password(self, s, a, p): KR.saved = (s, a, p)
        with patch('appbuilder.auth.sys.platform', 'linux'), patch.object(secretstore, '_keyring', return_value=KR()):
            auth.store_jira_token('tok')
        self.assertEqual(KR.saved, ('mac-app-builder.jira-token', 'enterprise', 'tok'))


class SystemdUnitTests(unittest.TestCase):
    def test_unit_contents(self):
        text = scheduler.build_systemd_unit(python='/usr/bin/python3', root='/srv/app', data='/srv/app/.appbuilder')
        self.assertIn('WorkingDirectory=/srv/app', text)
        self.assertIn('ExecStart=/usr/bin/python3 -m appbuilder scheduler run', text)
        self.assertIn('Restart=always', text)
        self.assertIn('WantedBy=default.target', text)

    def test_install_and_uninstall_write_and_remove_unit(self):
        calls = []
        class R:
            returncode = 0; stdout = ''; stderr = ''
        with tempfile.TemporaryDirectory() as d, \
             patch.dict(os.environ, {'XDG_CONFIG_HOME': d}), \
             patch('appbuilder.scheduler.sys.platform', 'linux'), \
             patch('appbuilder.scheduler.shutil.which', return_value='/bin/systemctl'), \
             patch('appbuilder.scheduler.DATA', Path(d) / 'data'), \
             patch('appbuilder.scheduler._systemctl', side_effect=lambda *a: calls.append(a) or R()):
            path = scheduler.install()
            self.assertTrue(path.exists())
            self.assertIn(('enable', '--now', scheduler.SYSTEMD_UNIT), calls)
            self.assertTrue(scheduler.uninstall())
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()

"""Settings page: GitHub token and Jenkins browser sign-in go to the Keychain, never back out."""
import unittest
from unittest.mock import patch

from appbuilder import credentials_api as C
from appbuilder.auth import CredentialError


class CredentialsApiTests(unittest.TestCase):
    def test_status_never_returns_secrets(self):
        with patch('appbuilder.auth.read_github_token', return_value='SECRET-PAT'), \
                patch('appbuilder.auth.read_credentials', return_value=('george', 'SECRET-PW')):
            out = C.status()
        self.assertEqual(out, {'github': True, 'jenkins_user': 'george'})
        self.assertNotIn('SECRET', repr(out))

    def test_save_stores_only_what_was_typed(self):
        with patch('appbuilder.auth.store_github_token') as gh, patch('appbuilder.auth.store_credentials') as jk, \
                patch.object(C, 'status', return_value={'github': True, 'jenkins_user': ''}):
            C.save({'github_token': ' pat '})
        gh.assert_called_once_with('pat')
        jk.assert_not_called()
        with patch('appbuilder.auth.store_github_token') as gh, patch('appbuilder.auth.store_credentials') as jk, \
                patch.object(C, 'status', return_value={}):
            C.save({'jenkins_user': 'george', 'jenkins_password': 'pw'})
        gh.assert_not_called()
        jk.assert_called_once_with('george', 'pw')

    def test_bad_input_is_a_400_with_a_plain_reason(self):
        for body, reason in (({}, '请填写'), ({'jenkins_user': 'george'}, '一起填写'),
                             ({'github_token': 'x' * 5000}, '过长')):
            with self.subTest(body=list(body)):
                status, out = C.handle_post('/api/credentials/save', body)
                self.assertEqual(status, 400)
                self.assertIn(reason, out['error'])
        with patch('appbuilder.auth.store_github_token', side_effect=CredentialError('Keychain 失败')):
            self.assertEqual(C.handle_post('/api/credentials/save', {'github_token': 'p'}), (400, {'error': 'Keychain 失败'}))
        self.assertIsNone(C.handle_post('/api/other', {}))


if __name__ == '__main__':
    unittest.main()

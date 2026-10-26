"""Branch lookup only uses server-side tokens and trusted configured hosts."""
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch
from appbuilder.branches import list_branches, NoRedirect

CONFIG={'github':{'plugin_url':'https://github.corp/team/plugin','gmx_url':'https://github.corp/team/gmx'}}


class BranchPickerTests(unittest.TestCase):
    def test_enterprise_paging_and_public_github_endpoint(self):
        for repo,config,expected in [
            ('https://github.corp/team/plugin.git',CONFIG,'https://github.corp/api/v3/repos/team/plugin/branches?per_page=100&page=2'),
            ('https://github.com/team/plugin',{'github':{'plugin_url':'https://github.com/team/plugin'}},'https://api.github.com/repos/team/plugin/branches?per_page=100&page=2')]:
            opener=MagicMock()
            opener.open.return_value.__enter__.return_value=io.StringIO(json.dumps([{'name':f'b{i}'} for i in range(100)]))
            with patch('appbuilder.branches.token_or_error',return_value='server-only-secret'),patch('appbuilder.branches.build_opener',return_value=opener):
                result=list_branches({'kind':'plugin','url':repo,'page':2},config)
            request=opener.open.call_args.args[0]
            self.assertEqual(request.full_url,expected)
            self.assertEqual(request.get_header('Authorization'),'Bearer server-only-secret')
            self.assertTrue(result['has_more'])
            self.assertNotIn('server-only-secret',json.dumps(result))

    def test_untrusted_hosts_invalid_urls_and_paging_fail_before_token_lookup(self):
        for body in [{'kind':'plugin','url':'https://evil.example/team/plugin'},
                     {'kind':'plugin','url':'https://secret@github.corp/team/plugin'},
                     {'kind':'plugin','url':'https://github.corp/team/plugin?token=secret'},
                     {'kind':'plugin','url':'https://github.corp/team/plugin','page':0},
                     {'kind':'other','url':'https://github.corp/team/plugin'}]:
            with self.subTest(body=body),patch('appbuilder.branches.token_or_error') as token:
                with self.assertRaises(ValueError):list_branches(body,CONFIG)
                token.assert_not_called()

    def test_error_messages_do_not_echo_auth_or_upstream_body(self):
        for error in [HTTPError('https://git',403,'server-only-secret',{},None),URLError('server-only-secret')]:
            opener=MagicMock();opener.open.side_effect=error
            with patch('appbuilder.branches.token_or_error',return_value='server-only-secret'),patch('appbuilder.branches.build_opener',return_value=opener):
                with self.assertRaises(ValueError) as caught:list_branches({'kind':'plugin','url':CONFIG['github']['plugin_url']},CONFIG)
            self.assertNotIn('server-only-secret',str(caught.exception))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))

    def test_missing_local_token_has_actionable_error(self):
        with patch('appbuilder.branches.token_or_error',side_effect=RuntimeError('not set')):
            with self.assertRaisesRegex(ValueError,'Keychain'):
                list_branches({'kind':'plugin','url':CONFIG['github']['plugin_url']},CONFIG)

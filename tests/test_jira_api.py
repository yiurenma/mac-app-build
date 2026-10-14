import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import jira_api as A
from appbuilder import jira_notify as J


class FakeClient:
    def __init__(self, found=True, names=('In Test', 'Done')):
        self.found, self.names = found, list(names)

    def myself(self):
        return {'displayName': 'George', 'name': 'george'}

    def issue(self, key):
        if not self.found:
            raise J.JiraNotFound('HTTP 404')
        return {'fields': {'summary': 'Fix title', 'status': {'name': 'Dev Done'}, 'reporter': {'displayName': 'Alice'},
                           'issuetype': {'name': 'Bug'}}}

    def transitions(self, key, expand=False):
        rc = {'customfield_100': {'required': True, 'name': 'Root Cause', 'allowedValues': [{'value': 'Code'}]}}
        return [{'name': n, 'id': str(i), 'fields': rc} for i, n in enumerate(self.names)]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / 'jira_settings.json'
        self.patches = [patch.object(J, 'SETTINGS_FILE', self.file),
                        patch.object(A, 'load_config', return_value={'jira': {'enabled': False, 'base_url': 'https://cfg.example.com'}})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_page_settings_override_config_yaml_and_persist(self):
        self.assertFalse(A.public_settings()['settings']['enabled'])
        out = A.save_settings({'enabled': True, 'base_url': 'https://wpb-jira.example.com/', 'first_run_commits': 7})
        self.assertTrue(out['settings']['enabled'])
        self.assertEqual(out['settings']['base_url'], 'https://wpb-jira.example.com')
        self.assertEqual(out['settings']['first_run_commits'], 7)
        self.assertEqual(json.loads(self.file.read_text())['enabled'], True)

    def test_invalid_values_are_rejected_before_anything_is_saved(self):
        for bad in ({'base_url': 'http://insecure'}, {'base_url': 'https://x?a=1'}, {'key_pattern': '(['},
                    {'flavor': 'other'}, {'transition_name': '  '}, {'first_run_commits': 999},
                    {'first_run_commits': True}, {'enabled': 'yes'}, {'max_tickets_per_run': 0},
                    {'transition_fields': 'Bug | Root Cause | Code | extra'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                A.save_settings(bad)
        self.assertFalse(self.file.exists())

    def test_token_goes_to_keychain_and_is_never_returned_or_written(self):
        with patch('appbuilder.auth.store_jira_token') as store:
            out = A.save_settings({'enabled': True, 'token': 'SECRET-PAT'})
        store.assert_called_once_with('SECRET-PAT')
        self.assertNotIn('SECRET-PAT', json.dumps(out))
        self.assertNotIn('SECRET-PAT', self.file.read_text())

    def test_unknown_or_mistyped_fields_in_the_saved_file_are_ignored(self):
        self.file.write_text(json.dumps({'enabled': True, 'evil': 1, 'first_run_commits': True}))
        self.assertEqual(J.saved_settings(), {'enabled': True})

    def test_connection_and_ticket_lookup(self):
        self.assertEqual(A.test_connection(FakeClient())['user'], 'George')
        r = A.lookup_ticket('TRANSACT-210390', FakeClient(), J.settings({}))
        self.assertTrue(r['found'] and r['can_transition'])
        self.assertEqual(r['reporter'], 'Alice')
        self.assertEqual(r['required_fields'], [{'name': 'Root Cause', 'allowed': ['Code'], 'configured': False, 'auto': 'Code'}])
        r = A.lookup_ticket('TRANSACT-1', FakeClient(), {**J.settings({}), 'transition_fields': 'Bug | Root Cause | Code'})
        self.assertEqual(r['problems'], [])
        self.assertTrue(r['required_fields'][0]['configured'])
        r = A.lookup_ticket('TRANSACT-1', FakeClient(names=('Done',)), J.settings({}))
        self.assertFalse(r['can_transition'])
        r = A.lookup_ticket('TRANSACT-1', FakeClient(found=False), J.settings({}))
        self.assertFalse(r['found'])
        with self.assertRaises(ValueError):
            A.lookup_ticket('not a key', FakeClient(), J.settings({}))

    def test_routes_return_400_with_message_on_bad_input(self):
        status, body = A.handle_post('/api/jira/preview', {'task_id': '', 'recent': 10})
        self.assertEqual(status, 400)
        self.assertIn('error', body)
        status, body = A.handle_post('/api/jira/preview', {'task_id': 'T', 'recent': 0})
        self.assertEqual(status, 400)
        self.assertIsNone(A.handle_post('/api/other', {}))
        self.assertEqual(A.handle_get('/api/jira/settings', {})[0], 200)


if __name__ == '__main__':
    unittest.main()

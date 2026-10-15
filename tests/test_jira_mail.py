"""Team email through Jira's notify API (replaces the old Outlook email)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import jira_api as A
from appbuilder import jira_mail as M
from appbuilder import jira_notify as J

LINK = 'https://tenant.testfairy.com/join/abc?id=1'
BASE = 'https://jira.example.com'
PARAMS = {'country': 'UK', 'project': 'PayByBank', 'plugin_source': 'develop', 'plugin_branch': 'release/v1.3.1'}


class FakeJira:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def _call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return {}


def runtime(report=None, link=LINK):
    rt = {'gmx_build_number': 42, 'testfairy_app': {'share_url': link, 'build_url': 'https://jenkins/x/42/'} if link else {}}
    if report is not None:
        rt['jira_report'] = report
    return rt


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'jira_settings.json'
        self.row = {'id': '202610150900-abcdef', 'status': 'SUCCESS', 'params': json.dumps(PARAMS)}
        self.state = {'runtime': runtime({'status': 'DONE', 'tickets': [{'key': 'TRANSACT-1', 'summary': 'Fix login'}],
                                          'errors': [], 'needs_manual': []})}
        self.writes = []

        def patch_runtime(task_id, **values):
            self.writes.append(values)
            self.state['runtime'].update(values)

        p = patch.object(J, 'SETTINGS_FILE', self.file); p.start(); self.addCleanup(p.stop)
        for name, value in [('task', lambda _: self.row), ('read_state', lambda _: self.state),
                            ('patch_runtime', patch_runtime),
                            ('load_config', lambda: {'jira': {'base_url': BASE}})]:
            p = patch.object(M, name, value); p.start(); self.addCleanup(p.stop)

    def save(self, **values):
        self.file.write_text(json.dumps(values))


class ParsingTests(unittest.TestCase):
    def test_users_accept_commas_spaces_newlines_and_drop_duplicates(self):
        self.assertEqual(M.parse_users('44065253, 43123456；alice.w\n44065253'), ['44065253', '43123456', 'alice.w'])
        self.assertEqual(M.parse_users(''), [])

    def test_display_names_and_too_many_users_are_rejected(self):
        for bad in ('George <g@x.com>', 'a/b', ' '.join(str(i) for i in range(51)), 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                M.parse_users(bad)

    def test_issue_key_is_normalised_and_checked(self):
        self.assertEqual(M.check_issue(' transact-12 '), 'TRANSACT-12')
        self.assertEqual(M.check_issue(''), '')
        for bad in ('12', 'TRANSACT', 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                M.check_issue(bad)


class ComposeTests(unittest.TestCase):
    row = {'id': 'T1'}

    def test_body_has_link_branch_build_and_ticket_links(self):
        report = {'status': 'DONE', 'tickets': [{'key': 'TRANSACT-1', 'summary': 'Fix login'}],
                  'errors': [{'key': 'TRANSACT-2', 'error': 'x'}], 'needs_manual': [{'key': 'TRANSACT-1', 'reason': 'r'}]}
        subject, body = M.compose(self.row, PARAMS, runtime(report), BASE)
        self.assertEqual(subject, '[新测试包] UK PayByBank · GMX #42')
        for text in (LINK, 'develop → release/v1.3.1', '#42', 'TRANSACT-1 Fix login  https://jira.example.com/browse/TRANSACT-1',
                     'TRANSACT-2（自动更新失败', '状态需手动修改'):
            self.assertIn(text, body)

    def test_dry_run_baseline_and_no_jira_are_said_plainly(self):
        _, body = M.compose(self.row, PARAMS, runtime({'status': 'DRY_RUN', 'tickets': []}), BASE)
        self.assertIn('本次没有需要更新的 Jira', body)
        _, body = M.compose(self.row, PARAMS, runtime({'status': 'DRY_RUN', 'tickets': [{'key': 'A-1'}]}), BASE)
        self.assertIn('演练模式', body)
        _, body = M.compose(self.row, PARAMS, runtime({'status': 'BASELINE'}), BASE)
        self.assertIn('首次运行', body)
        _, body = M.compose(self.row, PARAMS, runtime(), BASE)
        self.assertIn('本次未自动更新', body)

    def test_test_mail_is_marked(self):
        subject, body = M.compose(self.row, PARAMS, runtime(), BASE, test=True)
        self.assertTrue(subject.startswith('[TEST]'))
        self.assertIn('测试邮件', body)


class NotifyTeamTests(Fixture):
    def test_success_sends_one_email_to_named_users_only(self):
        self.save(mail_enabled=True, mail_issue='TRANSACT-9', mail_users='44065253, 43123456', base_url=BASE)
        jira = FakeJira()
        M.notify_team(self.row['id'], client=jira)
        M.notify_team(self.row['id'], client=jira)            # a second call never sends again
        self.assertEqual(len(jira.calls), 1)
        method, path, body = jira.calls[0]
        self.assertEqual((method, path), ('POST', '/rest/api/2/issue/TRANSACT-9/notify'))
        self.assertEqual(body['to'], {'reporter': False, 'assignee': False, 'watchers': False, 'voters': False,
                                      'users': [{'name': '44065253'}, {'name': '43123456'}]})
        self.assertIn(LINK, body['textBody'])
        self.assertIn('TRANSACT-1 Fix login', body['textBody'])
        self.assertEqual(self.state['runtime']['jira_mail']['status'], 'SENT')

    def test_cloud_uses_account_ids(self):
        self.save(mail_enabled=True, mail_issue='TRANSACT-9', mail_users='abc123', base_url=BASE, flavor='cloud')
        jira = FakeJira()
        M.notify_team(self.row['id'], client=jira)
        self.assertEqual(jira.calls[0][2]['to']['users'], [{'accountId': 'abc123'}])

    def test_off_failed_build_or_no_link_sends_nothing(self):
        jira = FakeJira()
        self.save(mail_enabled=False, mail_issue='TRANSACT-9', mail_users='a', base_url=BASE)
        M.notify_team(self.row['id'], client=jira)
        self.assertEqual(self.writes, [])
        self.save(mail_enabled=True, mail_issue='TRANSACT-9', mail_users='a', base_url=BASE)
        self.row['status'] = 'FAILED'
        M.notify_team(self.row['id'], client=jira)
        self.row['status'] = 'SUCCESS'
        self.state['runtime'] = runtime(link=None)
        M.notify_team(self.row['id'], client=jira)
        self.assertEqual(jira.calls, [])
        self.assertEqual(self.state['runtime']['jira_mail']['status'], 'SKIPPED')

    def test_missing_settings_are_recorded_not_raised(self):
        self.save(mail_enabled=True, mail_issue='', mail_users='', base_url=BASE)
        M.notify_team(self.row['id'], client=FakeJira())
        self.assertIn('公告单号', self.state['runtime']['jira_mail']['reason'])

    def test_jira_failure_is_recorded_once_and_not_retried(self):
        self.save(mail_enabled=True, mail_issue='TRANSACT-9', mail_users='a', base_url=BASE)
        jira = FakeJira(RuntimeError('Jira POST /rest/api/2/issue/TRANSACT-9/notify -> HTTP 403'))
        M.notify_team(self.row['id'], client=jira)
        mail = self.state['runtime']['jira_mail']
        self.assertEqual(mail['status'], 'FAILED')
        self.assertIn('HTTP 403', mail['reason'])


class TestEmailTests(Fixture):
    def test_uses_page_values_and_latest_successful_build(self):
        self.save(base_url=BASE)
        jira = FakeJira()
        with patch.object(M, 'latest_success', return_value=self.row['id']):
            out = M.test_email({'mail_issue': 'transact-9', 'mail_users': 'a b'}, client=jira)
        self.assertEqual((out['issue'], out['to'], out['from_task']), ('TRANSACT-9', ['a', 'b'], self.row['id']))
        self.assertTrue(jira.calls[0][2]['subject'].startswith('[TEST]'))
        self.assertIn(LINK, jira.calls[0][2]['textBody'])

    def test_without_any_successful_build_a_plain_test_is_sent(self):
        self.save(base_url=BASE)
        jira = FakeJira()
        with patch.object(M, 'latest_success', return_value=None):
            out = M.test_email({'mail_issue': 'TRANSACT-9', 'mail_users': 'a'}, client=jira)
        self.assertIsNone(out['from_task'])
        self.assertEqual(len(jira.calls), 1)

    def test_errors_explain_the_likely_cause(self):
        self.save(base_url=BASE)
        with patch.object(M, 'latest_success', return_value=None):
            for code, hint in ((400, '用户名不存在'), (403, '不允许'), (404, '公告单不存在')):
                jira = FakeJira(RuntimeError(f'Jira POST /x -> HTTP {code}'))
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, hint):
                    M.test_email({'mail_issue': 'TRANSACT-9', 'mail_users': 'a'}, client=jira)
            for body, message in (({'mail_issue': '', 'mail_users': 'a'}, '公告单号'),
                                  ({'mail_issue': 'TRANSACT-9', 'mail_users': ''}, '收件人')):
                with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                    M.test_email(body, client=FakeJira())

    def test_needs_jira_address(self):
        with patch.object(M, 'load_config', return_value={}), self.assertRaisesRegex(ValueError, 'Jira 地址'):
            M.test_email({'mail_issue': 'TRANSACT-9', 'mail_users': 'a'}, client=FakeJira())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'jira_settings.json'
        for p in (patch.object(J, 'SETTINGS_FILE', self.file), patch.object(A, 'load_config', return_value={})):
            p.start(); self.addCleanup(p.stop)

    def test_mail_settings_are_saved_from_the_page(self):
        out = A.save_settings({'mail_enabled': True, 'mail_issue': 'transact-9', 'mail_users': '44065253,\n43123456'})
        s = out['settings']
        self.assertEqual((s['mail_enabled'], s['mail_issue'], s['mail_users']), (True, 'TRANSACT-9', '44065253,\n43123456'))
        self.assertFalse(A.public_settings()['settings']['enabled'])     # email does not switch Jira write-back on

    def test_bad_mail_settings_are_rejected(self):
        for bad in ({'mail_issue': 'nope'}, {'mail_users': 'George <g@x.com>'}, {'mail_enabled': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                A.save_settings(bad)
        self.assertFalse(self.file.exists())

    def test_test_button_route_returns_400_with_the_reason(self):
        with patch.object(M, 'test_email', side_effect=ValueError('发送失败：HTTP 403')):
            self.assertEqual(A.handle_post('/api/jira/mail-test', {}), (400, {'error': '发送失败：HTTP 403'}))


class LegacyOutlookTemplateTests(unittest.TestCase):
    def test_schedules_saved_with_outlook_options_still_load(self):
        from appbuilder import schedules, web
        config = web.load_config()
        template = {**{k: v for k, v in web.form_defaults(config).items()
                       if k in schedules.BASIC_KEYS + schedules.OPTIONAL_TEXT + schedules.OPTIONAL_INT + schedules.OPTIONAL_BOOL},
                    'outlook_auto_send': True, 'outlook_recipients': 'a@example.com', 'outlook_account': ''}
        with patch('appbuilder.testfairy.saved_auth_available', return_value=True):
            cleaned = schedules.validate_template(template, config)
        self.assertFalse([k for k in cleaned if k.startswith('outlook')])


if __name__ == '__main__':
    unittest.main()

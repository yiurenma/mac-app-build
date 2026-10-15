"""交付记录: tickets with their App link (from task reports) and the email log."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import delivery_api as D
from appbuilder import jira_mail as M

LINK = 'https://tenant.testfairy.com/join/abc'


class DeliveryApiTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [{'id': 'T2', 'created': '2026-10-15T12:00:00'}, {'id': 'T1', 'created': '2026-10-15T06:00:00'},
                      {'id': 'T0', 'created': '2026-10-14T06:00:00'}]
        self.states = {
            'T2': {'runtime': {'jira_report': {'status': 'DONE', 'at': '2026-10-15T12:30:00', 'link': LINK, 'build': 7,
                                               'tickets': [{'key': 'TRANSACT-1', 'summary': 'Fix login',
                                                            'steps': ['comment', 'assign', 'transition']},
                                                           {'key': 'TRANSACT-2', 'summary': 'Bug', 'steps': ['comment']}],
                                               'needs_manual': [{'key': 'TRANSACT-2', 'reason': 'Fix Note 未配置'}],
                                               'errors': [{'key': 'TRANSACT-3', 'error': 'HTTP 500'}]}}},
            'T1': {'runtime': {'jira_report': {'status': 'DRY_RUN', 'tickets': [{'key': 'TRANSACT-1', 'steps': ['[dry-run] comment']}]},
                               'testfairy_app': {'share_url': LINK + '-old'}}},
            'T0': {'runtime': {'jira_report': {'status': 'BASELINE'}}},
        }
        for target, value in (('appbuilder.store.task_list', lambda: self.tasks),
                              ('appbuilder.store.read_state', lambda tid: self.states[tid])):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def test_every_ticket_with_its_link_result_and_build_newest_first(self):
        rows = D.tickets()
        self.assertEqual([(r['key'], r['result']) for r in rows],
                         [('TRANSACT-1', 'DONE'), ('TRANSACT-2', 'MANUAL'), ('TRANSACT-3', 'ERROR'), ('TRANSACT-1', 'DRY_RUN')])
        self.assertEqual((rows[0]['link'], rows[0]['build'], rows[0]['at']), (LINK, 7, '2026-10-15T12:30:00'))
        self.assertEqual(rows[1]['note'], 'Fix Note 未配置')
        self.assertEqual(rows[3]['link'], LINK + '-old')          # older reports: the task's App link
        self.assertEqual(rows[3]['at'], '2026-10-15T06:00:00')    # older reports: task time

    def test_search_by_jira_key_or_title(self):
        self.assertEqual([r['task_id'] for r in D.tickets('transact-1')], ['T2', 'T1'])
        self.assertEqual([r['key'] for r in D.tickets('login')], ['TRANSACT-1'])

    def test_non_https_links_are_dropped(self):
        self.states['T2']['runtime']['jira_report']['link'] = 'javascript:alert(1)'
        self.assertEqual(D.tickets()[0]['link'], '')

    def test_mail_route_reads_the_log(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(M, 'MAIL_LOG', Path(tmp) / 'log.jsonl'):
            M.log_mail({'kind': 'failure', 'subject': '[构建失败] X', 'to': ['a'], 'status': 'SENT', 'task_id': 'T2'})
            status, out = D.handle_get('/api/delivery/mails', {'q': ['构建失败']})
        self.assertEqual(status, 200)
        self.assertEqual(out['items'][0]['subject'], '[构建失败] X')
        with patch.object(D, 'jira_base', return_value='https://jira.example.com'):
            status, out = D.handle_get('/api/delivery/tickets', {})
        self.assertEqual((status, out['jira_base'], len(out['items'])), (200, 'https://jira.example.com', 4))
        self.assertIsNone(D.handle_get('/api/other', {}))


if __name__ == '__main__':
    unittest.main()

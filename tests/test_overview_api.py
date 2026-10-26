"""概览 summary: running build, last delivery, and what needs attention."""
import unittest
from datetime import datetime
from unittest.mock import patch

from appbuilder import overview_api as O

LINK = 'https://tenant.testfairy.com/join/abc'


class OverviewTests(unittest.TestCase):
    def build(self, tasks, states):
        with patch('appbuilder.store.task_list', return_value=tasks), \
                patch('appbuilder.store.read_state', side_effect=lambda tid: states.get(tid, {'completed': [], 'runtime': {}})), \
                patch('appbuilder.cli.alive', return_value=True), \
                patch.object(O, '_schedules', return_value=({'items': [], 'online': True},
                                                            [{'name': 'nightly', 'count': 1, 'planned': 'x'}])), \
                patch.object(O, '_automation', return_value={'mail_enabled': True}):
            return O.overview(now=datetime(2026, 10, 15, 20))

    def test_running_last_delivery_and_attention(self):
        tasks = [{'id': 'T4', 'created': '2026-10-15T18', 'status': 'RUNNING', 'stage': 'TESTFAIRY_LOOKUP', 'pid': 1},
                 {'id': 'T3', 'created': '2026-10-15T12', 'status': 'FAILED', 'stage': 'GMX_JENKINS', 'note': 'GMX 失败'},
                 {'id': 'T2', 'created': '2026-10-15T06', 'status': 'SUCCESS', 'stage': 'FINISHED'},
                 {'id': 'T1', 'created': '2026-10-15T00', 'status': 'FAILED', 'stage': 'PLUGIN_PREPARE', 'note': 'old'}]
        states = {'T4': {'completed': ['PLUGIN_PREPARE', 'PLUGIN_JENKINS', 'GMX_JENKINS'], 'runtime': {}},
                  'T2': {'runtime': {'testfairy_app': {'share_url': LINK},
                                     'jira_report': {'status': 'DONE', 'tickets': [{'key': 'TRANSACT-1'}],
                                                     'needs_manual': [{'key': 'TRANSACT-1', 'reason': 'Fix Note'}]},
                                     'jira_mail': {'status': 'SENT', 'to': ['a', 'b']}}}}
        o = self.build(tasks, states)
        self.assertEqual([s['state'] for s in o['running']['steps']], ['done', 'done', 'current', 'waiting', 'waiting'])
        self.assertEqual((o['last']['task_id'], o['last']['link'], o['last']['tickets'], o['last']['mail_to']),
                         ('T2', LINK, ['TRANSACT-1'], 2))
        kinds = [(a['kind'], a.get('task_id') or a.get('key') or a.get('name')) for a in o['attention']]
        # T1 failed before the last success, so it is no longer listed.
        self.assertEqual(kinds, [('task', 'T3'), ('jira', 'TRANSACT-1'), ('missed', 'nightly')])
        self.assertEqual(o['counts'], {'total': 4, 'running': 1, 'success': 1, 'attention': 2})

    def test_dead_worker_is_not_shown_as_running(self):
        tasks = [{'id': 'T1', 'created': '2026-10-15T18', 'status': 'RUNNING', 'stage': 'GMX_JENKINS', 'pid': 99}]
        with patch('appbuilder.store.task_list', return_value=tasks), \
                patch('appbuilder.store.read_state', return_value={'completed': [], 'runtime': {}}), \
                patch('appbuilder.cli.alive', return_value=False), \
                patch.object(O, '_schedules', return_value=({'items': [], 'online': False}, [])), \
                patch.object(O, '_automation', return_value={}):
            o = O.overview()
        self.assertIsNone(o['running'])
        self.assertEqual(o['attention'][0]['status'], 'INTERRUPTED')

    def test_non_https_link_is_dropped_and_route(self):
        tasks = [{'id': 'T1', 'created': '2026-10-15T06', 'status': 'SUCCESS', 'stage': 'FINISHED'}]
        o = self.build(tasks, {'T1': {'runtime': {'testfairy_app': {'share_url': 'javascript:x'}}}})
        self.assertEqual(o['last']['link'], '')
        with patch.object(O, 'overview', return_value={'ok': 1}):
            self.assertEqual(O.handle_get('/api/overview', {}), (200, {'ok': 1}))
        self.assertIsNone(O.handle_get('/api/x', {}))


if __name__ == '__main__':
    unittest.main()

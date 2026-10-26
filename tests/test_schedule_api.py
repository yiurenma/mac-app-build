"""Schedules over the real loopback Web Console, plus the real create_build() hand-off."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from appbuilder import schedules, scheduler, store, web

TOKEN = 'csrf-test-only-strong-token'
TEMPLATE = {'plugin_branch': 'develop', 'gmx_branch': 'develop', 'plugin_version': '1.3',
            'country': 'UK', 'project_name': 'PayByBank'}


class ScheduleAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer((web.HOST, 0), web.make_handler(TOKEN))
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for target, attr in ((store, 'DATA'), (scheduler, 'DATA')):
            p = patch.object(target, attr, Path(self.tmp.name))
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def call(self, method, path, body=None, csrf=True):
        headers = {'Host': f'{web.HOST}:{self.port}'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
            if csrf:
                headers['X-CSRF-Token'] = TOKEN
        conn = http.client.HTTPConnection(web.HOST, self.port, timeout=5)
        try:
            conn.request(method, path, data, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            return res.status, json.loads(raw) if raw[:1] in (b'{', b'[') else raw
        finally:
            conn.close()

    def create(self, **extra):
        return self.call('POST', '/api/schedules', {'name': 'nightly', 'cron': '0 9 * * 1-5',
                                                    'template': dict(TEMPLATE), **extra})

    def test_page_serves_schedules_script_and_nav(self):
        status, body = self.call('GET', '/schedules.js')
        self.assertEqual(status, 200)
        self.assertIn(b'schedules-body', body)
        status, html = self.call('GET', '/')
        self.assertIn(b'id="schedules"', html)
        self.assertNotIn(b'innerHTML', (web.STATIC / 'schedules.js').read_bytes())

    def test_full_lifecycle(self):
        status, data = self.create()
        self.assertEqual(status, 201, data)
        sid = data['schedule']['id']
        self.assertTrue(data['schedule']['enabled'])
        self.assertTrue(data['schedule']['next_run'])

        status, listing = self.call('GET', '/api/schedules')
        self.assertEqual(status, 200)
        self.assertEqual([s['id'] for s in listing['schedules']], [sid])
        self.assertFalse(listing['scheduler']['online'])
        self.assertEqual(listing['schedules'][0]['template']['country'], 'UK')

        status, data = self.call('POST', f'/api/schedules/{sid}/toggle', {'enabled': False})
        self.assertEqual((status, data['schedule']['enabled'], data['schedule']['next_run']), (200, False, None))

        status, data = self.call('POST', f'/api/schedules/{sid}/update',
                                 {'name': 'renamed', 'cron': '30 8 * * *'})
        self.assertEqual((status, data['schedule']['name'], data['schedule']['cron']), (200, 'renamed', '30 8 * * *'))

        status, data = self.call('POST', f'/api/schedules/{sid}/run-now', {})
        self.assertEqual(status, 202)
        status, data = self.call('GET', f'/api/schedules/{sid}/runs')
        self.assertEqual([r['status'] for r in data['runs']], ['PENDING'])

        status, data = self.call('POST', f'/api/schedules/{sid}/delete', {})
        self.assertEqual(status, 200)
        self.assertEqual(self.call('GET', '/api/schedules')[1]['schedules'], [])

    def test_preview_returns_five_runs_and_explains_errors(self):
        status, data = self.call('GET', '/api/schedules/preview?cron=0%209%20*%20*%201-5')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['next']), 5)
        for bad, fragment in (('nope', '5 段'), ('*%20*%20*%20*%20*', '最小')):
            status, data = self.call('GET', '/api/schedules/preview?cron=' + bad)
            self.assertEqual(status, 400)
            self.assertIn(fragment, data['error'])

    def test_validation_errors_are_400_and_nothing_is_saved(self):
        for payload in ({'cron': '0 * * * *', 'template': {**TEMPLATE, 'jenkins_password': 'x'}},
                        {'cron': '0 * * * *', 'template': {**TEMPLATE, 'plugin_version': 'bad'}},
                        {'cron': '* * * * *', 'template': dict(TEMPLATE)},
                        {'cron': '0 * * * *'}):
            with self.subTest(payload=payload):
                status, _ = self.call('POST', '/api/schedules', {'name': 'x', **payload})
                self.assertEqual(status, 400)
        self.assertEqual(self.call('GET', '/api/schedules')[1]['schedules'], [])

    def test_post_requires_csrf_and_ids_are_validated(self):
        status, _ = self.call('POST', '/api/schedules', {'name': 'x'}, csrf=False)
        self.assertEqual(status, 403)
        status, _ = self.call('POST', '/api/schedules/../x/delete', {})
        self.assertEqual(status, 404)     # never even routed
        for path in ('/api/schedules/sch-zzzz/delete', '/api/schedules/sch-00000000/delete'):
            status, _ = self.call('POST', path, {})
            self.assertEqual(status, 400, path)
        status, _ = self.call('GET', '/api/schedules/sch-0000000g/runs')
        self.assertEqual(status, 400)


class RealCreateBuildHandOffTests(unittest.TestCase):
    """A saved template must be accepted by the REAL create_build() the Start button uses."""

    def test_due_schedule_creates_a_real_queued_task(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)), \
                patch('appbuilder.cli.doctor_check', return_value=True), \
                patch('appbuilder.cli.spawn') as spawn, \
                patch('appbuilder.web.token_or_error', return_value='t'):
            row = schedules.create_schedule('nightly', '0 * * * *', dict(TEMPLATE), now=datetime(2026, 10, 10, 10, 15))
            result = schedules.tick(datetime(2026, 10, 10, 11, 0, 5))
            self.assertEqual(result, {'queued': 1, 'started': 1})
            tasks = store.task_list()
            self.assertEqual(len(tasks), 1)
            params = json.loads(tasks[0]['params'])
            self.assertEqual((params['plugin_source'], params['gmx_source'], params['project'], params['country']),
                             ('develop', 'develop', 'PayByBank', 'UK'))
            self.assertIs(params['visible'], False)
            self.assertTrue(params['plugin_branch'].startswith('release/1.3.'))
            spawn.assert_called_once_with(tasks[0]['id'], wait=False)
            run = schedules.last_run(row['id'])
            self.assertEqual((run['status'], run['task_id']), ('STARTED', tasks[0]['id']))

    def test_full_form_body_incl_advanced_fields_reaches_the_real_build(self):
        body = {**TEMPLATE, 'gmx_build_type': 'Release', 'gmx_force': False, 'build_timeout_minutes': '90',
                'visible': False, 'verbose': True}
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)), \
                patch('appbuilder.cli.doctor_check', return_value=True), patch('appbuilder.cli.spawn'), \
                patch('appbuilder.web.token_or_error', return_value='t'):
            schedules.create_schedule('adv', '0 * * * *', body, now=datetime(2026, 10, 10, 10, 15))
            schedules.tick(datetime(2026, 10, 10, 11, 0, 5))
            params = json.loads(store.task_list()[0]['params'])
            self.assertEqual(params['gmx_parameters']['BUILD_TYPE'], 'Release')
            self.assertIs(params['gmx_parameters']['FORCE'], False)
            self.assertEqual(params['jenkins_settings']['build_timeout_minutes'], 90)

    def test_default_concurrency_limit_is_shared_with_manual_builds(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)), \
                patch('appbuilder.cli.doctor_check', return_value=True), \
                patch('appbuilder.cli.spawn'), patch('appbuilder.web.token_or_error', return_value='t'):
            schedules.create_schedule('a', '0 * * * *', dict(TEMPLATE), now=datetime(2026, 10, 10, 10, 15))
            schedules.create_schedule('b', '5 * * * *', dict(TEMPLATE), now=datetime(2026, 10, 10, 10, 15))
            schedules.create_schedule('c', '10 * * * *', dict(TEMPLATE), now=datetime(2026, 10, 10, 10, 15))
            schedules.tick(datetime(2026, 10, 10, 11, 11))
            self.assertEqual(len(store.task_list()), 2)    # config max_parallel=2: third waits in the queue
            pending = [r for s in schedules.list_schedules() for r in schedules.list_runs(s['id']) if r['status'] == 'PENDING']
            self.assertEqual(len(pending), 1)


if __name__ == '__main__':
    unittest.main()

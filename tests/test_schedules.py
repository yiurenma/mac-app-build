"""Scheduler core: queueing, catch-up, concurrency, validation. No Jenkins/Git/network."""
import json
import plistlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from appbuilder import cron, schedules, scheduler, store

CONFIG = {'github': {'plugin_url': 'https://git.invalid/p', 'gmx_url': 'https://git.invalid/g'},
          'jenkins': {'plugin_parent': 'https://jenkins.invalid/job/p/job/',
                      'gmx_parent': 'https://jenkins.invalid/job/g/job/', 'browser_channel': 'chrome'},
          'build': {'max_parallel': 2, 'max_changed_lines': 100, 'upload_success_pattern': ''}}
TEMPLATE = {'plugin_branch': 'develop', 'gmx_branch': 'develop', 'plugin_version': '1.3',
            'country': 'uk', 'project_name': 'PayByBank'}


def dt(*a):
    return datetime(*a)


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(store, 'DATA', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.started = []
        self.active = 0

    def starter(self, body):
        self.started.append(body)
        return {'id': f'202610101200-{len(self.started):06x}'}

    def make(self, cron_expr='0 * * * *', now=dt(2026, 10, 10, 10, 15), **kw):
        return schedules.create_schedule('nightly', cron_expr, dict(TEMPLATE), now=now, config=CONFIG, **kw)


class ValidationTests(SchedulerCase):
    def test_create_normalizes_and_schedules_next_run(self):
        row = self.make()
        self.assertEqual(row['next_run'], '2026-10-10T11:00:00')
        self.assertEqual(json.loads(row['params'])['country'], 'UK')

    def test_secrets_are_refused_not_stored(self):
        for key in ('jenkins_password', 'jenkins_user', 'github_token'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Keychain'):
                schedules.create_schedule('x', '0 * * * *', {**TEMPLATE, key: 'secret'}, config=CONFIG)

    def test_bad_template_cron_and_aggressive_interval_rejected(self):
        with self.assertRaises(ValueError):
            schedules.create_schedule('x', '0 * * * *', {**TEMPLATE, 'plugin_version': 'abc'}, config=CONFIG)
        with self.assertRaises(ValueError):
            schedules.create_schedule('x', '0 * * * *', {**TEMPLATE, 'bogus': 1}, config=CONFIG)
        with self.assertRaises(cron.CronError):
            schedules.create_schedule('x', 'nope', dict(TEMPLATE), config=CONFIG)
        with self.assertRaisesRegex(cron.CronError, '最小'):
            schedules.create_schedule('x', '* * * * *', dict(TEMPLATE), config=CONFIG)
        self.assertEqual(schedules.list_schedules(), [])

    def test_update_reschedules_and_toggle_clears_next_run(self):
        row = self.make()
        row = schedules.update_schedule(row['id'], expression='30 12 * * *', now=dt(2026, 10, 10, 10, 20), config=CONFIG)
        self.assertEqual(row['next_run'], '2026-10-10T12:30:00')
        row = schedules.update_schedule(row['id'], enabled=False, config=CONFIG)
        self.assertIsNone(row['next_run'])
        row = schedules.update_schedule(row['id'], enabled=True, now=dt(2026, 10, 11, 8, 0), config=CONFIG)
        self.assertEqual(row['next_run'], '2026-10-11T12:30:00')  # no catch-up for the disabled period


class TickTests(SchedulerCase):
    def tick(self, now, active=0, config=CONFIG):
        with patch.object(store, 'active_count', lambda: self.active + len(self.started) if active is None else active + len(self.started)):
            return schedules.tick(now, start=self.starter, config=config)

    def test_nothing_happens_before_due_time(self):
        self.make()
        self.assertEqual(self.tick(dt(2026, 10, 10, 10, 59, 59)), {'queued': 0, 'started': 0})

    def test_due_schedule_starts_a_task_with_shared_defaults(self):
        row = self.make()
        self.assertEqual(self.tick(dt(2026, 10, 10, 11, 0, 5)), {'queued': 1, 'started': 1})
        body = self.started[0]
        self.assertIs(body['visible'], True)   # same default as New build
        self.assertIs(body['verbose'], True)
        self.assertNotIn('jenkins_password', body)
        run = schedules.last_run(row['id'])
        self.assertEqual((run['status'], run['planned']), ('STARTED', '2026-10-10T11:00:00'))
        self.assertTrue(run['task_id'])
        self.assertEqual(schedules.get_schedule(row['id'])['next_run'], '2026-10-10T12:00:00')

    def test_same_occurrence_is_never_run_twice(self):
        self.make()
        self.tick(dt(2026, 10, 10, 11, 0, 5))
        self.tick(dt(2026, 10, 10, 11, 0, 20))
        self.tick(dt(2026, 10, 10, 11, 30))
        self.assertEqual(len(self.started), 1)

    def test_sleeping_mac_catches_up_only_the_latest_missed_run(self):
        row = self.make()
        # asleep from 10:15 until 14:20 -> 11:00, 12:00, 13:00, 14:00 were missed
        self.assertEqual(self.tick(dt(2026, 10, 10, 14, 20)), {'queued': 1, 'started': 1})
        run = schedules.last_run(row['id'])
        self.assertEqual(run['planned'], '2026-10-10T14:00:00')
        self.assertEqual(run['missed'], 3)
        self.assertIn('错过 3 次', run['note'])
        self.assertEqual(schedules.get_schedule(row['id'])['next_run'], '2026-10-10T15:00:00')

    def test_full_concurrency_queues_instead_of_dropping(self):
        row = self.make()
        self.assertEqual(self.tick(dt(2026, 10, 10, 11, 0, 5), active=2), {'queued': 1, 'started': 0})
        self.assertEqual(self.started, [])
        self.assertEqual(schedules.last_run(row['id'])['status'], 'PENDING')
        # a slot frees up later: the queued run starts, nothing new is invented
        self.assertEqual(self.tick(dt(2026, 10, 10, 11, 10), active=0), {'queued': 0, 'started': 1})
        self.assertEqual(schedules.last_run(row['id'])['status'], 'STARTED')
        self.assertEqual(schedules.last_run(row['id'])['planned'], '2026-10-10T11:00:00')

    def test_queued_runs_start_oldest_first_and_respect_the_limit(self):
        a = self.make('0 * * * *')
        b = schedules.create_schedule('other', '30 * * * *', dict(TEMPLATE), now=dt(2026, 10, 10, 10, 15), config=CONFIG)
        self.tick(dt(2026, 10, 10, 11, 31), active=2)         # 11:00 and 11:30 both queue
        self.assertEqual(self.started, [])
        cfg = {**CONFIG, 'build': {**CONFIG['build'], 'max_parallel': 3}}
        self.tick(dt(2026, 10, 10, 11, 32), active=2, config=cfg)   # exactly one free slot
        self.assertEqual(len(self.started), 1)
        self.assertEqual(schedules.last_run(a['id'])['status'], 'STARTED')   # 11:00 before 11:30
        self.assertEqual(schedules.last_run(b['id'])['status'], 'PENDING')

    def test_start_failure_is_recorded_and_does_not_block_others(self):
        row = self.make()
        def failing(body):
            raise RuntimeError('启动前环境检查未通过 token=abc12345')
        with patch.object(store, 'active_count', lambda: 0):
            schedules.tick(dt(2026, 10, 10, 11, 0, 5), start=failing, config=CONFIG)
        run = schedules.last_run(row['id'])
        self.assertEqual(run['status'], 'FAILED')
        self.assertIn('环境检查', run['note'])
        self.assertNotIn('abc12345', run['note'])
        self.assertEqual(schedules.get_schedule(row['id'])['next_run'], '2026-10-10T12:00:00')

    def test_race_on_concurrency_limit_keeps_run_pending(self):
        row = self.make()
        def busy(body):
            raise ValueError('已达到并发任务上限，请先等待或恢复已有任务')
        with patch.object(store, 'active_count', lambda: 0):
            schedules.tick(dt(2026, 10, 10, 11, 0, 5), start=busy, config=CONFIG)
        self.assertEqual(schedules.last_run(row['id'])['status'], 'PENDING')

    def test_disabled_schedule_never_fires_and_delete_removes_runs(self):
        row = self.make()
        schedules.update_schedule(row['id'], enabled=False, config=CONFIG)
        self.assertEqual(self.tick(dt(2026, 10, 10, 13, 0)), {'queued': 0, 'started': 0})
        schedules.update_schedule(row['id'], enabled=True, now=dt(2026, 10, 10, 13, 5), config=CONFIG)
        self.tick(dt(2026, 10, 10, 14, 0, 5))
        schedules.delete_schedule(row['id'])
        self.assertEqual(schedules.list_schedules(), [])
        self.assertEqual(schedules.list_runs(row['id']), [])

    def test_run_now_queues_once_and_uses_normal_dispatch(self):
        row = self.make()
        schedules.run_now(row['id'], now=dt(2026, 10, 10, 10, 20, 0))
        with self.assertRaises(ValueError):
            schedules.run_now(row['id'], now=dt(2026, 10, 10, 10, 20, 0))  # double click
        self.assertEqual(self.tick(dt(2026, 10, 10, 10, 20, 5)), {'queued': 0, 'started': 1})
        self.assertEqual(schedules.last_run(row['id'])['note'], '手动触发')

    def test_hidden_schedule_and_manual_run_keep_mode_and_distinct_task_ids(self):
        row=schedules.create_schedule('hidden','0 * * * *',{**TEMPLATE,'visible':False},now=dt(2026,10,10,10,15),config=CONFIG)
        with patch.object(store,'active_count',return_value=0):
            schedules.tick(dt(2026,10,10,11,0,1),start=self.starter,config=CONFIG)
            receipt=schedules.run_now(row['id'],now=dt(2026,10,10,11,0,2))
            schedules.dispatch_pending(start=self.starter,config=CONFIG)
        runs=schedules.list_runs(row['id'])
        self.assertEqual(len({r['task_id'] for r in runs}),2)
        self.assertEqual(receipt['status'],'PENDING')
        self.assertTrue(all(body['visible'] is False for body in self.started))

    def test_manual_run_at_capacity_has_pending_id_and_no_worker(self):
        row=self.make()
        receipt=schedules.run_now(row['id'])
        with patch.object(store,'active_count',return_value=2),patch('builtins.print') as output:
            self.assertEqual(schedules.dispatch_pending(start=self.starter,config=CONFIG),0)
        run=schedules.last_run(row['id'])
        self.assertEqual(run['id'],receipt['run_id']);self.assertIsNone(run['task_id'])
        self.assertEqual(run['status'],'PENDING');self.assertFalse(self.started)
        self.assertIn('尚未创建',output.call_args.args[0])


class SchedulerProcessTests(SchedulerCase):
    def test_heartbeat_online_then_stale(self):
        with patch.object(scheduler, 'DATA', Path(self.tmp.name)):
            self.assertFalse(scheduler.heartbeat_status()['online'])
            scheduler.write_heartbeat()
            self.assertFalse(scheduler.heartbeat_status()['online'])
            with scheduler.lock_path().open('a+b') as handle:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(scheduler.heartbeat_status()['online'])
            self.assertFalse(scheduler.heartbeat_status(now=__import__('time').time() + 600)['online'])

    def test_run_forever_survives_a_failing_tick_and_single_instance(self):
        with patch.object(scheduler, 'DATA', Path(self.tmp.name)):
            calls = []
            def boom(**kw):
                calls.append(1)
                raise RuntimeError('db locked')
            with patch.object(schedules, 'tick', boom):
                scheduler.run_forever(interval=0, max_loops=3, sleep=lambda s: None)
            self.assertEqual(len(calls), 3)   # kept going after every failure
            with scheduler.lock_path().open('a+b') as other:
                import fcntl
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(scheduler.AlreadyRunning):
                    scheduler.run_forever(max_loops=1, sleep=lambda s: None)

    def test_launch_agent_plist_is_restartable_and_keeps_mac_awake(self):
        data = plistlib.loads(plistlib.dumps(scheduler.build_plist(python='/x/.venv/bin/python', root='/x', data='/x/.appbuilder')))
        self.assertEqual(data['Label'], 'com.mac-app-builder.scheduler')
        self.assertTrue(data['RunAtLoad'])
        self.assertTrue(data['KeepAlive'])
        args = data['ProgramArguments']
        self.assertEqual(args[-5:], ['/x/.venv/bin/python', '-m', 'appbuilder', 'scheduler', 'run'])
        self.assertEqual(data['WorkingDirectory'], '/x')
        self.assertIn('/usr/bin', data['EnvironmentVariables']['PATH'])   # git must be findable under launchd


if __name__ == '__main__':
    unittest.main()

"""Transactional schedule lifecycle through real dispatch/create/store; no external work."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from appbuilder import schedules, scheduler, store, web, runner, schedule_api, cli
from appbuilder.config import load_config

NOW = datetime(2026, 10, 12, 10, 15)
TEMPLATE = dict(plugin_branch='develop', gmx_branch='develop', plugin_version='1.3', country='UK', project_name='PayByBank')


class StopSuccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for target, value in [('appbuilder.store.DATA', Path(self.tmp.name)),
                              ('appbuilder.scheduler.DATA', Path(self.tmp.name)),
                              ('appbuilder.cli.doctor_check', lambda *a, **k: True),
                              ('appbuilder.web.token_or_error', lambda *a: 'fixture')]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        self.real_spawn = cli.spawn
        self.spawn = patch('appbuilder.cli.spawn').start(); self.addCleanup(patch.stopall)
        self.config = load_config()

    def make(self, **kw):
        return schedules.create_schedule('cycle', '0 * * * *', TEMPLATE, now=NOW, **kw)

    def start(self, row):
        schedules.run_now(row['id'], now=NOW)
        self.assertEqual(schedules.dispatch_pending(now=NOW), 1)
        return schedules.last_run(row['id'])['task_id']

    def success(self, task_id):
        store.update_task(task_id, status='SUCCESS', stage='FINISHED')

    def test_old_schema_migration_is_idempotent_and_defaults_off(self):
        with closing(store.connect()) as db, db:
            db.executescript(schedules.SCHEMA)
            db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?)', ('sch-12345678', 'old', '0 * * * *', 1, json.dumps(TEMPLATE), None, 'old', 'old'))
        for _ in range(2):
            row = schedules.get_schedule('sch-12345678')
            self.assertFalse(row['stop_after_success']); self.assertEqual(row['generation'], 1)
            self.assertEqual(json.loads(row['params']), TEMPLATE)

    def test_new_api_defaults_on_but_explicit_off_is_preserved(self):
        for body, expected in [({}, True), ({'stop_after_success':False}, False)]:
            status, response = schedule_api.handle_post('/api/schedules',
                {'name':'new', 'cron':'0 * * * *', 'template':TEMPLATE, **body})
            self.assertEqual(status,201)
            self.assertEqual(response['schedule']['stop_after_success'],expected)

    def test_success_is_attached_before_spawn_and_stops_atomically(self):
        row = self.make(stop_after_success=True)
        def complete(task_id, **kw):
            run = schedules.last_run(row['id'])
            self.assertEqual(run['task_id'], task_id); self.assertEqual(run['status'], 'STARTED')
            self.success(task_id)
        self.spawn.side_effect = complete
        task_id = self.start(row)
        stopped = schedules.get_schedule(row['id'])
        self.assertFalse(stopped['enabled']); self.assertIsNone(stopped['next_run'])
        self.assertEqual(stopped['success_task_id'], task_id)
        self.assertEqual(schedules.tick(datetime(2026, 10, 13, 15, 0)), {'queued': 0, 'started': 0})
        with self.assertRaisesRegex(ValueError, '重新启用'): schedules.run_now(row['id'])

    def test_failure_continues_and_default_off_keeps_repeating(self):
        for option in (True, False):
            row = self.make(stop_after_success=option); task = self.start(row)
            store.update_task(task, status='FAILED')
            self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
            schedules.enqueue_due(datetime(2026, 10, 12, 11, 0))
            self.assertGreaterEqual(schedules.dispatch_pending(), 1)
            if not option:
                self.success(schedules.last_run(row['id'])['task_id'])
                self.assertTrue(schedules.get_schedule(row['id'])['enabled'])

    def test_pending_cancelled_other_cycles_and_started_tasks_untouched(self):
        a = self.make(stop_after_success=True); first = self.start(a)
        b = self.make(stop_after_success=True); other = self.start(b)
        schedules.run_now(a['id'], now=datetime(2026, 10, 12, 10, 16))
        self.assertEqual(schedules.dispatch_pending(), 0)
        self.success(first)
        self.assertEqual(schedules.last_run(a['id'])['status'], 'CANCELLED')
        self.assertTrue(schedules.get_schedule(b['id'])['enabled'])
        self.assertEqual(store.task(other)['status'], 'QUEUED')

    def test_restart_reconciles_persisted_success_before_catchup(self):
        row = self.make(stop_after_success=True); task = self.start(row)
        with closing(store.connect()) as db, db:
            db.execute("UPDATE tasks SET status='SUCCESS',stage='FINISHED' WHERE id=?", (task,))
        with patch('appbuilder.scheduler.write_heartbeat'), patch('appbuilder.schedules._default_start') as launch:
            scheduler.run_forever(interval=0, max_loops=1, sleep=lambda _: None)
        launch.assert_not_called(); self.assertEqual(schedules.get_schedule(row['id'])['success_task_id'], task)

    def test_real_scheduler_entry_stops_after_fast_worker_success(self):
        row = self.make(stop_after_success=True)
        schedules.run_now(row['id'], now=NOW)
        self.spawn.side_effect = lambda task_id, **kw: self.success(task_id)
        with patch('appbuilder.scheduler.write_heartbeat'):
            scheduler.run_forever(interval=0, max_loops=2, sleep=lambda _: None)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])
        self.assertTrue(all(r['status'] != 'PENDING' for r in schedules.list_runs(row['id'])))

    def test_restart_releases_only_claims_without_tasks(self):
        row = self.make(stop_after_success=True)
        schedules.run_now(row['id'], now=NOW); run = schedules.last_run(row['id'])
        claim, _ = schedules._claim_pending(run['id'], NOW)
        schedules.recover_startup()
        self.assertEqual(schedules.last_run(row['id'])['status'], 'PENDING')
        schedules.dispatch_pending(); task = schedules.last_run(row['id'])['task_id']
        schedules.recover_startup(); schedules.dispatch_pending()
        self.assertEqual(self.spawn.call_count, 1); self.assertEqual(schedules.last_run(row['id'])['task_id'], task)

    def test_reenable_is_new_generation_old_resumed_success_cannot_stop_it(self):
        row = self.make(stop_after_success=True); old = self.start(row)
        store.update_task(old, status='FAILED')
        schedules.update_schedule(row['id'], enabled=False)
        new = schedules.update_schedule(row['id'], enabled=True, restart_cycle=True, now=NOW)
        self.assertEqual(new['generation'], 2)
        store.reserve_resume(self.config, old); self.success(old)
        self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
        schedules.run_now(row['id'], now=datetime(2026, 10, 12, 10, 17)); schedules.dispatch_pending()
        self.success(schedules.last_run(row['id'])['task_id'])
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])

    def test_reenable_after_success_clears_latch_but_keeps_history(self):
        row = self.make(stop_after_success=True); task = self.start(row); self.success(task)
        new = schedules.update_schedule(row['id'], enabled=True, restart_cycle=True, now=NOW)
        self.assertEqual(new['generation'], 2); self.assertIsNone(new['success_task_id'])
        schedules.recover_startup(); self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
        self.assertEqual(schedules.last_run(row['id'])['task_id'], task)

    def test_enabling_stop_option_starts_new_round_not_historical_success(self):
        row = self.make(stop_after_success=False); old = self.start(row); self.success(old)
        changed = schedules.update_schedule(row['id'],stop_after_success=True,now=NOW)
        self.assertTrue(changed['enabled']); self.assertEqual(changed['generation'],2)
        self.assertIsNone(changed['success_task_id'])
        schedules.recover_startup(); self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
        schedules.run_now(row['id'],now=datetime(2026,10,12,10,18)); schedules.dispatch_pending()
        self.success(schedules.last_run(row['id'])['task_id'])
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])

    def test_paused_manual_run_allowed_but_success_stopped_manual_run_refused(self):
        row = self.make(stop_after_success=True,enabled=False)
        task = self.start(row); self.success(task)
        with self.assertRaisesRegex(ValueError,'重新启用'):schedules.run_now(row['id'])

    def test_stale_editor_cannot_reenable_success_stopped_cycle(self):
        row = self.make(stop_after_success=True); task = self.start(row); self.success(task)
        with self.assertRaisesRegex(ValueError, '计划已变化'):
            schedules.update_schedule(row['id'], name='stale', enabled=True, expected_revision=row['revision'])
        with self.assertRaisesRegex(ValueError, '重新启用'):
            schedules.update_schedule(row['id'], name='stale', enabled=True)
        schedules.update_schedule(row['id'], name='rename only')
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])

    def test_two_dispatchers_and_two_due_runs_launch_only_one_task(self):
        row = self.make(stop_after_success=True)
        schedules.run_now(row['id'], now=NOW)
        schedules.run_now(row['id'], now=datetime(2026, 10, 12, 10, 16))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: schedules.dispatch_pending(), range(2)))
        self.assertEqual(sum(results), 1); self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(len(store.task_list()), 1)

    def test_cancel_between_claim_and_create_never_spawns_or_leaves_task(self):
        row = self.make(stop_after_success=True); schedules.run_now(row['id'], now=NOW)
        run = schedules.last_run(row['id']); claim, template = schedules._claim_pending(run['id'], NOW)
        schedules.update_schedule(row['id'], enabled=False)
        with self.assertRaises(schedules.RunUnavailable): web.create_build(template, schedule_claim=claim)
        self.spawn.assert_not_called(); self.assertEqual(store.task_list(), [])

    def test_claimed_run_cancelled_by_success_not_released_to_pending(self):
        row = self.make(stop_after_success=False); task = self.start(row)
        schedules.run_now(row['id'], now=datetime(2026, 10, 12, 10, 16))
        claim, template = schedules._claim_pending(schedules.last_run(row['id'])['id'], NOW)
        with closing(schedules.connect()) as db, db:
            db.execute('UPDATE schedules SET stop_after_success=1 WHERE id=?',(row['id'],))
        self.success(task)
        with self.assertRaises(schedules.RunUnavailable): web.create_build(template, schedule_claim=claim)
        schedules._release_claim(claim, 'PENDING')
        self.assertEqual(schedules.last_run(row['id'])['status'], 'CANCELLED')

    def test_success_stop_rolls_back_with_task_status_on_database_failure(self):
        row = self.make(); task = self.start(row)
        with patch('appbuilder.schedules.reconcile_successes',side_effect=RuntimeError('fixture database error')):
            with self.assertRaisesRegex(RuntimeError,'database'):
                self.success(task)
        self.assertEqual(store.task(task)['status'],'QUEUED')
        self.assertTrue(schedules.get_schedule(row['id'])['enabled'])

    def test_due_enqueue_racing_success_cannot_leave_pending_run(self):
        row = self.make(); task = self.start(row)
        barrier = threading.Barrier(2)
        def complete():barrier.wait();self.success(task)
        def enqueue():barrier.wait();schedules.enqueue_due(datetime(2026,10,12,11,0))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(complete);b=pool.submit(enqueue);a.result();b.result()
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])
        self.assertFalse(any(r['status'] in ('PENDING','CLAIMED') for r in schedules.list_runs(row['id'])))
        self.assertEqual(schedules.dispatch_pending(),0)
        self.assertEqual(self.spawn.call_count,1)

    def test_spawn_error_does_not_erase_task_link_or_retry_dispatch(self):
        row = self.make(stop_after_success=True); self.spawn.side_effect = RuntimeError('spawn uncertain')
        schedules.run_now(row['id'], now=NOW); schedules.dispatch_pending()
        run = schedules.last_run(row['id']); self.assertIsNotNone(run['task_id'])
        self.assertEqual(run['status'], 'STARTED')
        schedules.recover_startup(); schedules.dispatch_pending()
        self.assertEqual(self.spawn.call_count, 1)

    def test_popen_failure_releases_slots_and_next_due_can_run(self):
        row = self.make(); schedules.run_now(row['id'], now=NOW)
        self.spawn.side_effect = self.real_spawn
        with patch('appbuilder.cli.subprocess.Popen', side_effect=OSError('fixture launch denied')):
            self.assertEqual(schedules.dispatch_pending(), 0)
        failed = schedules.last_run(row['id'])
        self.assertIsNotNone(failed['task_id'])
        self.assertEqual(store.task(failed['task_id'])['status'], 'FAILED')
        self.assertEqual(store.active_count(), 0)
        self.assertIn('下次计划可继续', failed['note'])
        self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
        self.spawn.side_effect = None
        schedules.enqueue_due(datetime(2026, 10, 12, 11, 0))
        self.assertEqual(schedules.dispatch_pending(), 1)
        self.assertNotEqual(schedules.last_run(row['id'])['task_id'], failed['task_id'])

    def test_log_open_failure_marks_unstarted_task_failed(self):
        row = self.make(); task = self.start(row)
        with patch('pathlib.Path.open', side_effect=PermissionError('fixture log denied')), \
                patch('appbuilder.cli.subprocess.Popen') as popen:
            with self.assertRaises(PermissionError): self.real_spawn(task)
        popen.assert_not_called()
        self.assertEqual(store.task(task)['status'], 'FAILED')
        self.assertEqual(store.active_count(), 0)

    def test_error_after_popen_keeps_possible_worker_reserved(self):
        row = self.make(); task = self.start(row)
        with patch('appbuilder.cli.subprocess.Popen') as popen:
            popen.return_value.stdin.write.side_effect = BrokenPipeError('fixture pipe closed')
            with self.assertRaises(BrokenPipeError):
                self.real_spawn(task, transient_credentials={'user':'fixture'})
        self.assertEqual(store.task(task)['status'], 'QUEUED')
        self.assertEqual(store.active_count(), 1)
        self.assertEqual(schedules.last_run(row['id'])['task_id'], task)

    def test_resume_cannot_overlap_another_task_from_same_schedule(self):
        row = self.make(stop_after_success=True); old = self.start(row)
        store.update_task(old, status='FAILED')
        schedules.run_now(row['id'], now=datetime(2026, 10, 12, 10, 17)); schedules.dispatch_pending()
        with self.assertRaisesRegex(ValueError, '本计划已有'):
            store.reserve_resume(self.config, old)
        self.assertEqual(store.task(old)['status'], 'FAILED')

    def test_success_requires_finished_and_notification_failure_does_not_repeat(self):
        row = self.make(stop_after_success=True); task = self.start(row)
        store.update_task(task, status='SUCCESS', stage='TESTFAIRY_LOOKUP')
        self.assertTrue(schedules.get_schedule(row['id'])['enabled'])
        with patch('appbuilder.runner._run_unlocked', side_effect=lambda *a, **kw: self.success(task)), \
                patch('appbuilder.outlook.notify_outcome', side_effect=RuntimeError('fixture mail failure')):
            runner.run(task)
        self.assertFalse(schedules.get_schedule(row['id'])['enabled'])
        self.assertEqual(store.task(task)['status'], 'SUCCESS')

    def test_reading_template_does_not_require_live_auth_or_expose_secrets(self):
        row = self.make()
        raw = copy.deepcopy(row); template = json.loads(raw['params'])
        template.update(testfairy_enabled=True, testfairy_save_credentials=True, testfairy_password='must-not-return')
        raw['params'] = json.dumps(template)
        with patch('appbuilder.testfairy.saved_auth_available', return_value=False):
            public = schedule_api.public_schedule(raw)
            self.assertNotIn('testfairy_password', public['template'])
            with self.assertRaisesRegex(ValueError, '认证'):
                schedules.validate_template({k:v for k,v in template.items() if k not in schedules.SECRET_KEYS})

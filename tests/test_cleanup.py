"""Disk cleanup of finished tasks' git working copies. Local files only."""
import contextlib
import io
import os
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from appbuilder import cleanup, cli, schedules, store

NOW = datetime(2026, 10, 26, 12, 0, 0)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class CleanupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = patch.object(store, 'DATA', Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def add(self, task_id, status, days_ago=0, pid=None):
        with closing(store.connect()) as db, db:
            db.execute('INSERT INTO tasks(id,created,stamp,status,pid,params,stage) VALUES(?,?,?,?,?,?,?)',
                       (task_id, iso(days_ago), task_id[:12], status, pid, '{}', 'INIT'))
        home = store.task_folder(task_id)
        for name in ('plugin', 'gmx'):
            pack = home / name / '.git' / 'objects' / 'pack.bin'
            pack.parent.mkdir(parents=True)
            pack.write_bytes(b'x' * 60_000)
            pack.chmod(0o444)                         # like real git pack files
        (home / 'screenshots').mkdir()
        (home / 'screenshots' / 'a.png').write_bytes(b'png')
        for keep in ('run.log', 'state.json', 'result.json'):
            (home / keep).write_text('keep me')
        return home


class PlanApplyTests(CleanupCase):
    def test_success_task_loses_work_copies_but_keeps_logs_and_results(self):
        home = self.add('202610010900-aaaaaa', 'SUCCESS', 1)
        items = cleanup.plan(now=NOW)
        self.assertEqual([i['task_id'] for i in items], ['202610010900-aaaaaa'])
        self.assertGreater(items[0]['bytes'], 100_000)
        result = cleanup.apply(items)
        self.assertEqual((result['tasks'], result['errors']), (1, []))
        self.assertGreater(result['freed'], 100_000)
        self.assertFalse((home / 'plugin').exists() or (home / 'gmx').exists())
        for keep in ('run.log', 'state.json', 'result.json', 'screenshots/a.png'):
            self.assertTrue((home / keep).exists(), keep)
        self.assertTrue((home / cleanup.MARKER).exists())
        self.assertEqual(cleanup.plan(now=NOW), [])        # idempotent

    def test_planning_never_deletes(self):
        home = self.add('202610010900-aaaaaa', 'SUCCESS')
        cleanup.plan(now=NOW)
        self.assertTrue((home / 'plugin').is_dir() and (home / 'gmx').is_dir())

    def test_unsuccessful_tasks_are_kept_for_resume_until_old(self):
        self.add('202610250900-bbbbbb', 'FAILED', 1)
        self.add('202610240900-cccccc', 'NEEDS_ATTENTION', 6)
        old = self.add('202610100900-dddddd', 'TIMEOUT', 16)
        items = cleanup.plan(keep_days=7, now=NOW)
        self.assertEqual([i['task_id'] for i in items], ['202610100900-dddddd'])
        self.assertIn('超过 7 天', items[0]['reason'])
        cleanup.apply(items)
        self.assertFalse((old / 'plugin').exists())
        self.assertEqual({i['task_id'] for i in cleanup.plan(keep_days=0, now=NOW)},
                         {'202610250900-bbbbbb', '202610240900-cccccc'})

    def test_live_worker_is_never_touched(self):
        home = self.add('202610260900-eeeeee', 'RUNNING', 30, pid=os.getpid())   # this process is "alive"
        self.add('202610260901-ffffff', 'QUEUED', 30, pid=os.getpid())
        self.assertEqual(cleanup.plan(keep_days=0, now=NOW), [])
        self.assertTrue((home / 'plugin').is_dir())

    def test_symlinked_work_copy_is_not_followed(self):
        home = self.add('202610010900-aaaaaa', 'SUCCESS')
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'precious.txt').write_text('do not delete')
        os.replace(home / 'plugin', Path(self.tmp.name) / 'moved')
        (home / 'plugin').symlink_to(outside, target_is_directory=True)
        items = cleanup.plan(now=NOW)
        self.assertEqual([p.name for p in items[0]['paths']], ['gmx'])
        cleanup.apply(items)
        self.assertTrue((outside / 'precious.txt').exists())
        self.assertTrue((home / 'plugin').is_symlink())

    def test_breakdown_separates_work_copies_from_logs(self):
        self.add('202610010900-aaaaaa', 'SUCCESS')
        runs, work, other = cleanup.breakdown()
        self.assertEqual(runs, 1)
        self.assertGreater(work, 100_000)
        self.assertLess(other, work)


class SchedulerAutoCleanTests(CleanupCase):
    def mark_scheduled(self, task_id):
        with closing(schedules.connect()) as db, db:
            db.execute("INSERT INTO schedule_runs(schedule_id,planned,status,task_id,created) VALUES('sch-00000001',?,'STARTED',?,?)",
                       (task_id, task_id, iso(0)))

    def test_only_tasks_created_by_schedules_are_cleaned_automatically(self):
        scheduled = self.add('202610010900-aaaaaa', 'SUCCESS')
        manual = self.add('202610010901-bbbbbb', 'SUCCESS')
        self.mark_scheduled('202610010900-aaaaaa')
        result = cleanup.auto_cleanup({'build': {}})
        self.assertEqual(result['tasks'], 1)
        self.assertFalse((scheduled / 'plugin').exists())
        self.assertTrue((manual / 'plugin').is_dir())       # manual builds only via ./app-build cleanup

    def test_can_be_switched_off_and_noop_without_schedules(self):
        home = self.add('202610010900-aaaaaa', 'SUCCESS')
        self.mark_scheduled('202610010900-aaaaaa')
        self.assertIsNone(cleanup.auto_cleanup({'build': {'auto_cleanup_scheduled_workdirs': False}}))
        self.assertTrue((home / 'plugin').is_dir())
        other = Path(self.tmp.name) / 'empty'
        other.mkdir()
        with patch.object(store, 'DATA', other):
            self.assertIsNone(cleanup.auto_cleanup({'build': {}}))


class CliTests(CleanupCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['cleanup', *args])
        return out.getvalue()

    def test_default_is_preview_and_apply_deletes(self):
        home = self.add('202610010900-aaaaaa', 'SUCCESS')
        preview = self.run_cli()
        self.assertIn('预览', preview)
        self.assertIn('--apply', preview)
        self.assertTrue((home / 'plugin').is_dir())
        done = self.run_cli('--apply')
        self.assertIn('已清理 1 个任务', done)
        self.assertFalse((home / 'plugin').exists())
        self.assertIn('没有可清理', self.run_cli())

    def test_human_sizes(self):
        self.assertEqual(cleanup.human(512), '512 B')
        self.assertEqual(cleanup.human(1536), '1.5 KB')
        self.assertEqual(cleanup.human(5 * 1024 ** 3), '5.0 GB')


if __name__ == '__main__':
    unittest.main()

"""Starting a new ZIP version hands over from <name>-old: data copied, old services stopped."""
import signal
import sqlite3
import tempfile
import unittest
from pathlib import Path

from appbuilder import handover


def make_tasks(data, count):
    db = sqlite3.connect(Path(data) / 'tasks.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY)')
    db.executemany('INSERT INTO tasks VALUES (?)', [(f'T{i}',) for i in range(count)])
    db.commit()
    db.close()


class CarryDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.new, self.old = base / 'mac-app-build-main', base / 'mac-app-build-main-old'
        (self.old / '.appbuilder').mkdir(parents=True)
        make_tasks(self.old / '.appbuilder', 2)
        (self.old / '.appbuilder' / 'jira_settings.json').write_text('{"enabled": true}')
        self.new.mkdir()

    def test_copies_previous_data_once_and_leaves_the_old_folder(self):
        handover.carry_data(self.new, self.new / '.appbuilder')
        self.assertTrue((self.new / '.appbuilder' / 'tasks.sqlite').exists())
        self.assertEqual((self.new / '.appbuilder' / 'jira_settings.json').read_text(), '{"enabled": true}')
        self.assertTrue((self.old / '.appbuilder' / 'tasks.sqlite').exists())
        self.assertIsNone(handover.carry_data(self.new, self.new / '.appbuilder'))   # second start: no-op

    def test_this_folders_own_builds_are_never_overwritten(self):
        (self.new / '.appbuilder').mkdir()
        make_tasks(self.new / '.appbuilder', 1)
        self.assertIsNone(handover.carry_data(self.new, self.new / '.appbuilder'))
        self.assertEqual(handover._task_count(self.new / '.appbuilder'), 1)

    def test_stray_data_without_tasks_is_moved_aside_and_previous_data_copied(self):
        # Real case: the old scheduler, still running from the renamed folder, kept writing
        # to the original path and created an EMPTY tasks.sqlite here before `web` started.
        stray = self.new / '.appbuilder'
        (stray / 'services').mkdir(parents=True)
        make_tasks(stray, 0)
        handover.carry_data(self.new, stray)
        self.assertEqual(handover._task_count(stray), 2)
        aside = [p for p in self.new.iterdir() if p.name.startswith('.appbuilder-before-update-')]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / 'services').is_dir())          # moved, not deleted
        self.assertEqual(handover._task_count(self.old / '.appbuilder'), 2)   # old folder untouched

    def test_nothing_to_bring_over_leaves_everything_alone(self):
        (self.old / '.appbuilder' / 'tasks.sqlite').unlink()
        (self.new / '.appbuilder').mkdir()
        (self.new / '.appbuilder' / 'note').write_text('mine')
        self.assertIsNone(handover.carry_data(self.new, self.new / '.appbuilder'))
        self.assertEqual((self.new / '.appbuilder' / 'note').read_text(), 'mine')


class PreviousFolderTests(unittest.TestCase):
    def test_finds_old_2_when_old_already_existed(self):
        import os
        with tempfile.TemporaryDirectory() as base:
            new = Path(base) / 'app'
            new.mkdir()
            older, newer = Path(base) / 'app-old', Path(base) / 'app-old 2'
            for i, folder in enumerate((older, newer)):
                (folder / '.appbuilder').mkdir(parents=True)
                os.utime(folder / '.appbuilder', (1000 + i * 1000, 1000 + i * 1000))
            self.assertEqual(handover.previous_folder(new), newer)
            (Path(base) / 'app-other' / '.appbuilder').mkdir(parents=True)     # unrelated name
            self.assertEqual(handover.previous_folder(new), newer)
            self.assertIsNone(handover.previous_folder(Path(base) / 'nothing'))


class HandOverOrderTests(unittest.TestCase):
    def test_old_services_are_stopped_before_data_is_copied(self):
        from unittest.mock import patch
        order = []
        with patch.object(handover, 'stop_other_folders', lambda: order.append('stop')), \
                patch.object(handover, 'carry_data', lambda: order.append('copy')), \
                patch.object(handover, 'repoint_launch_agent', lambda: order.append('agent')):
            handover.hand_over()
        self.assertEqual(order, ['stop', 'copy', 'agent'])

    def test_working_directory_wins_over_the_command_line(self):
        from unittest.mock import patch
        import subprocess
        out = subprocess.CompletedProcess([], 0, stdout='p1\nn/tmp/app-old\n', stderr='')
        with patch('appbuilder.handover.subprocess.run', return_value=out):
            self.assertEqual(handover._folder(1, '/tmp/app/.venv/bin/python -m appbuilder web'),
                             Path('/tmp/app-old').resolve())


class StopOtherFoldersTests(unittest.TestCase):
    def test_only_services_from_other_folders_are_stopped(self):
        with tempfile.TemporaryDirectory() as base:
            base = Path(base).resolve()      # macOS temp dirs live behind a /var -> /private/var symlink
            new, old = Path(base) / 'app', Path(base) / 'app-old'
            for folder in (new, old):
                (folder / 'appbuilder').mkdir(parents=True)
            procs = [(101, f'{old}/.venv/bin/python -m appbuilder scheduler run'),
                     (102, f'{old}/.venv/bin/python -m appbuilder web --port 8765'),
                     (103, f'{new}/.venv/bin/python -m appbuilder web'),
                     (104, f'{old}/.venv/bin/python -m appbuilder worker T1')]
            procs = [p for p in procs if handover.SERVICE.search(p[1])]
            sent, alive = [], {101, 102}

            def kill(pid, sig):
                sent.append((pid, sig))
                if sig == 0 and pid not in alive:
                    raise ProcessLookupError
                if sig == signal.SIGTERM:
                    alive.discard(pid)
            # The old folder was renamed while its processes ran: their command line still shows
            # the ORIGINAL path (= the new folder now); only their working directory moved along.
            renamed = {101: old, 102: old, 103: new}
            procs = [(101, f'{new}/.venv/bin/python -m appbuilder scheduler run'),
                     (102, f'{new}/.venv/bin/python -m appbuilder web --port 8765'),
                     (103, f'{new}/.venv/bin/python -m appbuilder web')]
            stopped = handover.stop_other_folders(new, processes=procs, kill=kill, wait=1,
                                                  folder_of=lambda pid, cmd: renamed[pid])
        self.assertEqual(sorted(p for p, _, _ in stopped), [101, 102])
        self.assertNotIn(103, [p for p, _ in sent])
        self.assertNotIn(104, [p for p, _ in sent])


if __name__ == '__main__':
    unittest.main()

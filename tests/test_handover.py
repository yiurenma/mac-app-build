"""Starting a new ZIP version hands over from <name>-old: data copied, old services stopped."""
import signal
import sqlite3
import tempfile
import unittest
from pathlib import Path

from appbuilder import handover


class CarryDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.new, self.old = base / 'mac-app-build-main', base / 'mac-app-build-main-old'
        (self.old / '.appbuilder').mkdir(parents=True)
        sqlite3.connect(self.old / '.appbuilder' / 'tasks.sqlite').close()
        (self.old / '.appbuilder' / 'jira_settings.json').write_text('{"enabled": true}')
        self.new.mkdir()

    def test_copies_previous_data_once_and_leaves_the_old_folder(self):
        handover.carry_data(self.new, self.new / '.appbuilder')
        self.assertTrue((self.new / '.appbuilder' / 'tasks.sqlite').exists())
        self.assertEqual((self.new / '.appbuilder' / 'jira_settings.json').read_text(), '{"enabled": true}')
        self.assertTrue((self.old / '.appbuilder' / 'tasks.sqlite').exists())
        self.assertIsNone(handover.carry_data(self.new, self.new / '.appbuilder'))   # second start: no-op

    def test_existing_data_is_never_overwritten(self):
        (self.new / '.appbuilder').mkdir()
        (self.new / '.appbuilder' / 'note').write_text('mine')
        self.assertIsNone(handover.carry_data(self.new, self.new / '.appbuilder'))
        self.assertFalse((self.new / '.appbuilder' / 'tasks.sqlite').exists())


class StopOtherFoldersTests(unittest.TestCase):
    def test_only_services_from_other_folders_are_stopped(self):
        with tempfile.TemporaryDirectory() as base:
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
            stopped = handover.stop_other_folders(new, processes=procs, kill=kill, wait=1)
        self.assertEqual(sorted(p for p, _, _ in stopped), [101, 102])
        self.assertNotIn(103, [p for p, _ in sent])
        self.assertNotIn(104, [p for p, _ in sent])


if __name__ == '__main__':
    unittest.main()

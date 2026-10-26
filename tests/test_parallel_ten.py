"""Ten-process stress checks using a shared local task database and real scripts."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder import store
from appbuilder.config import load_config

PROJECT = Path(__file__).resolve().parents[1]


def finish(children):
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate()


class TenProcessTests(unittest.TestCase):
    def test_ten_concurrent_task_reservations_are_unique_and_limit_is_enforced(self):
        code = '''
import json, sys, time
from pathlib import Path
from appbuilder import store
from appbuilder.config import load_config
root = Path(sys.argv[1]); store.DATA = root
while not (root / 'start').exists(): time.sleep(.01)
cfg = load_config(); cfg['build']['max_parallel'] = 10
params = dict(plugin_version_input='1.3', plugin_source='develop', gmx_source='feature/UKPBB-203745-test',
              project='Demo', country='UK', plugin_url=cfg['github']['plugin_url'],
              gmx_url=cfg['github']['gmx_url'])
print(json.dumps(store.create_task(cfg, params)), flush=True)
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            children = [subprocess.Popen([sys.executable, '-c', code, td], cwd=PROJECT,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(10)]
            try:
                (root / 'start').touch()
                tasks = []
                for child in children:
                    out, err = child.communicate(timeout=40)
                    self.assertEqual(child.returncode, 0, err)
                    tasks.append(json.loads(out))
                for key in ('id', 'stamp', 'plugin_branch', 'gmx_branch', 'plugin_job', 'gmx_job'):
                    self.assertEqual(len({task[key] for task in tasks}), 10, key)
                cfg = load_config(); cfg['build']['max_parallel'] = 10
                with patch.object(store, 'DATA', root):
                    self.assertEqual(store.active_count(), 10)
                    for task in tasks:
                        self.assertEqual(store.read_state(task['id']), {'completed': [], 'runtime': {'dispatch_policy': 'receipt-v1'}})
                    with self.assertRaisesRegex(RuntimeError, '上限'):
                        store.create_task(cfg, tasks[0])
                    store.update_task(tasks[0]['id'], status='SUCCESS')
                    replacement = store.create_task(cfg, tasks[0])
                    self.assertNotIn(replacement['id'], {task['id'] for task in tasks})
                    self.assertEqual(store.active_count(), 10)
            finally:
                finish(children)

    def test_ten_workers_run_upgrade_and_bootstrap_without_crossing_logs_or_locks(self):
        code = '''
import sys
from pathlib import Path
from unittest.mock import patch
from appbuilder.runner import worker_lease, run_bootstrap
root = Path(sys.argv[1]); ident = sys.argv[2]
with patch('appbuilder.runner.task_folder', return_value=root / ident):
    with worker_lease(ident):
        run_bootstrap(root / ident, root / ident / 'bootstrap.log', timeout_minutes=1)
'''
        contender = '''
import sys
from pathlib import Path
from unittest.mock import patch
from appbuilder.runner import worker_lease
from appbuilder.jenkins import NeedsAttention
with patch('appbuilder.runner.task_folder', return_value=Path(sys.argv[1])):
    try:
        with worker_lease('task-0'): pass
    except NeedsAttention:
        sys.exit(0)
    sys.exit(9)
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); bindir = root / 'bin'; bindir.mkdir()
            upgrade = bindir / 'ci-script'
            upgrade.write_text('#!/bin/sh\nset -eu\ntest "$1" = --upgrade\npwd > upgrade-cwd.txt\n'
                               'echo upgrade >> order.txt\necho "upgrade $(basename "$PWD")"\n'
                               'touch ready\nwhile [ ! -f ../release ]; do sleep .02; done\n')
            upgrade.chmod(0o755)
            env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH']}
            children = []
            try:
                for i in range(10):
                    ident = f'task-{i}'; folder = root / ident; folder.mkdir()
                    script = folder / 'bootstrap.sh'
                    script.write_text(f'#!/bin/sh\nset -eu\necho bootstrap >> order.txt\necho "bootstrap {ident}"\npwd > bootstrap-cwd.txt\n')
                    script.chmod(0o755)
                    children.append(subprocess.Popen([sys.executable, '-c', code, td, ident],
                                    cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f'task-{i}' / 'ready').exists() for i in range(10)):
                    self.assertLess(time.monotonic(), deadline, 'ten upgrade commands did not overlap')
                    time.sleep(.02)
                duplicate = subprocess.run([sys.executable, '-c', contender, str(root / 'task-0')],
                                           cwd=PROJECT, capture_output=True, text=True, timeout=10)
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                (root / 'release').touch()
                for child in children:
                    out, err = child.communicate(timeout=20)
                    self.assertEqual(child.returncode, 0, err)
                for i in range(10):
                    folder = root / f'task-{i}'
                    self.assertEqual((folder / 'order.txt').read_text().splitlines(), ['upgrade', 'bootstrap'])
                    for name in ('upgrade-cwd.txt', 'bootstrap-cwd.txt'):
                        self.assertEqual((folder / name).read_text().strip(), str(folder))
                    log = (folder / 'bootstrap.log').read_text()
                    self.assertIn(f'upgrade task-{i}\n', log)
                    self.assertIn(f'bootstrap task-{i}\n', log)
                    for other in range(10):
                        if other != i:
                            self.assertNotIn(f'task-{other}\n', log)
                released = subprocess.run([sys.executable, '-c', contender, str(root / 'task-0')],
                                          cwd=PROJECT, capture_output=True, text=True, timeout=10)
                self.assertEqual(released.returncode, 9, released.stderr)
            finally:
                (root / 'release').touch()
                finish(children)

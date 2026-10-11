"""Regression cases from review rounds 1 and 3: enterprise auth + safe recovery."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appbuilder.gitops import credentials
from appbuilder.runner import worker_lease, run_bootstrap
from appbuilder.jenkins import NeedsAttention
from appbuilder import store
from appbuilder.config import load_config, token_or_error


class TestNexusAndTokenSafety(unittest.TestCase):
    def test_configurable_token_env_reaches_askpass(self):
        with patch.dict(os.environ, {'HSBC_GITHUB_PAT': 'test-only-never-print', 'GITHUB_TOKEN': ''}):
            with credentials('HSBC_GITHUB_PAT') as env:
                self.assertEqual(env['APP_BUILDER_GIT_TOKEN'], 'test-only-never-print')
                username = subprocess.run([env['GIT_ASKPASS'], 'Username for host'], env=env,
                                          capture_output=True, text=True, check=True)
                password = subprocess.run([env['GIT_ASKPASS'], 'Password for host'], env=env,
                                          capture_output=True, text=True, check=True)
                self.assertEqual(username.stdout.strip(), 'x-access-token')
                self.assertEqual(password.stdout.strip(), 'test-only-never-print')

    def test_missing_configured_env_is_blocked(self):
        with patch.dict(os.environ, {'HSBC_GITHUB_PAT': ''}):
            with self.assertRaisesRegex(RuntimeError, 'HSBC_GITHUB_PAT'):
                with credentials('HSBC_GITHUB_PAT'):
                    pass

    def test_missing_token_guides_to_enterprise_github(self):
        cfg = load_config()
        with patch.dict(os.environ, {'GITHUB_TOKEN': ''}):
            with self.assertRaises(RuntimeError) as err:
                token_or_error(cfg)
            self.assertIn('alm-github.systems.uk.hsbc/settings/tokens', str(err.exception))
            self.assertNotIn('github.com/settings/personal-access-tokens/new', str(err.exception))

    def test_installer_no_default_public_browser_or_pip_upgrade(self):
        install = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
        self.assertNotIn('pip install --upgrade pip', install)
        self.assertIn('python3 -m appbuilder.pip_sources', install)
        self.assertIn('PLAYWRIGHT_DOWNLOAD_HOST', install)
        self.assertIn('browser_channel', install)


class TestRecoveryAndIsolation(unittest.TestCase):
    def test_per_task_lease_blocks_second_worker(self):
        with tempfile.TemporaryDirectory() as tmp, patch('appbuilder.runner.task_folder', return_value=Path(tmp)):
            with worker_lease('task-1'):
                with self.assertRaises(NeedsAttention):
                    with worker_lease('task-1'):
                        pass
            with worker_lease('task-1'):
                pass

    def test_failed_task_state_creation_not_inserted(self):
        cfg = load_config()
        params = {'plugin_version_input': '1.3', 'plugin_source': 'develop',
                  'gmx_source': 'feature/UKPBB-203745-test', 'project': 'Demo', 'country': 'UK',
                  'plugin_url': cfg['github']['plugin_url'], 'gmx_url': cfg['github']['gmx_url']}
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)):
            with patch.object(store, 'save_state', side_effect=OSError('disk unavailable')):
                with self.assertRaisesRegex(OSError, 'disk unavailable'):
                    store.create_task(cfg, params)
            self.assertEqual(store.task_list(), [])
            self.assertEqual(list((Path(tmp) / 'runs').iterdir()), [])

    def test_bootstrap_is_executed_in_its_own_gmx_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'gmx'
            root.mkdir()
            script = root / 'bootstrap.sh'
            script.write_text('#!/bin/sh\npwd > where-executed.txt\n')
            script.chmod(0o755)
            upgrade = root / 'ci-script'
            upgrade.write_text('#!/bin/sh\nexit 0\n')
            upgrade.chmod(0o755)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH']}):
                run_bootstrap(root, Path(tmp) / 'bootstrap.log', timeout_minutes=1)
            self.assertEqual((root / 'where-executed.txt').read_text().strip(), str(root))

    def test_bootstrap_nonzero_exit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'gmx'
            root.mkdir()
            script = root / 'bootstrap.sh'
            script.write_text('#!/bin/sh\nexit 17\n')
            script.chmod(0o755)
            upgrade = root / 'ci-script'
            upgrade.write_text('#!/bin/sh\nexit 0\n')
            upgrade.chmod(0o755)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH']}), self.assertRaisesRegex(RuntimeError, r'exit=17'):
                run_bootstrap(root, Path(tmp) / 'bootstrap.log', timeout_minutes=1)

class TestTrueSeparateProcesses(unittest.TestCase):
    def test_two_distinct_workers_hold_leases_concurrently(self):
        """Real subprocesses, not merely two task objects in one process."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for taskid in ('A', 'B'):
                (root / taskid).mkdir()
            code = '''
import os, sys, time
from pathlib import Path
from unittest.mock import patch
from appbuilder.runner import worker_lease
home = Path(sys.argv[1]); ident = sys.argv[2]
with patch('appbuilder.runner.task_folder', return_value=home / ident):
    with worker_lease(ident):
        (home / (ident + '.ready')).touch()
        for _ in range(800):
            if (home / 'release').exists():
                break
            time.sleep(.01)
        else:
            raise RuntimeError('workers failed to rendezvous')
'''
            project = str(Path(__file__).resolve().parents[1])
            workers = [subprocess.Popen([os.sys.executable, '-c', code, str(root), ident],
                                        cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                       for ident in ('A', 'B')]
            try:
                for _ in range(800):
                    if (root / 'A.ready').exists() and (root / 'B.ready').exists():
                        break
                    __import__('time').sleep(.01)
                self.assertTrue((root / 'A.ready').exists())
                self.assertTrue((root / 'B.ready').exists())
                (root / 'release').touch()
                for p in workers:
                    out, err = p.communicate(timeout=5)
                    self.assertEqual(p.returncode, 0, err.decode())
            finally:
                (root / 'release').touch()
                for p in workers:
                    if p.poll() is None:
                        p.kill()
                        p.communicate()

"""Exercise upgrade ordering and failure handling using real local subprocesses."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appbuilder.runner import run_bootstrap
from appbuilder.live_tests import bootstrap_test
from appbuilder.config import load_config
from appbuilder.versions import ARTIFACTS
import json


class BootstrapUpgradeTests(unittest.TestCase):
    def run_local(self, upgrade_body):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upgrade = root / 'ci-script'
            upgrade.write_text('#!/bin/sh\n' + upgrade_body)
            upgrade.chmod(0o755)
            script = root / 'bootstrap.sh'
            script.write_text('#!/bin/sh\necho bootstrap >> order.txt\n')
            script.chmod(0o755)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH']}):
                error = None
                try:
                    run_bootstrap(root, root / 'bootstrap.log', timeout_minutes=1)
                except RuntimeError as exc:
                    error = exc
            return (root / 'order.txt').read_text().splitlines(), error, (root / 'bootstrap.log').read_text()

    def test_upgrade_runs_first_in_same_directory_with_upgrade_argument(self):
        order, error, log = self.run_local('test "$1" = upgrade || exit 99\necho upgrade >> order.txt\n')
        self.assertIsNone(error)
        self.assertEqual(order, ['upgrade', 'bootstrap'])
        self.assertLess(log.index('$ ci-script upgrade'), log.index('$ ./bootstrap.sh'))

    def test_upgrade_failure_blocks_bootstrap(self):
        order, error, log = self.run_local('echo upgrade >> order.txt\nexit 23\n')
        self.assertEqual(order, ['upgrade'])
        self.assertRegex(str(error), r'ci-script upgrade.*exit=23')
        self.assertNotIn('$ ./bootstrap.sh', log)

    def test_live_bootstrap_upgrade_failure_blocks_bootstrap_and_lock_check(self):
        commands = []
        def process(args, *, cwd, env, emit, timeout, label):
            commands.append(args)
            if label.startswith('Git clone'):
                root = Path(args[-1]); root.mkdir(parents=True)
                (root / 'bootstrap.sh').write_text('#!/bin/sh\nexit 0\n')
                (root / 'bootstrap.sh').chmod(0o755)
                fixture = {'dependencies': [{'group': 'paybybank-ios-plugin', 'library': n,
                                             'version': '1.2.2', 'sha256': 'a'*64} for n in ARTIFACTS]}
                for name in ('mobile-ci.json', 'mobile-ci.lock'):
                    (root / name).write_text(json.dumps(fixture))
            elif label == 'ci-script upgrade':
                raise RuntimeError('ci-script upgrade failed exit=23')
            return []
        with tempfile.TemporaryDirectory() as td, patch('appbuilder.live_tests.credentials') as credentials, patch('appbuilder.live_tests.subprocess.run', return_value=SimpleNamespace(stdout='deadbeef\n')):
            credentials.return_value.__enter__.return_value = {}
            with self.assertRaisesRegex(RuntimeError, 'upgrade failed'):
                bootstrap_test('upload/test', lambda line: None, artifact_version='1.3.1-alpha.1-SNAPSHOT.2',
                               config=load_config(), base_dir=td, run_process=process)
        self.assertEqual(commands[-1], ['ci-script', 'upgrade'])
        self.assertNotIn(['./bootstrap.sh'], commands)

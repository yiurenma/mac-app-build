"""Exercise upgrade ordering and failure handling using real local subprocesses."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appbuilder.runner import run_bootstrap
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
        order, error, log = self.run_local('test "$1" = --upgrade || exit 99\necho upgrade >> order.txt\n')
        self.assertIsNone(error)
        self.assertEqual(order, ['upgrade', 'bootstrap'])
        self.assertLess(log.index('$ ci-script --upgrade'), log.index('$ ./bootstrap.sh'))

    def test_upgrade_failure_blocks_bootstrap(self):
        order, error, log = self.run_local('echo upgrade >> order.txt\nexit 23\n')
        self.assertEqual(order, ['upgrade'])
        self.assertRegex(str(error), r'ci-script --upgrade.*exit=23')
        self.assertNotIn('$ ./bootstrap.sh', log)

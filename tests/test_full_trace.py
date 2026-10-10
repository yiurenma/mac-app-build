"""Regression tests for observable and credential-scrubbed automation actions."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from appbuilder.cli import args_parser, main
from appbuilder.telemetry import safe_command, safe_text, set_verbose
from appbuilder.runner import run_bootstrap
from appbuilder.jenkins import JenkinsBrowser


class FullTraceTests(unittest.TestCase):
    def test_live_flag_enables_all_three_modes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['run', '--plugin-branch', 'develop', '--gmx-branch', 'develop',
                  '--plugin-version', '1.3', '--project-name', 'PayByBank',
                  '--dry-run', '--live'])
        self.assertIn('"visible": true', out.getvalue())
        self.assertIn('"verbose": true', out.getvalue())
        self.assertTrue(args_parser().parse_args(['resume', 't1', '--live']).live)

    def test_url_token_and_header_redaction(self):
        with patch.dict(os.environ, {'MYCOMPANY_TOKEN':'XSUPERSECRETXYZ'}, clear=False):
            output = safe_text('https://bob:password@jenkins.corp/job/one?token=abc#x Authorization: SECRETCOOKIE XSUPERSECRETXYZ')
            for word in ('bob', 'password', 'abc', 'SECRETCOOKIE', 'XSUPERSECRETXYZ'):
                self.assertNotIn(word, output)
            self.assertIn('jenkins.corp/job/one', output)
        self.assertNotIn('pw', safe_command(['git', 'https://user:pw@github.corp/repo']))

    def test_bootstrap_stream_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / 'bootstrap.sh'
            script.write_text('#!/bin/sh\necho "Resolving Nexus dependencies"\necho "token=abc123"\n', encoding='utf-8')
            script.chmod(0o755)
            upgrade = root / 'ci-script'
            upgrade.write_text('#!/bin/sh\necho upgrade-output\n')
            upgrade.chmod(0o755)
            trace = io.StringIO()
            set_verbose(True)
            try:
                with contextlib.redirect_stdout(trace), patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH']}):
                    run_bootstrap(root, root / 'bootstrap.log', timeout_minutes=1)
            finally:
                set_verbose(False)
            output = trace.getvalue()
            self.assertIn('[SHELL]', output)
            self.assertIn('[BOOTSTRAP]', output)
            self.assertIn('Resolving Nexus dependencies', output)
            self.assertNotIn('abc123', output)
            self.assertNotIn('abc123', (root / 'bootstrap.log').read_text())

    def test_playwright_visit_traces_navigation_without_query(self):
        class Page:
            def __init__(self):
                self.url = 'https://jenkins.corp/job/x'
            def goto(self, url, wait_until, timeout):
                return SimpleNamespace(status=200)
            def locator(self, name):
                return SimpleNamespace(count=lambda: 0)
            def title(self):
                return 'Jenkins'
        fake = SimpleNamespace(site='plugin', page=Page(), _login_if_needed=lambda _: False)
        out = io.StringIO()
        set_verbose(True)
        try:
            with contextlib.redirect_stdout(out):
                code = JenkinsBrowser.visit(fake, 'https://jenkins.corp/job/x?secret=yes')
        finally:
            set_verbose(False)
        self.assertEqual(code, 200)
        self.assertIn('page.goto', out.getvalue())
        self.assertNotIn('secret=yes', out.getvalue())

    def test_click_trace_mentions_locator_click(self):
        class Locator:
            def count(self): return 1
            def click(self): return None
        class Page:
            url = 'https://jenkins.corp'
            def get_by_role(self, role, name): return Locator()
        obj = SimpleNamespace(page=Page(), site='plugin')
        out = io.StringIO()
        set_verbose(True)
        try:
            with contextlib.redirect_stdout(out):
                JenkinsBrowser._safe_click(obj, 'Build', 'button')
        finally:
            set_verbose(False)
        self.assertIn('locator.click()', out.getvalue())
        self.assertIn('[CLICK]', out.getvalue())


if __name__ == '__main__':
    unittest.main()

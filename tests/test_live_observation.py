"""Offline regression tests: progress display, visible browser, safe command trace."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from appbuilder.cli import args_parser, create_params, watch_task
from appbuilder.telemetry import safe_command, set_verbose
from appbuilder import gitops, store
from appbuilder.jenkins import JenkinsBrowser


class LiveFlags(unittest.TestCase):
    def test_cli_accepts_live_flags(self):
        a = args_parser().parse_args([
            'run', '--plugin-version', '1.3', '--plugin-branch', 'develop',
            '--gmx-branch', 'develop', '--project-name', 'PayByBank',
            '--watch', '--visible', '--verbose'])
        self.assertTrue(a.watch)
        self.assertTrue(a.visible)
        self.assertTrue(a.verbose)
        conf = {'github': {'plugin_url': 'https://git.corp/plugin', 'gmx_url': 'https://git.corp/gmx'}}
        params = create_params(a, conf)
        self.assertTrue(params['visible'])
        self.assertTrue(params['verbose'])

    def test_resume_option_parse(self):
        a = args_parser().parse_args(['resume', 'some-task', '--watch', '--visible', '--verbose'])
        self.assertEqual(a.task_id, 'some-task')
        self.assertTrue(all((a.watch, a.visible, a.verbose)))

    def test_watch_does_not_spawn_or_call_jenkins(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'run.log'
            log.write_text('2026-10-10 [STEP] PLUGIN_JENKINS\n', encoding='utf-8')
            rows = [dict(stage='PLUGIN_JENKINS', status='RUNNING', note='', pid=111),
                    dict(stage='PLUGIN_JENKINS', status='SUCCESS', note='completed', pid=111)]
            out = io.StringIO()
            with (patch('appbuilder.cli.task_folder', return_value=Path(td)),
                  patch('appbuilder.cli.task', side_effect=rows),
                  patch('appbuilder.cli.alive', return_value=True),
                  patch('appbuilder.cli.time.sleep'),
                  contextlib.redirect_stdout(out)):
                watch_task('task-id', interval=1)
            self.assertIn('PLUGIN_JENKINS', out.getvalue())
            self.assertIn('任务结束: SUCCESS', out.getvalue())

    def test_watch_log_file_created_late(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'run.log'
            rows = [dict(stage='INIT', status='QUEUED', note='', pid=None),
                    dict(stage='FINISHED', status='SUCCESS', note='', pid=None)]
            out = io.StringIO()
            def create_log(t):
                path.write_text('New log line\n', encoding='utf-8')
            with (patch('appbuilder.cli.task_folder', return_value=Path(td)),
                  patch('appbuilder.cli.task', side_effect=rows),
                  patch('appbuilder.cli.time.sleep', side_effect=create_log),
                  contextlib.redirect_stdout(out)):
                watch_task('task-id')
            self.assertIn('New log line', out.getvalue())

    def test_sanitize_token_url_credentials_and_query(self):
        args = ['git', 'clone', 'https://alice:pw@git.corp/repo?token=secret',
                'TOKEN_SUPER_SECRET', '--', 'https://git.corp/ok#fragment']
        redacted = safe_command(args, {'GITHUB_TOKEN': 'TOKEN_SUPER_SECRET'})
        for secret in ('pw', 'alice', 'secret', 'TOKEN_SUPER_SECRET', 'fragment'):
            self.assertNotIn(secret, redacted)
        self.assertIn('[REDACTED]', redacted)

    def test_git_debug_prints_command_without_token(self):
        output = io.StringIO()
        secret = 'SPECIAL_CUSTOM_TOKEN_XYZ'
        with (patch('appbuilder.gitops.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='done')),
              contextlib.redirect_stdout(output)):
            tok = set_verbose(True)
            try:
                gitops.git(['push', 'origin', 'refs/heads/branch'], env={'APP_BUILDER_GIT_TOKEN':secret})
            finally:
                set_verbose(False)
        result = output.getvalue()
        self.assertIn('git', result)
        self.assertIn('push', result)
        self.assertNotIn(secret, result)

    def test_visible_launch_uses_chrome_not_headless(self):
        manager = MagicMock()
        playwright = MagicMock()
        playwright.chromium.launch.return_value = MagicMock()
        manager.start.return_value = playwright
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            auth = root / 'auth'
            auth.mkdir()
            (auth / 'plugin.json').write_text('{}', encoding='utf-8')
            with patch('appbuilder.jenkins.sync_playwright', return_value=manager):
                browser = JenkinsBrowser({'jenkins': {'browser_channel': 'chrome', 'poll_seconds': 45}},
                                         root, root, 'plugin', visible=True, verbose=True)
                try:
                    options = playwright.chromium.launch.call_args.kwargs
                    self.assertEqual(options, {'channel': 'chrome', 'headless': False, 'slow_mo': 300})
                finally:
                    browser.close()

    def test_stored_resume_flags_persist(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(store, 'DATA', Path(td)):
                with store.connect() as db:
                    db.execute('INSERT INTO tasks(id, created, stamp, status, params, stage) '
                               'VALUES (?, ?, ?, ?, ?, ?)',
                               ('t1', '2026-10-10', '202610100000', 'FAILED',
                                json.dumps({'visible': False, 'verbose': False}), 'PLUGIN_JENKINS'))
                store.update_task_params('t1', visible=True, verbose=True)
                flags = json.loads(store.task('t1')['params'])
                self.assertTrue(flags['visible'])
                self.assertTrue(flags['verbose'])
                store.update_task_params('t1', visible=None, verbose=None)
                self.assertEqual(json.loads(store.task('t1')['params']), flags)


if __name__ == '__main__':
    unittest.main()

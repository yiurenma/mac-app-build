import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import store, runner
from appbuilder.config import load_config
from appbuilder.versions import branch_names, gmx_issue_key
from appbuilder.cli import args_parser, create_params


class BranchRulesTests(unittest.TestCase):
    def test_issue_unambiguous_and_valid_when_supplied(self):
        for source in ('feature/UKPBB-12-ABC-34',):
            with self.subTest(source=source), self.assertRaises(ValueError):
                branch_names('1.3', '202610102341', source)
        self.assertEqual(gmx_issue_key('develop', 'UKPBB-203745'), 'UKPBB-203745')
        for key in ('UKPBB', 'ukpbb-123', 'ABC-1/other', ' ABC-1', 'ABC-1\n'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                gmx_issue_key('feature/UKPBB-203745', key)

    def test_cli_develop_allows_missing_issue(self):
        argv = ['run', '--plugin-branch', 'develop', '--gmx-branch', 'develop',
                '--plugin-version', '1.3', '--project-name', 'PayByBank']
        self.assertEqual(create_params(args_parser().parse_args(argv), load_config())['gmx_issue_key'], '')
        params = create_params(args_parser().parse_args(argv + ['--gmx-issue-key', 'UKPBB-203745']), load_config())
        self.assertEqual(params['gmx_issue_key'], 'UKPBB-203745')

    def test_no_issue_generates_short_compliant_target(self):
        with tempfile.TemporaryDirectory() as td, patch.object(store, 'DATA', Path(td)):
            params = store.create_task(load_config(), {'plugin_version_input':'1.3', 'gmx_source':'develop'})
            self.assertEqual(params['gmx_branch'], 'upload/AUTO-' + params['stamp'])
            self.assertEqual(len(params['gmx_branch']), 24)
            # Applicable upload alternative visible in the supplied repository-rule log.
            rule = r'^(infra-staging|feature|hotfix|bugfix|chore|upload)/([A-Z]{1,10}-[0-9]+){1}[a-zA-Z0-9_\-.]*$'
            self.assertRegex(params['gmx_branch'], rule)
            self.assertIn('upload%2FAUTO-', params['gmx_job'])
            self.assertEqual(len(store.task_list()), 1)

    def test_resume_preserves_commit_and_stages_and_recovers_interrupted_rename(self):
        for interrupted, source in ((False, 'feature/UKPBB-203745-test'), (True, 'feature/UKPBB-203745-test'), (False, 'develop'), (True, 'develop')):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as td, patch.object(store, 'DATA', Path(td)):
                config = load_config()
                params = store.create_task(config, {'plugin_version_input':'1.3', 'gmx_source':source,
                                                   'plugin_source':'develop', 'gmx_url':config['github']['gmx_url']})
                task_id = params['id']; new = params['gmx_branch']; old = 'upload/auto' + params['stamp']
                params = store.replace_gmx_target(task_id, new, old, 'https://old/job/')
                store.complete(task_id, 'PLUGIN_JENKINS'); store.complete(task_id, 'GMX_LOCK')
                store.patch_runtime(task_id, artifact_version='kept')
                root = store.task_folder(task_id) / 'gmx'; root.mkdir()
                def command(*args):
                    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
                command('init'); command('config','user.name','Test'); command('config','user.email','test@example.invalid')
                command('checkout','-b',new if interrupted else old)
                command('remote','add','origin',params['gmx_url'])
                (root/'mobile-ci.lock').write_text('preserved')
                command('add','.'); command('commit','-m','existing validated commit')
                sha=command('rev-parse','HEAD'); before=store.read_state(task_id)
                real_git = runner.git
                def git(args, **kwargs):
                    if args[0] == 'ls-remote': return ''
                    return real_git(args, **kwargs)
                with patch.object(runner, 'git', side_effect=git):
                    fixed=runner.repair_legacy_gmx_target(task_id, params, config, root, {})
                self.assertEqual(command('rev-parse','HEAD'),sha)
                self.assertEqual(command('branch','--show-current'),new)
                self.assertEqual(store.read_state(task_id),before)
                self.assertEqual(json.loads(store.task(task_id)['params'])['gmx_branch'],new)
                self.assertIn(new.replace('/', '%2F'),fixed['gmx_job'])

    def test_published_or_conflicting_targets_never_renamed(self):
        params={'gmx_branch':'upload/auto202610102341','plugin_version_input':'1.3',
                'gmx_source':'feature/UKPBB-203745-test','gmx_url':'https://example.invalid/repo'}
        for state, remote in (({'completed':['GMX_PUSH'],'runtime':{}}, ''),
                              ({'completed':[],'runtime':{}}, 'abc refs/heads/occupied')):
            with patch.object(runner,'read_state',return_value=state), patch.object(runner,'git',return_value=remote), patch.object(runner,'replace_gmx_target') as write:
                with self.assertRaises(runner.NeedsAttention):
                    runner.repair_legacy_gmx_target('task',params,load_config(),Path('/nonexistent'),{})
                write.assert_not_called()

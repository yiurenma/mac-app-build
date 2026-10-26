"""Runner/store integration over real temporary Git repositories, no CI requests."""
from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

from appbuilder import runner, store
from appbuilder.config import load_config
from appbuilder.gitops import git
from appbuilder.jenkins import NeedsAttention
import test_plugin_git_prepare as fixture
URL, TARGET = fixture.URL, fixture.TARGET


class PluginRunnerFlowTests(unittest.TestCase):
    setUp = fixture.PluginGitPrepareTests.setUp
    commit_source = fixture.PluginGitPrepareTests.commit_source
    human = fixture.PluginGitPrepareTests.human

    def setup_store(self):
        for module in (store, runner):
            item = patch.object(module, 'DATA', self.home / 'data')
            item.start(); self.addCleanup(item.stop)

    def create(self):
        return store.create_task(load_config(), dict(plugin_version_input='1.3.1',
            plugin_source='develop', plugin_url=URL, gmx_source='develop', country='UK',
            project='PayByBank', gmx_url='https://gmx.fixture.invalid/repo.git'))

    def prepare(self, params):
        with runner.plugin_target_lease(params):
            runner.prepare_plugin_task(params['id'], params, store.task_folder(params['id'])/'plugin', self.env)
        return store.read_state(params['id'])

    def test_three_tasks_follow_source_then_unchanged_without_empty_commit(self):
        self.setup_store()
        first = self.create(); one = self.prepare(first)
        (self.source/'code.txt').write_text('second source\n')
        source = self.commit_source('update source')
        second = self.create(); two = self.prepare(second)
        third = self.create(); three = self.prepare(third)
        self.assertEqual(first['plugin_flow_version'], 2)
        self.assertEqual(first['plugin_branch'], TARGET)
        self.assertEqual(two['runtime']['plugin_source_sha'], source)
        self.assertNotEqual(one['runtime']['plugin_sha'], two['runtime']['plugin_sha'])
        self.assertEqual(two['runtime']['plugin_sha'], three['runtime']['plugin_sha'])
        self.assertNotEqual(second['gmx_branch'], third['gmx_branch'])
        self.assertEqual(three['completed'], ['PLUGIN_PREPARE'])
        # Completed Resume skips preparation and does not reinterpret a new source tip.
        (self.source/'code.txt').write_text('future\n'); self.commit_source('future')
        self.assertEqual(self.prepare(third), three)

    def test_actual_runner_reaches_jenkins_with_persisted_source_and_build_sha(self):
        self.setup_store(); params = self.create()
        class StopBeforeCI(Exception):pass
        with patch.object(runner, 'credentials', return_value=nullcontext(self.env)), \
             patch.object(runner, 'token_or_error'), \
             patch.object(runner, 'JenkinsBrowser', side_effect=StopBeforeCI('no real CI')) as browser:
            with self.assertRaises(StopBeforeCI):runner._run_unlocked(params['id'])
        browser.assert_called_once()
        state = store.read_state(params['id'])
        self.assertEqual(state['completed'], ['PLUGIN_PREPARE'])
        self.assertEqual(state['runtime']['plugin_source_sha'], git(['rev-parse','HEAD'],cwd=self.source))
        self.assertEqual(state['runtime']['plugin_sha'], state['runtime']['plugin_preparation']['build_sha'])
        self.assertEqual(state['runtime']['plugin_preparation']['phase'], 'PUSHED')

    def test_existing_task_without_marker_keeps_legacy_stages(self):
        self.setup_store(); params = self.create(); params.pop('plugin_flow_version')
        calls=[]
        with patch.object(runner,'step',side_effect=lambda _,name,fn:calls.append(name)), \
             patch('appbuilder.plugin_git.prepare') as new_prepare:
            runner.prepare_plugin_task(params['id'],params,self.home/'legacy',self.env)
        self.assertEqual(calls,['PLUGIN_CHECKOUT','PLUGIN_EDIT','PLUGIN_PUSH'])
        new_prepare.assert_not_called()

    def test_same_target_lease_blocks_and_releases_after_error(self):
        self.setup_store(); params = self.create()
        with self.assertRaisesRegex(RuntimeError,'fixture'):
            with runner.plugin_target_lease(params):
                with self.assertRaises(NeedsAttention):
                    with runner.plugin_target_lease(params):pass
                raise RuntimeError('fixture')
        with runner.plugin_target_lease(params):pass

    def test_main_path_conflict_keeps_remote_and_records_error(self):
        self.setup_store(); self.prepare(self.create())
        human = self.human('code.txt','human\n')
        (self.source/'code.txt').write_text('source conflict\n'); self.commit_source('conflict')
        params = self.create()
        with self.assertRaises(RuntimeError):self.prepare(params)
        state=store.read_state(params['id'])
        self.assertNotIn('PLUGIN_PREPARE',state['completed'])
        self.assertIn('PLUGIN_PREPARE',state['runtime']['step_errors'])
        self.assertTrue(git(['ls-remote',URL,'refs/heads/'+TARGET],env=self.env).startswith(human))

    def test_completed_prepare_external_move_blocks_runner_before_ci(self):
        self.setup_store(); params=self.create(); self.prepare(params)
        self.human('human.txt','external after preparation\n')
        with patch.object(runner,'credentials',return_value=nullcontext(self.env)), \
             patch.object(runner,'token_or_error'), patch.object(runner,'JenkinsBrowser') as browser:
            with self.assertRaisesRegex(RuntimeError,'远端分支 SHA'):
                runner._run_unlocked(params['id'])
        browser.assert_not_called()

    def test_two_part_new_task_prepares_exact_generated_timestamp_version(self):
        self.setup_store()
        params=store.create_task(load_config(),dict(plugin_version_input='1.3',plugin_source='develop',
             plugin_url=URL,gmx_source='develop'))
        state=self.prepare(params)
        self.assertEqual(params['plugin_version'],'1.3.'+params['stamp'])
        self.assertEqual(params['plugin_branch'],'release/v1.3.'+params['stamp'])
        self.assertEqual(state['runtime']['plugin_preparation']['version'],params['plugin_version'])
        self.assertNotEqual(state['runtime']['plugin_source_sha'],state['runtime']['plugin_sha'])

    def test_equivalent_dot_git_url_uses_same_target_lock(self):
        self.setup_store(); params=self.create()
        with runner.plugin_target_lease(params):
            with self.assertRaises(NeedsAttention):
                with runner.plugin_target_lease({**params,'plugin_url':URL.removesuffix('.git')}):pass

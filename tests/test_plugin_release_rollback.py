"""Release rollback preserves saved tasks and GMX refs."""
import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from appbuilder import store
from appbuilder.config import load_config
from appbuilder.runner import effective_task_config, log_jenkins_targets
from appbuilder.versions import branch_names, job_urls, PLUGIN_RELEASE_PARENT, PLUGIN_FEATURE_PARENT
from appbuilder.diagnose import parse_target


class PluginReleaseRollbackTests(unittest.TestCase):
    def test_version_and_gmx_naming_remain_independent(self):
        for source, expected in [('develop', 'upload/AUTO-202610120930'),
                                 ('feature/UKPBB-123-work', 'upload/UKPBB-123-auto202610120930')]:
            names = branch_names('2.4', '202610120930', source)
            self.assertEqual(names, {'plugin_version': '2.4.202610120930',
                                     'plugin_branch': 'release/2.4.202610120930',
                                     'gmx_branch': expected})

    def test_exact_release_job_urls_use_branch_child_not_folder(self):
        config = load_config()
        parent = ('https://mobile-premier01-jenkins.build-mobile.iwpb.euw2.prod.aws.cloud.hsbc/'
                  'job/IWPB_Transaction_Banking/job/Domestic_Payment_and_Transfer_Mobile_Global_12824743/'
                  'job/Lib/job/mobile_paybybank-ios-plugin/job/release-build/job/')
        self.assertEqual(config['jenkins']['plugin_parent'], parent)
        for branch, job in [('release/2.4.202610120930', 'release_2_4_202610120930'),
                            ('release/1.3.202610120930', 'release_1_3_202610120930')]:
            urls = job_urls(config, branch, 'upload/UKPBB-123-auto202610120930')
            self.assertEqual(urls['plugin_job'], parent + job + '/')
            self.assertEqual(urls['gmx_job'], config['jenkins']['gmx_parent'] + 'upload%2FUKPBB-123-auto202610120930/')

    def test_new_task_uses_release_parent_and_preserves_explicit_override(self):
        config = load_config()
        for override in (None, PLUGIN_RELEASE_PARENT, PLUGIN_FEATURE_PARENT, 'https://ci.fixture.invalid/job/custom/job/'):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)):
                inputs = {'plugin_version_input': '1.3', 'gmx_source': 'feature/UKPBB-123-work'}
                if override:
                    inputs['jenkins_settings'] = {'plugin_parent': override}
                params = store.create_task(config, inputs)
                self.assertEqual(params['plugin_branch'], 'release/1.3.' + params['stamp'])
                self.assertEqual(params['plugin_job'], (PLUGIN_RELEASE_PARENT if override == PLUGIN_FEATURE_PARENT else override or config['jenkins']['plugin_parent']) + 'release_1_3_' + params['stamp'] + '/')
                self.assertEqual(params['gmx_branch'], 'upload/UKPBB-123-auto' + params['stamp'])
                if override == PLUGIN_FEATURE_PARENT:
                    self.assertEqual(effective_task_config(config, params)['jenkins']['plugin_parent'], PLUGIN_RELEASE_PARENT)

    def test_diagnosis_accepts_exact_current_and_historical_project_only(self):
        config = load_config()
        urls = [PLUGIN_RELEASE_PARENT + 'release_1_3_202610110930/',
                PLUGIN_FEATURE_PARENT + 'feature_TRANSACT-122556/',
                PLUGIN_FEATURE_PARENT + 'feature_1_3_202610120930/']
        for url in urls:
            self.assertEqual(parse_target(url, config).job_url, url)
            self.assertEqual(parse_target(url + '7/', config).number, 7)
        for url in [PLUGIN_FEATURE_PARENT, PLUGIN_FEATURE_PARENT.removesuffix('job/'),
                    urls[0].replace('mobile_paybybank-ios-plugin', 'another-plugin'),
                    urls[0].replace('https://mobile-premier01-jenkins.', 'https://other-jenkins.'),
                    urls[0].replace('/release-build/', '/other-build/')]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_target(url, config)
        config['jenkins']['plugin_parent'] = 'https://custom.fixture.invalid/job/custom/job/'
        with self.assertRaises(ValueError):
            parse_target(urls[0], config)

    def test_resume_keeps_saved_release_branch_url_and_runtime(self):
        config = load_config()
        legacy_parent = 'https://ci.fixture.invalid/job/plugin/job/release-build/job/'
        for branch_kind, saved_settings in [(kind, saved) for kind in ('release', 'feature') for saved in (False, True)]:
            with self.subTest(saved_settings=saved_settings), tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)):
                params = store.create_task(config, {'plugin_version_input': '1.3', 'gmx_source': 'develop'})
                params['plugin_branch'] = branch_kind + '/1.3.202610110930'
                legacy_parent = 'https://ci.fixture.invalid/job/plugin/job/' + branch_kind + '-build/job/'
                params['plugin_job'] = legacy_parent + branch_kind + '_1_3_202610110930/'
                if saved_settings:
                    params['jenkins_settings'] = {'plugin_parent': legacy_parent}
                with closing(store.connect()) as db, db:
                    db.execute('UPDATE tasks SET params=?, status=? WHERE id=?',
                               (json.dumps(params), 'FAILED', params['id']))
                state = {'completed': ['PLUGIN_PUSH'], 'runtime': {'plugin_build_number': 7, 'plugin_sha': 'saved-sha'}}
                store.save_state(params['id'], state)
                before = copy.deepcopy(params)
                config['jenkins']['plugin_parent'] = 'https://ci.fixture.invalid/job/plugin/job/feature-build/job/'
                store.reserve_resume(config, params['id'])
                saved = json.loads(store.task(params['id'])['params'])
                with patch('appbuilder.runner.log'):
                    log_jenkins_targets(saved, effective_task_config(config, saved))
                self.assertEqual(saved, before)
                self.assertEqual(store.read_state(params['id']), state)

"""Offline integration for the shared Jenkins APP-link entrypoints."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from appbuilder import app_links, cli
from appbuilder.jenkins import NeedsAttention

HOST = 'https://jenkins.link-fixture.invalid'
JOB = HOST + '/job/GMX/job/new/'
OLD = HOST + '/job/GMX/job/old/'
UPLOAD = HOST + '/job/upload-to-testfairy/job/master/51/'
JOIN = 'https://testfairy.com/join/CompleteLink?id=573108'
PARAMS = {'gmx_job': JOB, 'country': 'UK', 'project': 'PBB', 'visible': False}
RUNTIME = {'gmx_build_number': 812, 'gmx_sha': 'a' * 40}


class SharedAppLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.browser = Mock()
        self.browser.__enter__ = Mock(return_value=self.browser)
        self.browser.__exit__ = Mock(return_value=False)
        self.config = {'jenkins': {}, 'build': {}}
        self.result = {'share_url': JOIN, 'downstream_build_url': UPLOAD,
                       'downstream_job_url': UPLOAD.rsplit('51/',1)[0], 'downstream_build_number': 51}

    def lookup(self, runtime=None):
        with patch.object(app_links, 'JenkinsBrowser', return_value=self.browser) as factory, \
                patch.object(app_links, 'lookup_app_link', return_value=self.result) as extract:
            app = app_links.lookup_task_app(self.config, self.directory, self.directory, PARAMS,
                                            RUNTIME if runtime is None else runtime)
        return app, factory, extract

    def test_shared_service_uses_only_saved_build_and_keeps_full_app_url(self):
        app, factory, extract = self.lookup()
        extract.assert_called_once_with(self.browser, JOB, 812, emit=None)
        self.assertEqual(app['share_url'], JOIN)
        self.assertEqual(app['source_job_url'], JOB)
        self.assertEqual(app['source_build_number'], 812)
        self.assertEqual(app['source_build_url'], JOB + '812/')
        self.assertEqual(app['downstream_build_url'], UPLOAD)
        self.assertIsNone(app['app_version'])
        self.assertIsNone(app['app_build'])
        self.assertFalse(factory.call_args.kwargs['visible'])
        self.assertFalse(factory.call_args.kwargs['manual_login'])

    def test_reused_job_and_number_are_one_pair_despite_current_numbers(self):
        runtime = {**RUNTIME, 'gmx_formal_number': 813,
                   'app_reuse': {'job_url': OLD, 'build_number': 709, 'build_url': OLD + '709/',
                                 'build_sha': 'b' * 40, 'folder': 'old-folder'}}
        snapshot = copy.deepcopy(runtime)
        app, _, extract = self.lookup(runtime)
        extract.assert_called_once_with(self.browser, OLD, 709, emit=None)
        self.assertEqual(app['build_url'], OLD + '709/')
        self.assertEqual(app['sha'], 'b' * 40)
        self.assertEqual(app['folder'], 'old-folder')
        self.assertEqual(runtime, snapshot)

    def test_reused_source_with_missing_metadata_does_not_borrow_current_sha_or_folder(self):
        app,_,extract=self.lookup({**RUNTIME,'app_reuse':{'job_url':OLD,'build_number':709}})
        extract.assert_called_once_with(self.browser,OLD,709,emit=None)
        self.assertIsNone(app['sha']);self.assertIsNone(app['folder'])
        self.assertEqual(app['source_build_url'],OLD+'709/')

    def test_incomplete_reuse_never_falls_back_or_opens_browser(self):
        with patch.object(app_links, 'JenkinsBrowser') as browser:
            with self.assertRaises(NeedsAttention):
                app_links.lookup_task_app(self.config,self.directory,self.directory,PARAMS,
                                           {**RUNTIME,'app_reuse':{'job_url':OLD}})
        browser.assert_not_called()

    def test_missing_sha_or_versions_does_not_block_a_saved_jenkins_source(self):
        app, _, _ = self.lookup({'gmx_build_number': 812})
        self.assertEqual(app['share_url'], JOIN)
        self.assertIsNone(app['sha'])

    def test_history_preserves_verified_old_metadata_only_for_same_complete_identity(self):
        previous={'build_url':JOB+'812/','number':812,'sha':'a'*40,'folder':'0_A_UK_PBB',
                  'app_version':'3.76.0','app_build':'100','path':'original/path'}
        app,_,_=self.lookup({**RUNTIME,'testfairy_app':previous})
        self.assertEqual((app['app_version'],app['app_build'],app['path']),('3.76.0','100','original/path'))
        for key,value in [('number',813),('build_url',OLD+'812/'),('sha','b'*40),('folder','other')]:
            app,_,_=self.lookup({**RUNTIME,'testfairy_app':{**previous,key:value}})
            self.assertIsNone(app['app_version'])
            self.assertNotIn('path',app)

    def test_history_saves_only_app_result_and_never_restarts_or_changes_status(self):
        row={'id':'202610141000-abcdef','params':json.dumps(PARAMS),'status':'FAILED'}
        with patch('appbuilder.store.task',return_value=row), \
                patch('appbuilder.store.read_state',return_value={'runtime':RUNTIME}), \
                patch('appbuilder.store.task_folder',return_value=self.directory), \
                patch('appbuilder.store.patch_runtime') as save, \
                patch('appbuilder.store.update_task') as update, \
                patch.object(app_links,'lookup_task_app',return_value=self.result) as lookup, \
                patch.object(cli,'spawn') as spawn:
            app=app_links.lookup_history(row['id'],config=self.config,data=self.directory)
        save.assert_called_once_with(row['id'],testfairy_app=self.result)
        lookup.assert_called_once()
        update.assert_not_called();spawn.assert_not_called()
        self.assertEqual(app,self.result)

    def test_running_history_is_not_overwritten(self):
        with patch('appbuilder.store.task',return_value={'status':'RUNNING'}), \
                patch.object(app_links,'lookup_task_app') as lookup:
            with self.assertRaises(NeedsAttention):
                app_links.lookup_history('task',config=self.config,data=self.directory)
        lookup.assert_not_called()

    def test_cli_routes_to_same_history_service_without_build_preflight(self):
        with patch.object(app_links,'lookup_history',return_value=self.result) as lookup, \
                patch.object(cli,'load_config',return_value=self.config), \
                patch.object(cli,'doctor_check') as doctor, \
                patch.object(cli,'spawn') as spawn,patch('builtins.print') as output:
            cli.main(['app-link','202610141000-abcdef','--headless'])
        self.assertEqual(lookup.call_args.args,('202610141000-abcdef',))
        self.assertFalse(lookup.call_args.kwargs['visible'])
        self.assertIn(JOIN,output.call_args.args[0])
        doctor.assert_not_called();spawn.assert_not_called()

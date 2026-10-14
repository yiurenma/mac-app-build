from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from appbuilder import store, runner, reuse_flow as flow
from appbuilder.config import load_config
from appbuilder.jenkins import NeedsAttention
from appbuilder.versions import ARTIFACTS

PROOF={'history_complete':True}



class ReuseFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for module in (store,runner):
            p=patch.object(module,'DATA',self.root);p.start();self.addCleanup(p.stop)
        self.params=dict(plugin_version_input='1.3',plugin_source='develop',gmx_source='develop',
            plugin_url='https://git.fixture.invalid/plugin',gmx_url='https://git.fixture.invalid/gmx',
            country='UK',project='PayByBank',gmx_parameters={'BUILD_TYPE':'Develop','SYNC_UPLOAD':False},
            testfairy={'enabled':True,'login_url':'https://hsbc-mobilex.testfairy.com/login'})
        self.old=store.create_task(load_config(),self.params)
        self.hashes={name: str(i+1)*64 for i,name in enumerate(ARTIFACTS)}
        self.app=dict(build_url=self.old['gmx_job']+'4/',number=4,sha='d'*40,folder='0_A_UK_PayByBank',
            path='IOS_Upload/IOS_Develop/0_A_UK_PayByBank',share_url='https://hsbc-mobilex.testfairy.com/join/old',
            app_version='3.76.0',app_build='100',matched_at='2026-01-01T00:00:00Z')
        store.patch_runtime(self.old['id'],plugin_source_sha='a'*40,plugin_sha='b'*40,
            plugin_formal_number=1,plugin_artifact_number=2,artifact_version='1.3.1-SNAPSHOT.7',
            plugin_artifact_hashes=self.hashes,gmx_base_sha='c'*40,gmx_sha='d'*40,gmx_build_number=4,
            testfairy_app=self.app)
        flow.remember(self.old['id'],self.old,'plugin');flow.remember(self.old['id'],self.old,'app')
        store.update_task(self.old['id'],status='FAILED')
        self.new=store.create_task(load_config(),self.params)
        self.verifier=SimpleNamespace(plugin=Mock(return_value=PROOF),app=Mock(return_value=PROOF))

    def plugin(self,params=None):
        with patch('appbuilder.plugin_git.remote_tip',return_value='a'*40):
            return flow.try_plugin(self.new['id'],params or self.new,{},self.verifier)

    def seed_gmx(self):
        store.patch_runtime(self.new['id'],gmx_base_sha='c'*40,artifact_version='1.3.1-SNAPSHOT.7',
                            plugin_artifact_hashes=self.hashes)

    def test_complete_saved_results_reuse_after_logs_removed(self):
        from appbuilder.reuse_verification import LiveVerifier
        downloads = {k: dict(url='https://nexus.invalid/'+k+'.tar.gz',
                     version='1.3.1-SNAPSHOT.7', sha256=self.hashes[k]) for k in ARTIFACTS}
        store.patch_runtime(self.old['id'], plugin_artifact_result='SUCCESS',
                            plugin_artifact_downloads=downloads, gmx_formal_result='SUCCESS')
        flow.remember(self.old['id'], self.old, 'plugin')
        flow.remember(self.old['id'], self.old, 'app')
        self.verifier = LiveVerifier({}, None, None, {}, {})
        with patch('appbuilder.jenkins.read_full_console', side_effect=AssertionError('logs deleted')), \
             patch.object(runner, 'JenkinsBrowser', side_effect=AssertionError('no Jenkins')):
            self.assertTrue(self.plugin())
            self.seed_gmx()
            self.assertTrue(flow.try_app(self.new['id'], self.new, self.verifier))
        rt = store.read_state(self.new['id'])['runtime']
        self.assertEqual(rt['app_reuse']['source_task_id'], self.old['id'])
        self.assertEqual(rt['plugin_reuse']['downloads'], downloads)

    def jenkins_history(self):
        from urllib.parse import urlsplit
        origin=urlsplit(self.old['gmx_job'])
        downstream=origin.scheme+'://'+origin.netloc+'/job/upload-to-testfairy/job/master/'
        app={**self.app,'app_version':None,'app_build':None,'lookup_method':'jenkins-console-html',
             'source_job_url':self.old['gmx_job'],'source_build_number':4,
             'source_build_url':self.old['gmx_job']+'4/','downstream_job_url':downstream,
             'downstream_build_number':51,'downstream_build_url':downstream+'51/',
             'downstream_result':'SUCCESS'}
        app.pop('path')
        downloads={k:dict(url='https://nexus.invalid/'+k+'.tar.gz',version='1.3.1-SNAPSHOT.7',
                         sha256=self.hashes[k]) for k in ARTIFACTS}
        store.patch_runtime(self.old['id'],plugin_artifact_result='SUCCESS',plugin_artifact_downloads=downloads,
                            gmx_formal_result='SUCCESS',testfairy_app=app)
        flow.remember(self.old['id'],self.old,'plugin');flow.remember(self.old['id'],self.old,'app')
        return app

    def test_full_runner_reuses_complete_jenkins_link_history_without_rebuilding(self):
        from appbuilder.reuse_verification import LiveVerifier
        app=self.jenkins_history()
        with patch.object(runner,'credentials',return_value=nullcontext({})), \
             patch.object(runner,'token_or_error'),patch.object(runner,'checkout',return_value='c'*40), \
             patch('appbuilder.plugin_git.remote_tip',return_value='a'*40), \
             patch.object(runner,'prepare_plugin_task') as prepare, \
             patch.object(runner,'JenkinsBrowser') as browser, \
             patch.object(runner,'publish_gmx') as publish, \
             patch('appbuilder.app_links.lookup_task_app') as lookup:
            runner._run_unlocked(self.new['id'],reuse_verifier=LiveVerifier({},None,None,{},{}))
        prepare.assert_not_called();browser.assert_not_called();publish.assert_not_called();lookup.assert_not_called()
        result=json.loads((store.task_folder(self.new['id'])/'result.json').read_text())
        self.assertEqual(result['status'],'SUCCESS')
        self.assertEqual(result['gmx_job'],self.old['gmx_job'])
        self.assertEqual(result['runtime']['testfairy_app'],app)
        self.assertEqual(result['runtime']['app_reuse']['source_task_id'],self.old['id'])

    def test_jenkins_link_history_rejects_missing_or_conflicting_chain_and_fingerprints(self):
        from appbuilder.reuse import app_candidate
        from appbuilder.reuse_verification import LiveVerifier
        self.jenkins_history()
        record=store.read_state(self.old['id'])['runtime']['reuse_records']['app']
        verifier=LiveVerifier({},None,None,{},{})
        self.assertIsNotNone(app_candidate(record,record,verifier.app))
        for key in ('source_job_url','source_build_number','source_build_url','downstream_job_url',
                    'downstream_build_number','downstream_build_url','downstream_result','share_url'):
            changed=deepcopy(record);changed['app'].pop(key)
            with self.subTest(missing=key):
                self.assertIsNone(app_candidate(record,changed,verifier.app))
        for key,value in [('source_build_number',99),('source_job_url',self.new['gmx_job']),
                          ('downstream_result','FAILURE'),('downstream_build_url','https://evil.invalid/job/x/51/'),
                          ('share_url','https://evil.invalid/join/complete'),('sha','e'*40)]:
            changed=deepcopy(record);changed['app'][key]=value
            with self.subTest(conflict=key):
                self.assertIsNone(app_candidate(record,changed,verifier.app))
        changed=deepcopy(record)
        job=changed['app']['downstream_job_url'].replace('/job/upload-to-testfairy/',
                                                       '/job/other%2Fjob%2Fupload-to-testfairy/')
        changed['app'].update(downstream_job_url=job,downstream_build_url=job+'51/')
        self.assertIsNone(app_candidate(record,changed,verifier.app))
        self.assertFalse(verifier.app(changed)['history_complete'])
        for key,value in [('source_sha','e'*40),('plugin_hashes',{}),('parameters',{}),('result','FAILURE')]:
            changed=deepcopy(record);changed[key]=value
            with self.subTest(fingerprint=key):
                self.assertIsNone(app_candidate(record,changed,verifier.app))

    def test_incomplete_history_is_miss_without_fetching_deleted_logs(self):
        from appbuilder.reuse_verification import LiveVerifier
        self.verifier = LiveVerifier({}, None, None, {}, {})
        with patch('appbuilder.jenkins.read_full_console', side_effect=AssertionError('no history lookup')):
            self.assertFalse(self.plugin())
            self.seed_gmx()
            self.assertFalse(flow.try_app(self.new['id'], self.new, self.verifier))

    def test_failed_overall_task_still_supplies_plugin_candidate(self):
        self.assertTrue(self.plugin())
        rt=store.read_state(self.new['id'])['runtime']
        self.assertEqual(rt['plugin_reuse']['source_task_id'],self.old['id'])
        effective=flow.effective_params(self.new,rt)
        self.assertEqual(effective['plugin_job'],self.old['plugin_job'])
        self.assertEqual(effective['plugin_version'],self.old['plugin_version'])
        self.assertNotEqual(effective['plugin_version'],self.new['plugin_version'])
        self.assertEqual(json.loads(store.task(self.new['id'])['params'])['plugin_version'],self.new['plugin_version'])

    def test_fixed_version_mismatch_is_miss_without_live_call(self):
        self.assertFalse(self.plugin({**self.new,'plugin_version_input':'1.3.1'}))
        self.verifier.plugin.assert_not_called()

    def test_missing_proof_never_defaults_success(self):
        self.verifier.plugin.return_value={}
        self.assertFalse(self.plugin())
        self.assertNotIn('plugin_reuse',store.read_state(self.new['id'])['runtime'])
        self.seed_gmx();self.verifier.app.return_value={'delivered_app':True}
        self.assertFalse(flow.try_app(self.new['id'],self.new,self.verifier))

    def test_source_and_parameters_mismatch_do_not_invoke_verifier(self):
        with patch('appbuilder.plugin_git.remote_tip',return_value='e'*40):
            self.assertFalse(flow.try_plugin(self.new['id'],self.new,{},self.verifier))
        self.assertFalse(self.plugin({**self.new,'plugin_parameters':{'changed':True}}))
        self.verifier.plugin.assert_not_called()

    def test_app_four_quadrants(self):
        for source_changed in (False,True):
            for plugin_changed in (False,True):
                with self.subTest(source=source_changed,plugin=plugin_changed):
                    task=store.create_task(load_config(),self.params)
                    hashes=dict(self.hashes)
                    if plugin_changed:hashes[ARTIFACTS[0]]='f'*64
                    store.patch_runtime(task['id'],gmx_base_sha=('e' if source_changed else 'c')*40,
                        artifact_version='1.3.1-SNAPSHOT.7',plugin_artifact_hashes=hashes)
                    hit=flow.try_app(task['id'],task,self.verifier)
                    self.assertEqual(hit,not source_changed and not plugin_changed)

    def test_app_packing_parameter_change_is_miss(self):
        self.seed_gmx()
        changed={**self.new,'gmx_parameters':{**self.new['gmx_parameters'],'FORCE':True}}
        self.assertFalse(flow.try_app(self.new['id'],changed,self.verifier))
        self.verifier.app.assert_not_called()

    def test_resume_revalidates_and_does_not_replace_adopted_origin(self):
        self.assertTrue(self.plugin());self.verifier.plugin.return_value={}
        with self.assertRaises(NeedsAttention):self.plugin()
        self.assertEqual(store.read_state(self.new['id'])['runtime']['plugin_reuse']['source_task_id'],self.old['id'])

    def test_reuse_chain_preserves_first_task_and_deduplicates(self):
        self.assertTrue(self.plugin())
        flow.remember(self.new['id'],self.new,'plugin')
        records=list(flow.candidates('other','plugin'))
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['source_task_id'],self.old['id'])

    def test_full_runner_reuses_both_without_preparing_plugin_or_building_app(self):
        with patch.object(runner,'credentials',return_value=nullcontext({})), \
             patch.object(runner,'token_or_error'), patch.object(runner,'checkout',return_value='c'*40), \
             patch('appbuilder.plugin_git.remote_tip',return_value='a'*40), \
             patch.object(runner,'prepare_plugin_task') as prepare, \
             patch.object(runner,'JenkinsBrowser') as browser, \
             patch.object(runner,'publish_gmx') as publish:
            runner._run_unlocked(self.new['id'],reuse_verifier=self.verifier)
        prepare.assert_not_called();browser.assert_not_called();publish.assert_not_called()
        result=json.loads((store.task_folder(self.new['id'])/'result.json').read_text())
        self.assertEqual(result['status'],'SUCCESS')
        self.assertEqual(result['plugin_job'],self.old['plugin_job'])
        self.assertEqual(result['gmx_job'],self.old['gmx_job'])
        self.assertEqual(result['runtime']['testfairy_app'],self.app)
        from appbuilder.web import _jenkins_links, public_task
        from appbuilder.testfairy import expected_build, mail_preview
        from appbuilder.reporting import render_steps
        rt=result['runtime'];row=store.task(self.new['id'])
        links=_jenkins_links(row,self.new)
        self.assertTrue(any(link['url']==self.old['gmx_job']+'4/' for link in links))
        self.assertFalse(any(self.new['gmx_job'] in link['url'] for link in links))
        self.assertEqual(expected_build(self.new,rt)['build_url'],self.app['build_url'])
        mail=mail_preview(self.new,rt,row['created'])['body']
        self.assertIn(self.old['plugin_job'],mail);self.assertNotIn(self.new['plugin_job'],mail)
        report=render_steps(self.new,store.read_state(self.new['id']),row)
        self.assertIn(self.old['gmx_job'],report)
        self.assertNotIn(self.new['gmx_job'],report)
        self.assertEqual(public_task(row)['gmx_branch'],self.old['gmx_branch'])

    def test_missing_app_delivery_record_does_not_reuse_success_status(self):
        old=store.read_state(self.old['id']);old['runtime'].pop('testfairy_app')
        old['runtime']['reuse_records'].pop('app');store.save_state(self.old['id'],old)
        store.update_task(self.old['id'],status='SUCCESS')
        self.seed_gmx();self.assertFalse(flow.try_app(self.new['id'],self.new,self.verifier))

    def test_input_mismatch_and_missing_evidence_both_continue_normal_plugin_path(self):
        class StopBeforeCI(Exception):pass
        for missing in (False,True):
            task=store.create_task(load_config(),{**self.params,'plugin_version_input':'1.3' if missing else '9.9.9'})
            verifier=SimpleNamespace(plugin=Mock(return_value={} if missing else PROOF),app=Mock(return_value=PROOF))
            def prepared(task_id,*args):store.patch_runtime(task_id,plugin_sha='e'*40)
            with patch.object(runner,'credentials',return_value=nullcontext({})), \
                 patch.object(runner,'token_or_error'),patch('appbuilder.plugin_git.remote_tip',return_value='a'*40), \
                 patch.object(runner,'prepare_plugin_task',side_effect=prepared) as prepare, \
                 patch.object(runner,'verify_pushed'), \
                 patch.object(runner,'JenkinsBrowser',side_effect=StopBeforeCI):
                with self.assertRaises(StopBeforeCI):runner._run_unlocked(task['id'],reuse_verifier=verifier)
            prepare.assert_called_once()
            checks=store.read_state(task['id'])['runtime']['reuse_checks']['plugin']
            self.assertEqual(checks[0]['decision'],'EVIDENCE_INSUFFICIENT' if missing else 'INPUT_MISMATCH')
            self.assertEqual(verifier.plugin.called,missing)

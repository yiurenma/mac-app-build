from copy import deepcopy
import unittest
from unittest.mock import patch
from appbuilder.reuse_verification import LiveVerifier
from appbuilder.versions import ARTIFACTS


class HistoricalReuseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = LiveVerifier({}, None, None, {}, {})
        self.record = dict(source_task_id='old', result='SUCCESS', repository='https://git.invalid/p',
            source_sha='a'*40, branch='release/v1.3.1', build_sha='b'*40,
            job_url='https://ci.invalid/job/old/', build_number=2, formal_number=1,
            build_url='https://ci.invalid/job/old/2/', parameters={}, version='1.3.1-SNAPSHOT.7',
            hashes={k:'c'*64 for k in ARTIFACTS})
        self.record['downloads'] = {k:dict(url='https://nexus.invalid/'+k+'.tar.gz',
            version=self.record['version'], sha256='c'*64) for k in ARTIFACTS}

    def test_complete_history_survives_deleted_logs_without_network(self):
        with patch('appbuilder.jenkins.read_full_console', side_effect=AssertionError('deleted log')), \
             patch('appbuilder.jenkins.JenkinsBrowser', side_effect=AssertionError('no network')):
            self.assertTrue(self.verifier.plugin(self.record)['history_complete'])

    def test_missing_or_failed_history_cannot_be_reused(self):
        for key in ('source_task_id','result','source_sha','parameters','downloads','formal_number','build_url'):
            record=deepcopy(self.record);record.pop(key)
            self.assertFalse(self.verifier.plugin(record)['history_complete'],key)
        self.record['result']='FAILURE'
        self.assertFalse(self.verifier.plugin(self.record)['history_complete'])

    def test_url_version_and_hash_are_preserved_together(self):
        for key,value in [('url',''),('version','1.3.0'),('sha256','d'*64)]:
            record=deepcopy(self.record);record['downloads'][ARTIFACTS[0]][key]=value
            self.assertFalse(self.verifier.plugin(record)['history_complete'])

    def test_app_history_needs_exact_original_identity_without_permanent_link_requirement(self):
        record={**self.record,'folder':'folder','share_url':'https://tf.invalid/join/old',
                'app_version':'3.76.0','app_build':'100'}
        record['app']=dict(build_url=record['build_url'],number=2,sha='b'*40,folder='folder',path='path',
                          share_url=record['share_url'],app_version='3.76.0',app_build='100')
        with patch('appbuilder.testfairy.TestFairyBrowser',side_effect=AssertionError('no history lookup')):
            self.assertTrue(self.verifier.app(record)['history_complete'])
        record['app']['number']=3
        self.assertFalse(self.verifier.app(record)['history_complete'])

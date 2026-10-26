from copy import deepcopy
import unittest
from appbuilder.reuse import plugin_candidate, app_candidate
from appbuilder.versions import ARTIFACTS


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(repository='https://git.invalid/repo', source_sha='a'*40,
                           parameters={'BUILD_TYPE':'Develop'}, job_url='https://ci.invalid/job/old/',
                           build_url='https://ci.invalid/job/old/9/', build_number=9, build_sha='b'*40,
                           version='1.3.1-SNAPSHOT.7', effective_version='1.3.1',
                           hashes={name:'c'*64 for name in ARTIFACTS}, folder='UK',
                           share_url='https://testfairy.invalid/join/existing', app_version='2.3.4', app_build='17')
        self.record.update(plugin_version=self.record['version'], plugin_hashes=self.record['hashes'])
        self.proof = {'history_complete': True}


    def test_series_reuses_old_effective_version_and_original_job(self):
        request = {**self.record, 'version_input':'1.3'}
        result = plugin_candidate(request, self.record, lambda r:self.proof)
        self.assertEqual(result, self.record)
        self.assertIsNot(result, self.record)

    def test_fixed_version_must_match(self):
        for value, expected in [('1.3.1',True),('1.3.2',False),('1.4',False)]:
            result = plugin_candidate({**self.record, 'version_input':value}, self.record, lambda r:self.proof)
            self.assertEqual(result is not None, expected)

    def test_source_or_parameters_change_is_miss(self):
        for key, value in [('source_sha','d'*40),('parameters',{'BUILD_TYPE':'Release'})]:
            for fn in (plugin_candidate,app_candidate):
                self.assertIsNone(fn({**self.record, 'version_input':'1.3', key:value}, self.record, lambda r:self.proof))

    def test_app_four_quadrants(self):
        for source_changed in (False,True):
            for plugin_changed in (False,True):
                request = deepcopy(self.record)
                if source_changed:request['source_sha']='d'*40
                if plugin_changed:request['plugin_hashes'][ARTIFACTS[0]]='e'*64
                result = app_candidate(request,self.record,lambda r:self.proof)
                self.assertEqual(result is not None, not source_changed and not plugin_changed)

    def test_saved_success_and_links_without_complete_history_are_not_enough(self):
        for fn in (plugin_candidate,app_candidate):
            request={**self.record,'version_input':'1.3'}
            for proof in ({},True,{'status':'SUCCESS'},None):
                self.assertIsNone(fn(request,self.record,lambda r:proof))

    def test_each_required_evidence_failure_rejects(self):
        for fn, fields in ((plugin_candidate,('history_complete',)),
                           (app_candidate,('history_complete',))):
            for field in fields:
                proof={**self.proof,field:False}
                self.assertIsNone(fn({**self.record,'version_input':'1.3'},self.record,lambda r:proof))

    def test_never_pairs_new_job_with_old_number(self):
        record={**self.record,'job_url':'https://ci.invalid/job/new/'}
        self.assertIsNone(app_candidate(self.record,record,lambda r:self.proof))

    def test_verifier_error_is_cache_miss(self):
        def fail(record):raise TimeoutError('unavailable')
        self.assertIsNone(app_candidate(self.record,self.record,fail))

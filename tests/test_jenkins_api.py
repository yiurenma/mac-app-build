"""Jenkins over its HTTP API with a user token: same receipts and checks as the browser."""
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from appbuilder import dispatch_identity, runner
from appbuilder.jenkins import NeedsAttention, read_full_console
from appbuilder.jenkins_api import ApiJenkins, JenkinsHttp, TokenUnavailable, who_am_i

HOST = 'https://platform.fixture.invalid'
JOB = HOST + '/job/uploads/job/upload%2FX-1/'
CONF = {'jenkins': {'poll_seconds': 10, 'result_poll_seconds': 2, 'discover_poll_seconds': 1,
                    'queue_timeout_minutes': 1, 'build_timeout_minutes': 1}}
DEFS = [{'name': 'ENTITIES', 'type': 'ChoiceParameterDefinition', 'choices': ['UK', 'HK']},
        {'name': 'SYNC_UPLOAD', 'type': 'BooleanParameterDefinition'},
        {'name': 'CUSTOM_FOLDER', 'type': 'StringParameterDefinition'},
        {'name': 'BUILD_TYPE', 'type': 'ChoiceParameterDefinition', 'choices': ['Develop', 'Release']},
        {'name': 'SKIP_TEST', 'type': 'BooleanParameterDefinition'}]


class FakeHttp(JenkinsHttp):
    """Routes by URL path; records every request. No network."""
    def __init__(self, routes=None, post_status=201, location=HOST + '/queue/item/77/'):
        self.calls, self.routes = [], dict(routes or {})
        self.post_status, self.location = post_status, location
        self.queue = [{'id': 77, 'task': {'url': JOB}},
                      {'id': 77, 'task': {'url': JOB}, 'executable': {'number': 5, 'url': JOB + '5/'}}]
        self.build_states = [{'building': True, 'result': None}, {'building': False, 'result': 'SUCCESS'}]
        self.params = None

    def request(self, method, url, data=None, accept='application/json'):
        self.calls.append((method, url, data))
        path = url.split('?')[0]
        if method == 'POST':
            if self.post_status in (401, 403):
                return self.post_status, url, {}, b''
            self.params = dict(data or {})
            return self.post_status, url, {'Location': self.location}, b''
        if path in self.routes:
            status, body = self.routes[path]
            return status, url, {'Content-Type': 'application/json'}, json.dumps(body).encode()
        if path == JOB + 'api/json':
            return 200, url, {'Content-Type': 'application/json'}, json.dumps(
                {'buildable': True, 'property': [{'parameterDefinitions': DEFS}], 'builds': [{'number': 4}]}).encode()
        if path == HOST + '/queue/item/77/api/json':
            item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
            return 200, url, {'Content-Type': 'application/json'}, json.dumps(item).encode()
        if path == JOB + '5/api/json' and 'parameters' in url:
            values = [{'name': k, 'value': (v == 'true') if v in ('true', 'false') else v}
                      for k, v in (self.params or {}).items()]
            return 200, url, {'Content-Type': 'application/json'}, json.dumps({'actions': [{'parameters': values}]}).encode()
        if path == JOB + '5/api/json':
            state = self.build_states.pop(0) if len(self.build_states) > 1 else self.build_states[0]
            return 200, url, {'Content-Type': 'application/json'}, json.dumps(state).encode()
        if path == JOB + '5/consoleText':
            return 200, url, {'Content-Type': 'text/plain'}, b'upload ok\nFinished: SUCCESS\n'
        return 404, url, {}, b''


def api(http, site='gmx', settings=None):
    j = ApiJenkins(CONF, site, ('45012345', 'tok'), http=http)
    j.gmx_settings = settings if settings is not None else {'SYNC_UPLOAD': True, 'BUILD_TYPE': 'Develop', 'SKIP_TEST': True}
    return j


class SubmitTests(unittest.TestCase):
    def test_gmx_parameters_checked_posted_and_receipt_recorded(self):
        http, states = FakeHttp(), []
        j = api(http)
        j.build_strict_dispatch = True
        j.build_dispatch_checkpoint = lambda **f: states.append(f)
        j.request_build(JOB, with_parameters=True, country='UK', project='PayByBank')
        post = [c for c in http.calls if c[0] == 'POST']
        self.assertEqual(post[0][1], JOB + 'buildWithParameters')
        self.assertEqual(post[0][2], {'ENTITIES': 'UK', 'SYNC_UPLOAD': 'true', 'CUSTOM_FOLDER': '0_A_UK_PayByBank',
                                      'SKIP_TEST': 'true', 'BUILD_TYPE': 'Develop'})
        self.assertEqual([s['state'] for s in states], ['DISPATCHING', 'ACCEPTED'])
        self.assertEqual((states[1]['queue_id'], states[1]['http_status']), (77, 201))

    def test_invalid_choice_or_missing_parameter_stops_before_any_post(self):
        for settings, country in (({'SYNC_UPLOAD': True}, 'SG'), ({'SYNC_UPLOAD': True, 'BUILD_TYPE': 'Nightly'}, 'UK'),
                                  ({'SYNC_UPLOAD': True, 'FORCE': True}, 'UK')):
            with self.subTest(settings=settings):
                http, states = FakeHttp(), []
                j = api(http, settings=settings)
                j.build_strict_dispatch = True
                j.build_dispatch_checkpoint = lambda **f: states.append(f)
                with self.assertRaises(NeedsAttention):
                    j.request_build(JOB, with_parameters=True, country=country, project='P')
                self.assertFalse(any(c[0] == 'POST' for c in http.calls))
                self.assertEqual(states, [])

    def test_rejected_post_is_marked_not_submitted_so_the_browser_may_retry(self):
        http, states = FakeHttp(post_status=403), []
        j = api(http)
        j.build_strict_dispatch = True
        j.build_dispatch_checkpoint = lambda **f: states.append(f)
        with self.assertRaises(TokenUnavailable):
            j.request_build(JOB, with_parameters=True, country='UK', project='P')
        self.assertEqual([s['state'] for s in states], ['DISPATCHING', 'PREPARING'])

    def test_plain_build_for_bootstrap_uses_build_endpoint(self):
        http = FakeHttp()
        j = api(http, site='plugin')
        j.build_strict_dispatch = True
        j.request_build(JOB, with_parameters=False)
        self.assertEqual([c[1] for c in http.calls if c[0] == 'POST'], [JOB + 'build'])


class ReceiptFlowTests(unittest.TestCase):
    def test_receipt_v1_reconcile_resolves_queue_to_build_and_result_checks_parameters(self):
        state = {'runtime': {}}
        read = lambda task_id: state
        def patch_rt(task_id, **fields):
            state['runtime'].update(fields)
        http = FakeHttp()
        j = api(http)
        with patch('appbuilder.jenkins_api.time.sleep'):
            number = dispatch_identity.reconcile(j, JOB, 'T', 'gmx_formal', read, patch_rt,
                                                 with_parameters=True, country='UK', project='PayByBank')
            self.assertEqual(number, 5)
            self.assertEqual(state['runtime']['gmx_formal_identity']['kind'], 'QUEUE_EXECUTABLE')
            result, tail = j.wait_result(JOB, 5)
        self.assertEqual(result, 'SUCCESS')
        self.assertIn('Finished: SUCCESS', tail)
        self.assertIn((JOB, 5), j._verified)

    def test_parameters_changed_by_jenkins_are_reported(self):
        http = FakeHttp()
        j = api(http)
        j.build_strict_dispatch = True
        j.request_build(JOB, with_parameters=True, country='UK', project='PayByBank')
        http.params['ENTITIES'] = 'HK'
        with self.assertRaisesRegex(NeedsAttention, 'ENTITIES'):
            j.wait_result(JOB, 5)

    def test_callback_found_from_description_or_cause(self):
        builds = {'builds': [{'number': 3, 'displayName': '#3', 'description': 'Manual Callback ← Source #2'},
                             {'number': 2, 'displayName': '#2', 'description': 'Manual Trigger → build will be created soon'}]}
        http = FakeHttp(routes={JOB + 'api/json': (200, builds)})
        self.assertEqual(api(http, site='plugin').find_callback(JOB, 2), 3)
        builds = {'builds': [{'number': 9, 'actions': [{'causes': [{'shortDescription': 'Manual Callback ← Source #8'}]}]}]}
        http = FakeHttp(routes={JOB + 'api/json': (200, builds)})
        self.assertEqual(api(http, site='plugin').find_callback(JOB, 8), 9)

    def test_full_console_is_read_with_the_token(self):
        http = FakeHttp()
        console = read_full_console(api(http), JOB, 5, emit=lambda m: None)
        self.assertTrue(console.endswith('Finished: SUCCESS\n'))


class IdentityTests(unittest.TestCase):
    def test_who_am_i_rejects_anonymous_and_sso_redirect(self):
        ok = FakeHttp(routes={HOST + '/whoAmI/api/json': (200, {'name': '45012345', 'authenticated': True, 'anonymous': False})})
        self.assertEqual(who_am_i(ok, JOB), '45012345')
        anon = FakeHttp(routes={HOST + '/whoAmI/api/json': (200, {'name': 'anonymous', 'authenticated': True, 'anonymous': True})})
        with self.assertRaises(TokenUnavailable):
            who_am_i(anon, JOB)
        redirect = FakeHttp(routes={HOST + '/whoAmI/api/json': (302, {})})
        with self.assertRaisesRegex(TokenUnavailable, '重定向'):
            who_am_i(redirect, JOB)


class FallbackTests(unittest.TestCase):
    def run_step(self, params, creds, policy='receipt-v1', fail=None):
        used = []

        @contextmanager
        def fake_browser(*a, **k):
            used.append('browser')
            yield 'BROWSER'

        class FakeApi:
            def __init__(self, *a, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *e):
                return False
            def probe(self, job):
                if fail:
                    raise TokenUnavailable(fail)
        with patch.object(runner, 'JenkinsBrowser', fake_browser), \
                patch('appbuilder.jenkins_api.ApiJenkins', FakeApi), \
                patch('appbuilder.auth.jenkins_api_credentials', return_value=creds), \
                patch.object(runner, 'read_state', return_value={'runtime': {'dispatch_policy': policy}}), \
                patch.object(runner, 'log'):
            result = runner.with_jenkins('T', CONF, None, 'gmx', params, JOB,
                                         lambda j: used.append('api' if isinstance(j, FakeApi) else 'browser-body') or j)
        return used

    def test_switch_on_with_token_uses_api(self):
        self.assertEqual(self.run_step({'jenkins_api': True}, ('u', 't')), ['api'])

    def test_switch_off_old_task_missing_token_or_refused_token_use_browser(self):
        self.assertEqual(self.run_step({'jenkins_api': False}, ('u', 't')), ['browser', 'browser-body'])
        self.assertEqual(self.run_step({}, ('u', 't')), ['browser', 'browser-body'])
        self.assertEqual(self.run_step({'jenkins_api': True}, None), ['browser', 'browser-body'])
        self.assertEqual(self.run_step({'jenkins_api': True}, ('u', 't'), policy=None), ['browser', 'browser-body'])
        self.assertEqual(self.run_step({'jenkins_api': True}, ('u', 't'), fail='HTTP 401'), ['browser', 'browser-body'])


class TokenEndpointTests(unittest.TestCase):
    def test_status_save_and_read_only_test(self):
        from appbuilder import jenkins_token_api as T
        with patch('appbuilder.auth.store_jenkins_api_user') as su, patch('appbuilder.auth.store_jenkins_api_token') as st, \
                patch.object(T, 'status', return_value={'user': '45012345', 'tokens': {'plugin': True, 'gmx': True}}):
            out = T.save({'user': '45012345', 'plugin_token': 'p1', 'gmx_token': ''})
        su.assert_called_once_with('45012345')
        st.assert_called_once_with('plugin', 'p1')
        self.assertNotIn('p1', json.dumps(out))
        self.assertEqual(T.handle_post('/api/jenkins-token/save', {})[0], 400)
        who = FakeHttp(routes={'https://mobile-premier01-jenkins.build-mobile.iwpb.euw2.prod.aws.cloud.hsbc/whoAmI/api/json':
                               (200, {'name': '45012345', 'authenticated': True, 'anonymous': False})})
        with patch('appbuilder.auth.jenkins_api_credentials', side_effect=lambda site: ('u', 't') if site == 'plugin' else None):
            result = T.test(http_factory=lambda creds: who)
        self.assertTrue(result['plugin']['ok'])
        self.assertIn('45012345', result['plugin']['message'])
        self.assertFalse(result['gmx']['ok'])



BRANCH_JOB = HOST + '/job/uploads/job/upload%2FAUTO-1/'


class QueueIdentityTests(unittest.TestCase):
    def http(self, queue_item=None, builds=None):
        routes = {}
        if queue_item is not None:
            routes[HOST + '/queue/item/77/api/json'] = (200, queue_item)
        else:
            routes[HOST + '/queue/item/77/api/json'] = (404, {})
        routes[BRANCH_JOB + 'api/json'] = (200, {'builds': builds or []})
        return FakeHttp(routes=routes)

    def test_branch_job_written_with_double_encoding_is_the_same_job(self):
        item = {'id': 77, 'task': {'url': HOST + '/job/uploads/job/upload%252FAUTO-1/'},
                'executable': {'number': 2, 'url': HOST + '/job/uploads/job/upload%252FAUTO-1/2/'}}
        found = api(self.http(item)).resolve_queue_build(BRANCH_JOB, 77)
        self.assertEqual(found['build_url'], BRANCH_JOB + '2/')

    def test_other_job_is_still_refused_and_the_message_shows_both_addresses(self):
        item = {'id': 77, 'task': {'url': HOST + '/job/uploads/job/upload%2FOTHER/'}}
        with self.assertRaisesRegex(NeedsAttention, 'OTHER.*本次'):
            api(self.http(item)).resolve_queue_build(BRANCH_JOB, 77)

    def test_expired_queue_item_is_linked_by_the_builds_own_queue_id(self):
        state = {'runtime': {'gmx_formal_dispatch': {
            'job_url': BRANCH_JOB, 'attempt_id': 'a1', 'state': 'ACCEPTED', 'policy': 'receipt-v1',
            'http_status': 201, 'queue_id': 77, 'queue_url': HOST + '/queue/item/77/'}}}
        http = self.http(None, builds=[{'number': 3, 'queueId': 90}, {'number': 2, 'queueId': 77}])
        number = dispatch_identity.reconcile(api(http), BRANCH_JOB, 'T', 'gmx_formal', lambda t: state,
                                             lambda t, **f: state['runtime'].update(f))
        self.assertEqual(number, 2)
        self.assertEqual(state['runtime']['gmx_formal_identity']['build_url'], BRANCH_JOB + '2/')
        self.assertFalse(any(c[0] == 'POST' for c in http.calls))

    def test_no_build_with_our_queue_id_still_stops_without_guessing(self):
        state = {'runtime': {'gmx_formal_dispatch': {
            'job_url': BRANCH_JOB, 'attempt_id': 'a1', 'state': 'ACCEPTED', 'policy': 'receipt-v1',
            'http_status': 201, 'queue_id': 77, 'queue_url': HOST + '/queue/item/77/'}}}
        http = self.http(None, builds=[{'number': 3, 'queueId': 90}])
        with self.assertRaisesRegex(NeedsAttention, '队列记录已消失'):
            dispatch_identity.reconcile(api(http), BRANCH_JOB, 'T', 'gmx_formal', lambda t: state,
                                        lambda t, **f: state['runtime'].update(f))

    def test_same_job_and_build_number_helpers(self):
        self.assertTrue(dispatch_identity.same_job(BRANCH_JOB, HOST + '/job/uploads/job/upload%252FAUTO-1'))
        self.assertFalse(dispatch_identity.same_job(BRANCH_JOB, 'https://other.invalid/job/uploads/job/upload%2FAUTO-1/'))
        self.assertEqual(dispatch_identity.build_number(BRANCH_JOB, HOST + '/job/uploads/job/upload%252FAUTO-1/5/'), 5)
        self.assertIsNone(dispatch_identity.build_number(BRANCH_JOB, HOST + '/job/uploads/job/upload%2FOTHER/5/'))


if __name__ == '__main__':
    unittest.main()

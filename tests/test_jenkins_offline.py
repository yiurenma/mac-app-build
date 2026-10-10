"""Browser-independent Jenkins simulation: no corporate endpoints contacted."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from appbuilder.jenkins import (
    JenkinsBrowser, callback_number_from_text, ensure_bootstrap,
    formal_build, plugin_artifact_version, plugin_artifact_info, NeedsAttention, final_result_from_console,
    result_from_status_markup,
)
from appbuilder.versions import ARTIFACTS
from appbuilder import store


VERSION = '1.3.1-alpha.1-SNAPSHOT.2'


def nexus_console(version=VERSION, result='SUCCESS'):
    rows = [{'artifactUrl': f'https://nexus.invalid/plugin/{version}/{name}-{version}.tar.gz',
             'version': '1.3.1-alpha.1', 'artifactSHA256': str(i + 1) * 64}
            for i, name in enumerate(ARTIFACTS)]
    body = '::group::Nexus upload output\n' + json.dumps({'upload_results': rows}, indent=2)
    return '\n'.join('20:43:53 ' + line for line in body.splitlines()) + f'\nFinished: {result}\n'


NO_UPLOAD_CONSOLE = '20:40:00 Manual Trigger\nFinished: SUCCESS\n'


class NoArtifactPages:
    """A browser that fails the test if anything opens the Jenkins Artifact browser."""
    conf = {'callback_timeout_minutes': 1}
    poll = 0.01

    def __init__(self, logs, callback=None):
        self.logs, self.callback, self.read = logs, callback, []

    def visit(self, url, timeout=60000):
        raise AssertionError(f'Artifact/页面不应被访问: {url}')

    def wait_result(self, job_url, num, *, watch_callback=False):
        return 'SUCCESS', ''

    def find_callback(self, job_url, source_number):
        return self.callback


class TestPluginArtifactFromLog(unittest.TestCase):
    def run_case(self, browser, source=7):
        def fake_read(b, job_url, number, emit=None):
            b.read.append(number)
            return b.logs[number]
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            store.save_state('t', {'completed': [], 'runtime': {}})
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', fake_read):
                return plugin_artifact_info(browser, 'https://ci.corp/job/plugin/job/release_1_3/', source, 't')

    def test_version_and_sha_come_from_source_build_log_without_artifact_pages(self):
        b = NoArtifactPages({7: nexus_console()})
        version, hashes, number = self.run_case(b)
        self.assertEqual((version, number), (VERSION, 7))
        self.assertEqual(hashes, {name: str(i + 1) * 64 for i, name in enumerate(ARTIFACTS)})
        self.assertEqual(b.read, [7])

    def test_falls_back_to_callback_log_when_source_build_has_no_upload_output(self):
        b = NoArtifactPages({7: NO_UPLOAD_CONSOLE, 8: nexus_console()}, callback=8)
        version, hashes, number = self.run_case(b)
        self.assertEqual((version, number), (VERSION, 8))
        self.assertEqual(b.read, [7, 8])

    def test_failed_log_result_is_rejected(self):
        b = NoArtifactPages({7: nexus_console(result='FAILURE')})
        with self.assertRaises(NeedsAttention):
            self.run_case(b)

    def test_missing_upload_output_everywhere_needs_attention(self):
        b = NoArtifactPages({7: NO_UPLOAD_CONSOLE})
        b.conf = {'callback_timeout_minutes': 0}
        with self.assertRaisesRegex(NeedsAttention, 'Nexus 上传记录'):
            self.run_case(b)

    def test_callback_source_relation(self):
        text = '''Builds\n#3 04:48\nManual Callback ← Source #2\n#2 04:47\nManual Trigger → build will be created soon\n#1 04:20\nFailure\n'''
        self.assertEqual(callback_number_from_text(text, 2), 3)
        self.assertIsNone(callback_number_from_text(text, 1))
        text += '#4 04:49\nManual Callback ← Source #2\n'
        with self.assertRaises(NeedsAttention):
            callback_number_from_text(text, 2)


class TestConsoleStatus(unittest.TestCase):
    def test_accepts_only_last_status_line(self):
        self.assertEqual(final_result_from_console('hello\nFinished: SUCCESS\n'), 'SUCCESS')
        self.assertEqual(final_result_from_console('hello\nFinished: FAILURE\n'), 'FAILURE')
        self.assertIsNone(final_result_from_console('command printed Finished: SUCCESS\nmore work pending'))
        self.assertIsNone(final_result_from_console('Finished: SUCCESS\nRunning another step...'))


class SimulatedJenkins:
    """Init Build failed; Jenkinsfile then exposes the parameterized form."""
    def __init__(self):
        self.current_mode = 'build'
        self.numbers = set()
        self.requests = []
        self.polled = []
        self.conf = {'callback_timeout_minutes': 1}
        self.poll = 0.01

    def wait_job(self, job_url):
        return True

    def mode(self, job_url):
        return self.current_mode

    def wait_parameter_mode(self, job_url):
        assert self.current_mode == 'parameters'

    def visit(self, job_url):
        return 200

    def build_numbers(self, job_url):
        return self.numbers.copy()

    def request_build(self, job_url, *, with_parameters, country='', project=''):
        self.requests.append((with_parameters, country, project))
        self.numbers.add(len(self.requests))
        return set(self.numbers) - {len(self.requests)}

    def detect_new_build(self, job_url, baseline, minutes=12):
        matches = self.numbers - set(baseline)
        assert len(matches) == 1
        return next(iter(matches))

    def wait_result(self, job_url, num, *, watch_callback=False):
        self.polled.append(num)
        if num == 1 and self.requests[0][0] is False:
            self.current_mode = 'parameters'
            return 'FAILURE', 'Finished: FAILURE'
        return 'SUCCESS', 'Finished: SUCCESS'


class TestJenkinsStateMachine(unittest.TestCase):
    def test_init_failure_then_formal_success_no_duplicate_on_resume(self):
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            task_id = 'test-1'
            store.save_state(task_id, {'completed': [], 'runtime': {}})
            b = SimulatedJenkins()
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', linked_read):
                ensure_bootstrap(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(b.requests, [(False, '', '')])
                self.assertEqual(store.read_state(task_id)['runtime']['plugin_bootstrap_result'], 'FAILURE')
                formal_build(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(b.requests, [(False, '', ''), (True, '', '')])
                ensure_bootstrap(b, 'job_url/', task_id, 'plugin')
                formal_build(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(len(b.requests), 2)

    def test_gmx_parameters_passed_once(self):
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            store.save_state('task', {'completed': [], 'runtime': {}})
            b = SimulatedJenkins()
            b.current_mode = 'parameters'
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', linked_read):
                ensure_bootstrap(b, 'job_url/', 'task', 'gmx')
                formal_build(b, 'job_url/', 'task', 'gmx', country='UK', project='PayByBank')
                self.assertEqual(b.requests, [(True, 'UK', 'PayByBank')])

class TestParameterControls(unittest.TestCase):
    def test_gmx_changes_only_three_controls(self):
        class Control:
            def __init__(self, tag, typ='', options=None, value=''):
                self.tag = tag
                self.typ = typ
                self.options = options or []
                self.value = value
                self.checked = False

            def evaluate(self, script):
                return self.tag

            def get_attribute(self, attr):
                return self.typ if attr == 'type' else None

            def locator(self, css):
                return SimpleNamespace(evaluate_all=lambda expression: self.options)

            def select_option(self, value):
                self.value = value

            def check(self, **kwargs):
                self.checked = True

            def is_checked(self):
                return self.checked

            def fill(self, value):
                self.value = value

            def input_value(self):
                return self.value

        fake_controls = {
            'ENTITIES': Control('select', options=[{'value':'UK', 'text':'UK'}, {'value':'HK', 'text':'HK'}]),
            'SYNC_UPLOAD': Control('input', 'checkbox'),
            'CUSTOM_FOLDER': Control('input', 'text'),
            'BUILD_TYPE': Control('select', value='Develop'),
            'SIGNING_REPO_BRANCH': Control('input', 'text', value='master'),
        }
        fake = SimpleNamespace(conf={'gmx_fields': {}},
                               _find_control=lambda field, css='': fake_controls[field])
        JenkinsBrowser.fill_gmx_parameters(fake, 'UK', 'PayByBank')
        self.assertEqual(fake_controls['ENTITIES'].value, 'UK')
        self.assertTrue(fake_controls['SYNC_UPLOAD'].checked)
        self.assertEqual(fake_controls['CUSTOM_FOLDER'].value, '0_A_UK_PayByBank')
        self.assertEqual(fake_controls['BUILD_TYPE'].value, 'Develop')
        self.assertEqual(fake_controls['SIGNING_REPO_BRANCH'].value, 'master')

    def test_nonactionable_text_does_not_authorize_build(self):
        class NoLinkPage:
            def get_by_role(self, role, name):
                return SimpleNamespace(count=lambda: 0)
        fake = SimpleNamespace(page=NoLinkPage(), visit=lambda url: None)
        self.assertEqual(JenkinsBrowser.mode(fake, 'https://ci.corp/job/example/'), 'unknown')


def linked_read(browser, job_url, number, emit=None):
    """#1 is the Manual Trigger (no upload); #2 is the Callback that uploaded to Nexus."""
    return NO_UPLOAD_CONSOLE if number == 1 else nexus_console()


class LinkedReleaseJenkins(SimulatedJenkins):
    """The first formal job is a Manual Trigger; #2 is its real callback."""
    def __init__(self):
        super().__init__()
        self.current_mode = 'parameters'
        self.conf['callback_timeout_minutes'] = 1

    def wait_result(self, job_url, num, *, watch_callback=False):
        self.polled.append(num)
        if num == 1:
            assert watch_callback  # must not poll trigger until timeout
            self.numbers.add(2)
            return 'DELEGATED', '2'
        if num == 2:
            return 'SUCCESS', ''
        raise AssertionError(f'unexpected Build #{num}')

    def find_callback(self, job_url, source_number):
        return 2 if source_number == 1 else None


class TestLinkedPluginBuild(unittest.TestCase):
    def test_live_trigger_hands_off_to_linked_callback_and_resumes_without_click(self):
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            task_id = 'linked-callback'
            store.save_state(task_id, {'completed': [], 'runtime': {}})
            b = LinkedReleaseJenkins()
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', linked_read):
                ensure_bootstrap(b, 'job_url/', task_id, 'plugin')
                n, _ = formal_build(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(n, 1)
                self.assertEqual(store.read_state(task_id)['runtime']['plugin_formal_result'], 'DELEGATED')
                self.assertEqual(store.read_state(task_id)['runtime']['plugin_callback_number'], 2)
                v, callback = plugin_artifact_version(b, 'job_url/', n, task_id)
                self.assertEqual(callback, 2)
                self.assertEqual(v, '1.3.1-alpha.1-SNAPSHOT.2')
                # Resuming the task MUST neither click a new Build nor re-watch #1.
                n2, _ = formal_build(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(n2, 1)
                self.assertEqual(len(b.requests), 1)
                self.assertEqual(b.polled, [1, 2])

    def test_modern_jenkins_svg_header_icons_not_other_text(self):
        self.assertEqual(result_from_status_markup('<svg class="svg-icon icon-success" />'), 'SUCCESS')
        self.assertEqual(result_from_status_markup('<svg class="svg-icon icon-failure" />'), 'FAILURE')
        self.assertEqual(result_from_status_markup('<svg class="svg-icon icon-unstable" />'), 'UNSTABLE')
        self.assertIsNone(result_from_status_markup('<svg class="svg-icon icon-running" title="Success" />'))
        self.assertIsNone(result_from_status_markup('Manual Trigger build will be created soon'))
        self.assertIsNone(result_from_status_markup('<svg class="svg-icon icon-clock" />'))

    def test_wait_result_switches_when_related_callback_is_visible(self):
        class Loc:
            def __init__(self, count=0, contents=''):
                self.num, self.contents = count, contents
            def count(self): return self.num
            def nth(self, i): return self
            def evaluate(self, expr): return self.contents
        class Page:
            def __init__(self): self.url = ''
            def locator(self, css):
                if css == 'body': return Loc(1, 'No final Finished marker')
                return Loc(0)
        class B:
            site = 'plugin'
            conf = {'build_timeout_minutes': 2}
            poll = 0.01
            page = Page()
            def visit(self, url, timeout=60000):
                self.page.url = url
                return 200
            def find_callback(self, job_url, source):
                assert source == 1
                return 2
        b = B()
        self.assertEqual(JenkinsBrowser.wait_result(b, 'https://ci.corp/job/release/', 1, watch_callback=True), ('DELEGATED', '2'))

    def test_failure_status_takes_precedence_over_callback(self):
        class Loc:
            def __init__(self, content): self.content = content
            def count(self): return 1
            def nth(self, i): return self
            def evaluate(self, expr): return self.content
        class Page:
            def locator(self, css):
                if css == 'body': return Loc('Still running')
                return Loc('<svg class="svg-icon icon-failure"></svg>')
        class B:
            site = 'plugin'
            conf = {'build_timeout_minutes': 1}
            page = Page()
            def visit(self, url, timeout=60000): return 200
            def find_callback(self, *_): raise AssertionError('Should not follow callback after failure')
        self.assertEqual(JenkinsBrowser.wait_result(B(), 'https://ci.corp/job/release/', 1, watch_callback=True)[0], 'FAILURE')

class TestExistingTaskCallbackRecovery(unittest.TestCase):
    def test_resume_old_running_trigger_build_without_clicking_again(self):
        """Observed on Mac: #1 trigger and #2 callback exist while worker watches #1."""
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            task_id = 'existing-job'
            store.save_state(task_id, {'completed': ['PLUGIN_CHECKOUT', 'PLUGIN_EDIT', 'PLUGIN_PUSH'],
                                       'runtime': {'plugin_formal_number': 1,
                                                   'plugin_formal_baseline': []}})
            b = LinkedReleaseJenkins()
            b.numbers = {1, 2}
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', linked_read):
                n, _ = formal_build(b, 'job_url/', task_id, 'plugin')
                self.assertEqual(n, 1)
                self.assertEqual(b.requests, [])  # no new build, no new branch
                self.assertEqual(store.read_state(task_id)['runtime']['plugin_callback_number'], 2)
                v, target = plugin_artifact_version(b, 'job_url/', n, task_id)
                self.assertEqual((v, target), ('1.3.1-alpha.1-SNAPSHOT.2', 2))

    def test_failed_callback_does_not_advance_to_gmx(self):
        class FailedCallback(LinkedReleaseJenkins):
            def wait_result(self, job_url, num, *, watch_callback=False):
                if num == 2:
                    return 'FAILURE', ''
                return super().wait_result(job_url, num, watch_callback=watch_callback)
        with TemporaryDirectory() as root, patch.object(store, 'DATA', Path(root)):
            task_id = 'bad-callback'
            store.save_state(task_id, {'completed': [], 'runtime': {}})
            b = FailedCallback()
            with patch('appbuilder.jenkins.read_state', store.read_state), \
                    patch('appbuilder.jenkins.patch_runtime', store.patch_runtime), \
                    patch('appbuilder.jenkins.read_full_console', linked_read):
                n, _ = formal_build(b, 'job_url/', task_id, 'plugin')
                with self.assertRaisesRegex(RuntimeError, 'Callback #2 失败'):
                    plugin_artifact_version(b, 'job_url/', n, task_id)

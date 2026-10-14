"""Read-only Jenkins HTML link extraction, with no real Jenkins/TestFairy access."""
import unittest
from copy import deepcopy
from html import escape

from appbuilder.jenkins import NeedsAttention
from appbuilder.jenkins_links import (
    source_build, upload_build_from_html, join_url_from_html, extract_link,
    lookup_app_link, validate_join_url,
)


HOST = 'https://jenkins.link-fixture.invalid'
GMX_JOB = HOST + '/job/GMX/job/feature%2Fcurrent/'
OLD_JOB = HOST + '/job/GMX/job/feature%2Freused/'
UPLOAD_JOB = HOST + '/job/mobile/job/upload-to-testfairy/job/master/'
SOURCE = {'job_url': GMX_JOB, 'number': 812, 'build_url': GMX_JOB + '812/'}
UPLOAD = {'job_url': UPLOAD_JOB, 'number': 36674, 'build_url': UPLOAD_JOB + '36674/'}
JOIN = 'https://tenant.testfairy.com/join/7R3cFa1ryToken'


def block(number=36674, result='SUCCESS', flavor='', href=None):
    """A complete downstream invocation, not merely a build-number mention."""
    href = href if href is not None else UPLOAD_JOB + str(number) + '/'
    return ('<span>[Pipeline] build (' + escape('Mobile' + flavor + ' » upload-to-testfairy » master)') + '</span>\n'
            'Starting building: <a href="' + escape(href, quote=True) + '">#' + str(number) + '</a>\n'
            'Build <a href="' + escape(href, quote=True) + '">#' + str(number) + '</a> completed: ' + result + '\n')


def console_html(contents):
    return '<html><body><pre class="console-output">' + contents + '</pre></body></html>'


class SourceBuildTests(unittest.TestCase):
    def test_current_upload_build_number_is_used_with_current_job(self):
        self.assertEqual(source_build({'gmx_job': GMX_JOB}, {'gmx_build_number': 812}), SOURCE)

    def test_reused_source_job_and_number_are_taken_as_one_pair(self):
        params = {'gmx_job': GMX_JOB}
        runtime = {'gmx_build_number': 812, 'gmx_formal_number': 813,
                   'app_reuse': {'job_url': OLD_JOB, 'build_number': 709,
                                 'build_url': OLD_JOB + '709/'}}
        before = deepcopy((params, runtime))
        self.assertEqual(source_build(params, runtime),
                         {'job_url': OLD_JOB, 'number': 709, 'build_url': OLD_JOB + '709/'})
        self.assertEqual((params, runtime), before)

    def test_incomplete_reuse_pair_cannot_fall_back_to_current_source(self):
        for record in ({'job_url': OLD_JOB}, {'build_number': 709},
                       {'job_url': OLD_JOB, 'build_number': 0},
                       {'job_url': OLD_JOB, 'build_number': True}):
            with self.subTest(record=record), self.assertRaises(NeedsAttention):
                source_build({'gmx_job': GMX_JOB}, {'gmx_build_number': 812, 'app_reuse': record})

    def test_invalid_current_number_is_rejected(self):
        for number in (None, 0, -1, True, '812', 812.5):
            with self.subTest(number=number), self.assertRaises(NeedsAttention):
                source_build({'gmx_job': GMX_JOB}, {'gmx_build_number': number})

    def test_source_url_does_not_admit_credentials_or_query(self):
        for job in ('https://secret@jenkins.link-fixture.invalid/job/GMX/',
                    GMX_JOB + '?token=secret', GMX_JOB + '#wrong',
                    'javascript:alert(1)'):
            with self.subTest(job=job), self.assertRaises(NeedsAttention):
                source_build({'gmx_job': job}, {'gmx_build_number': 812})


class UploadBuildHtmlTests(unittest.TestCase):
    def test_first_successful_segment_selects_36674_not_second_cutting_edge_36675(self):
        html = console_html(block() + block(36675, flavor=' cutting-edge'))
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'), UPLOAD)

    def test_anchor_href_survives_truncated_display_text(self):
        html = console_html(block().replace('>#36674</a>', '>upload-to-testfairy/…/36674</a>'))
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'), UPLOAD)

    def test_relative_href_is_resolved_without_decoding_encoded_job_component(self):
        job = HOST + '/job/mobile%20apps/job/upload-to-testfairy/job/master/'
        html = console_html(block(href='/job/mobile%20apps/job/upload-to-testfairy/job/master/36674/'))
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'),
                         {'job_url': job, 'number': 36674, 'build_url': job + '36674/'})

    def test_missing_first_completion_does_not_borrow_second_segments_success(self):
        incomplete = block().split('Build <a')[0]
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(incomplete + block(36675, flavor=' cutting-edge')),
                                   SOURCE['build_url'] + 'consoleFull')

    def test_first_failed_segment_does_not_skip_to_later_success(self):
        for result in ('FAILURE', 'ABORTED', 'UNSTABLE'):
            with self.subTest(result=result), self.assertRaises(NeedsAttention):
                upload_build_from_html(console_html(block(result=result) + block(36675)),
                                       SOURCE['build_url'] + 'consoleFull')

    def test_text_number_or_ellipsis_is_not_a_complete_build_href(self):
        for body in (block().replace('<a href="' + UPLOAD['build_url'] + '">#36674</a>', '#36674'),
                     block(href='/job/…/36674/'), block(href='/job/.../36674/')):
            with self.subTest(body=body), self.assertRaises(NeedsAttention):
                upload_build_from_html(console_html(body), SOURCE['build_url'] + 'consoleFull')

    def test_other_branches_and_similarly_named_jobs_do_not_match_exact_marker(self):
        for wrong in ('» upload-to-testfairy » develop)',
                      '» upload-to-testfairy-copy » master)',
                      '» upload-to-testfairy » master-old)'):
            html = block().replace('» upload-to-testfairy » master)', wrong)
            with self.subTest(wrong=wrong), self.assertRaises(NeedsAttention):
                upload_build_from_html(console_html(html), SOURCE['build_url'] + 'consoleFull')

    def test_link_after_completion_cannot_supply_missing_build_evidence(self):
        body = ('[Pipeline] build (Mobile » upload-to-testfairy » master)\n'
                'Build #36674 completed: SUCCESS\n'
                '<a href="' + UPLOAD['build_url'] + '">#36674</a>')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(body), SOURCE['build_url'] + 'consoleFull')

    def test_external_host_build_href_is_rejected(self):
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(block(href='https://evil.invalid/job/upload-to-testfairy/job/master/36674/')),
                                   SOURCE['build_url'] + 'consoleFull')


class JoinUrlHtmlTests(unittest.TestCase):
    def test_complete_href_wins_over_truncated_anchor_label(self):
        html = console_html('<a href="' + JOIN + '">https://tenant.testfairy.com/join/…</a>')
        self.assertEqual(join_url_from_html(html, UPLOAD['build_url'] + 'consoleFull'), JOIN)

    def test_main_domain_and_tenant_plain_text_links_are_supported(self):
        for url in (JOIN, 'https://testfairy.com/join/CompleteToken',
                    'https://team-one.testfairy.com/join/Another_Complete-Token'):
            with self.subTest(url=url):
                self.assertEqual(join_url_from_html(console_html('Share link: ' + url + '\n'),
                                                  UPLOAD['build_url'] + 'consoleFull'), url)

    def test_html_entity_in_safe_query_is_preserved_as_url(self):
        url = JOIN + '?source=jenkins&build=36674'
        self.assertEqual(join_url_from_html(console_html('<a href="' + escape(url, quote=True) + '">Share</a>'),
                                          UPLOAD['build_url'] + 'consoleFull'), url)

    def test_truncated_links_are_rejected(self):
        for suffix in ('…', '...', 'Complete…', 'Complete...', '%E2%80%A6', ''):
            url = 'https://tenant.testfairy.com/join/' + suffix
            for body in ('Share link: ' + url, '<a href="' + url + '">Share</a>'):
                with self.subTest(body=body), self.assertRaises(NeedsAttention):
                    join_url_from_html(console_html(body), UPLOAD['build_url'] + 'consoleFull')

    def test_spoofed_domains_non_https_and_userinfo_are_rejected(self):
        for url in ('https://testfairy.com.evil.invalid/join/Token',
                    'https://eviltestfairy.com/join/Token',
                    'https://testfairy.com@evil.invalid/join/Token',
                    'https://secret@tenant.testfairy.com/join/Token',
                    'http://tenant.testfairy.com/join/Token',
                    'https://tenant.testfairy.com/not-join/Token'):
            with self.subTest(url=url), self.assertRaises(NeedsAttention):
                join_url_from_html(console_html('<a href="' + url + '">Share</a>'),
                                   UPLOAD['build_url'] + 'consoleFull')

    def test_authentication_sensitive_query_is_rejected(self):
        for key in ('token', 'access_token', 'id_token', 'api_key', 'authorization', 'password', 'auth', 'code'):
            url = JOIN + '?' + key + '=fixture-secret'
            with self.subTest(key=key), self.assertRaises(NeedsAttention):
                join_url_from_html(console_html('<a href="' + url + '">Share</a>'),
                                   UPLOAD['build_url'] + 'consoleFull')



class FieldScreenshotRegressionTests(unittest.TestCase):
    def test_timestamped_uk_first_upload_and_cutting_edge_keep_distinct_hrefs(self):
        def real_block(number, cutting=False):
            href = '/job/ci/job/shared/job/upload-to-testfairy/job/master/' + str(number) + '/'
            label = 'ci &raquo; shared &raquo; upload-to-testfairy &raquo; master #' + str(number)
            return ('[UK] <span class="timestamp">00:05:18</span> Triggering Testfairy upload job'
                    + (' [cutting edge build]' if cutting else '') + ': (not recorded)\n'
                    '<span class="timestamp">00:05:18</span> [Pipeline] build '
                    '(Building ci &raquo; shared &raquo; upload-to-testfairy &raquo; master)\n'
                    '[UK] <span class="timestamp">00:05:18</span> Scheduling project: ci &raquo; shared '
                    '&raquo; upload-to-testfairy &raquo; master\n'
                    '<span class="timestamp">00:05:24</span> Starting building: <a href="' + href + '">' + label + '</a>\n'
                    '<span class="timestamp">00:07:49</span> Build <a href="' + href + '">' + label
                    + '</a> completed: SUCCESS\n'
                    '<span class="timestamp">00:07:49</span> [Pipeline] echo\n')
        html = console_html(real_block(36674) + real_block(36675, True))
        actual = upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull')
        self.assertEqual(actual['number'], 36674)
        self.assertEqual(actual['build_url'], HOST + '/job/ci/job/shared/job/upload-to-testfairy/job/master/36674/')

    def test_screenshot_join_url_preserves_id_query(self):
        url = JOIN + '?id=573108'
        html = console_html('00:07:43 [TestFairyUpload] [UploadOutcome=Success] Build URL: '
                            '<a href="' + url + '">' + url + '</a>\n')
        self.assertEqual(join_url_from_html(html, UPLOAD['build_url'] + 'console'), url)

    def test_nested_console_divs_are_not_mistaken_for_console_boundary(self):
        html = '<div class="console-output"><div>preamble</div>' + block() + '</div>'
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'), UPLOAD)

    def test_completion_from_other_job_with_same_number_is_not_accepted(self):
        start, complete = block().split('Build <a', 1)
        other = HOST + '/job/other/job/master/36674/'
        complete = complete.replace(UPLOAD['build_url'], other).replace('>#36674</a>', '>other-job #36674</a>')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(start + 'Build <a' + complete), SOURCE['build_url'] + 'consoleFull')

    def test_completion_without_href_can_use_matching_bare_build_number(self):
        start, complete = block().split('Build <a', 1)
        html = console_html(start + 'Build #36674 completed: SUCCESS\n')
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'), UPLOAD)

    def test_buildfoo_is_not_mistaken_for_another_pipeline_build(self):
        html = console_html(block().replace('Build <a', '[Pipeline] buildfoo\nBuild <a'))
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull'), UPLOAD)

    def test_simplified_second_build_marker_bounds_the_incomplete_first_segment(self):
        first = '[Pipeline] build (Mobile » upload-to-testfairy » master)\nScheduling project: upload-to-testfairy\n'
        second = block(36675).replace('[Pipeline] build (Mobile » upload-to-testfairy » master)', '[Pipeline] build')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(first + second), SOURCE['build_url'] + 'consoleFull')

    def test_completion_number_conflict_is_not_accepted(self):
        html = console_html(block().replace('>#36674</a> completed', '>#36675</a> completed'))
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull')

    def test_next_different_job_bounds_an_incomplete_first_target(self):
        first = block().split('Build <a')[0]
        unrelated = block(36675).replace('upload-to-testfairy', 'other-job')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(first + unrelated), SOURCE['build_url'] + 'consoleFull')

    def test_non_build_completed_message_cannot_complete_the_upload(self):
        body = block().replace('Build <a', 'Operation <a')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(body), SOURCE['build_url'] + 'consoleFull')

    def test_multiple_actual_build_hrefs_in_first_segment_are_ambiguous(self):
        body = block().replace('Starting building:',
                               '<a href="' + UPLOAD_JOB + '36675/">#36675</a>\nStarting building:')
        with self.assertRaises(NeedsAttention):
            upload_build_from_html(console_html(body), SOURCE['build_url'] + 'consoleFull')

    def test_sidebar_or_script_links_cannot_supply_console_evidence(self):
        for body in ('<html><body>' + block() + '</body></html>',
                     console_html('<script>' + block() + '</script>nothing')):
            with self.subTest(body=body), self.assertRaises(NeedsAttention):
                upload_build_from_html(body, SOURCE['build_url'] + 'consoleFull')
        html = '<a href="' + JOIN + '">outside log</a>' + console_html('no APP URL')
        with self.assertRaises(NeedsAttention):
            join_url_from_html(html, UPLOAD['build_url'] + 'console')

    def test_first_link_can_be_far_before_log_tail(self):
        html = console_html(block() + 'ordinary log line\n' * 20000 + block(36675))
        self.assertEqual(upload_build_from_html(html, SOURCE['build_url'] + 'consoleFull')['number'], 36674)

    def test_rejected_href_with_convincing_label_is_not_plaintext_evidence(self):
        html = console_html('<a href="https://evil.invalid/join/fake">' + JOIN + '</a>')
        with self.assertRaises(NeedsAttention):
            join_url_from_html(html, UPLOAD['build_url'] + 'console')

    def test_two_different_join_urls_are_rejected(self):
        html = console_html(JOIN + '\nhttps://testfairy.com/join/DifferentToken')
        with self.assertRaises(NeedsAttention):
            join_url_from_html(html, UPLOAD['build_url'] + 'console')

    def test_html_entities_and_split_spans_remain_one_url(self):
        html = console_html('Share: <span>https://tenant.testfairy.com</span>/join/CompleteToken?id=12&amp;source=jenkins')
        self.assertEqual(join_url_from_html(html, UPLOAD['build_url'] + 'console'),
                         'https://tenant.testfairy.com/join/CompleteToken?id=12&source=jenkins')

    def test_plaintext_query_punctuation_is_not_silently_removed(self):
        url = JOIN + '?source=release(v1);'
        self.assertEqual(join_url_from_html(console_html(url), UPLOAD['build_url'] + 'console'), url)

    def test_double_encoded_ellipsis_and_query_credentials_are_rejected(self):
        for url in (JOIN + '%252E%252E%252E', JOIN + '?%74oken=fixture-secret',
                    JOIN + '?session_id=fixture-secret', JOIN + '?authorization=fixture-secret',
                    'https://-.testfairy.com/join/Token', JOIN + '#anything'):
            with self.subTest(url=url), self.assertRaises(NeedsAttention):
                validate_join_url(url)


class HtmlNavigationTests(unittest.TestCase):
    class Page:
        def __init__(self):
            self.url = 'about:blank'
            self.html = ''
        def content(self):
            return self.html
        def title(self):
            return 'Console Output'
        def locator(self, _selector):
            return self
        def all_text_contents(self):
            return []

    class Browser:
        def __init__(self, routes):
            self.page = HtmlNavigationTests.Page()
            self.routes = routes
            self.visits = []
        def visit(self, url):
            self.visits.append(url)
            if url not in self.routes:
                raise AssertionError('Unexpected route: ' + url)
            entry = self.routes[url]
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, tuple):
                status, actual, html = entry
            else:
                status, actual, html = 200, url, entry
            self.page.url, self.page.html = actual, html
            return status

    def setUp(self):
        self.source_console = SOURCE['build_url'] + 'console'
        self.source_full = SOURCE['build_url'] + 'consoleFull'
        self.downstream_console = UPLOAD['build_url'] + 'console'
        self.downstream_full = UPLOAD['build_url'] + 'consoleFull'
        self.routes = {
            self.source_console: '<a href="consoleFull">Full log</a>' + console_html('tail only'),
            self.source_full: console_html(block() + block(36675)),
            UPLOAD['build_url']: '<a href="console">Console Output</a>',
            self.downstream_console: console_html(JOIN),
        }

    def lookup(self, routes=None):
        browser = self.Browser(self.routes if routes is None else routes)
        logs = []
        result = lookup_app_link(browser, GMX_JOB, SOURCE['number'], emit=logs.append)
        return result, browser, logs

    def test_shared_navigation_follows_source_full_log_real_build_and_downstream_console(self):
        result, browser, logs = self.lookup()
        self.assertEqual(browser.visits, [self.source_console, self.source_full,
                                          UPLOAD['build_url'], self.downstream_console])
        self.assertEqual(result['share_url'], JOIN)
        self.assertEqual(result['source_build_number'], 812)
        self.assertEqual(result['downstream_build_number'], 36674)
        self.assertEqual(result['downstream_build_url'], UPLOAD['build_url'])
        self.assertNotIn(JOIN, '\n'.join(logs))
        self.assertNotIn('7R3cFa1ryToken', '\n'.join(logs))
        self.assertFalse(any('consoleText' in url for url in browser.visits))
        self.assertFalse(any('testfairy.com' in url for url in browser.visits))

    def test_optional_downstream_full_log_is_followed_even_if_preview_contains_link(self):
        self.routes[self.downstream_console] += '<a href="consoleFull">Full log</a>'
        self.routes[self.downstream_full] = console_html(JOIN + '?id=573108')
        result, browser, _logs = self.lookup()
        self.assertEqual(browser.visits[-1], self.downstream_full)
        self.assertEqual(result['share_url'], JOIN + '?id=573108')

    def test_source_without_full_log_control_requests_exact_html_full_endpoint(self):
        self.routes[self.source_console] = console_html(block(36675))
        result, browser, _logs = self.lookup()
        self.assertEqual(browser.visits[1], self.source_full)
        self.assertEqual(result['downstream_build_number'], 36674)

    def test_relative_context_path_and_encoded_branch_are_preserved(self):
        job = HOST + '/jenkins/job/GMX/job/upload%252Fbranch/'
        source_console, source_full = job + '27/console', job + '27/consoleFull'
        routes = dict(self.routes)
        routes[source_console] = '<a href="./consoleFull">Full log</a>'
        routes[source_full] = self.routes[self.source_full]
        browser = self.Browser(routes)
        result = lookup_app_link(browser, job, 27, emit=lambda _: None)
        self.assertEqual(browser.visits[:2], [source_console, source_full])
        self.assertEqual(result['source_job_url'], job)

    def test_first_incomplete_target_stops_before_opening_any_downstream(self):
        self.routes[self.source_full] = console_html(block().split('Build <a')[0] + block(36675))
        browser = self.Browser(self.routes)
        with self.assertRaisesRegex(NeedsAttention, '第一个.*缺少完成行'):
            lookup_app_link(browser, GMX_JOB, 812, emit=lambda _: None)
        self.assertEqual(browser.visits, [self.source_console, self.source_full])

    def test_foreign_or_wrong_build_full_log_link_is_not_followed(self):
        for href in ('https://evil.invalid/consoleFull', GMX_JOB + '813/consoleFull',
                     self.source_full + '?password=fixture-secret', self.source_full + '#fragment'):
            routes = dict(self.routes)
            routes[self.source_console] = '<a href="' + href + '">Full log</a>'
            browser = self.Browser(routes)
            logs = []
            with self.subTest(href=href), self.assertRaises(NeedsAttention) as error:
                lookup_app_link(browser, GMX_JOB, 812, emit=logs.append)
            self.assertEqual(browser.visits, [self.source_console])
            self.assertNotIn('fixture-secret', '\n'.join(logs) + str(error.exception))

    def test_wrong_downstream_console_control_is_not_followed(self):
        self.routes[UPLOAD['build_url']] = '<a href="../36675/console">Console Output</a>'
        browser = self.Browser(self.routes)
        with self.assertRaises(NeedsAttention):
            lookup_app_link(browser, GMX_JOB, 812, emit=lambda _: None)
        self.assertEqual(browser.visits[-1], UPLOAD['build_url'])

    def test_failure_or_redirect_at_each_stage_stops_without_using_old_page(self):
        sequence = [self.source_console, self.source_full, UPLOAD['build_url'], self.downstream_console]
        for index, url in enumerate(sequence):
            for entry in ((403, url, self.routes[url]),
                          (200, HOST + '/login?token=fixture-secret', self.routes[url]),
                          (200, url, '<input type="password" value="fixture-secret">' + self.routes[url])):
                with self.subTest(url=url, entry=entry):
                    routes = dict(self.routes)
                    routes[url] = entry
                    browser, logs = self.Browser(routes), []
                    with self.assertRaises(NeedsAttention) as error:
                        lookup_app_link(browser, GMX_JOB, 812, emit=logs.append)
                    self.assertEqual(browser.visits, sequence[:index + 1])
                    self.assertNotIn('fixture-secret', '\n'.join(logs) + str(error.exception))

    def test_browser_error_details_never_reach_diagnostic_or_returned_exception(self):
        self.routes[self.source_full] = NeedsAttention('password=fixture-secret <html>cookie=fixture-secret</html>')
        browser, logs = self.Browser(self.routes), []
        with self.assertRaises(NeedsAttention) as error:
            lookup_app_link(browser, GMX_JOB, 812, emit=logs.append)
        self.assertNotIn('fixture-secret', '\n'.join(logs) + str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_extract_wrapper_reuses_job_and_build_pair(self):
        routes = dict(self.routes)
        old_console, old_full = OLD_JOB + '709/console', OLD_JOB + '709/consoleFull'
        routes[old_console] = '<a href="consoleFull">Full log</a>'
        routes[old_full] = self.routes[self.source_full]
        browser = self.Browser(routes)
        result = extract_link(browser, {'gmx_job': GMX_JOB},
                              {'gmx_build_number': 812, 'app_reuse': {'job_url': OLD_JOB, 'build_number': 709}},
                              emit=lambda _: None)
        self.assertEqual(browser.visits[:2], [old_console, old_full])
        self.assertEqual(result['source_build_number'], 709)
        self.assertEqual(result['source_job_url'], OLD_JOB)


if __name__ == '__main__':
    unittest.main()

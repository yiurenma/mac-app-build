"""Offline real-browser acceptance: the same editor serves builds and schedules."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect
from appbuilder import schedules, store, web


class SharedBuildUIFixture:
    headless = True
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.requests = []
        self.manual = []
        def branches(body, config):
            self.requests.append(body)
            if 'bad' in body['url']:
                raise ValueError('仓库不存在或无读取权限')
            if body['page'] == 1:
                return {'branches': ['develop'] + [f'feature/one-{i}' for i in range(99)], 'has_more': True}
            return {'branches': ['feature/page-two'], 'has_more': False}
        def create_build(body):
            self.manual.append(body)
            return {'id': '202610110900-abcdef'}
        for target, value in [('appbuilder.store.DATA', Path(self.tmp.name)),
                              ('appbuilder.web.get_doctor', lambda: {'ready': True, 'messages': []}),
                              ('appbuilder.branches.list_branches', branches),
                              ('appbuilder.web.create_build', create_build)]:
            patcher = patch(target, value); patcher.start(); self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer((web.HOST, 0), web.make_handler('browser-test-csrf'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.engine = sync_playwright().start(); self.addCleanup(self.engine.stop)
        self.browser = self.engine.chromium.launch(executable_path='/usr/bin/chromium', headless=self.headless,
                                                   args=['--no-sandbox'])
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(f'http://{web.HOST}:{self.server.server_port}')
        expect(self.page.locator('#plugin-branches option')).to_have_count(101)
        self.form = self.page.locator('#build-form')

    def field(self, name):
        scope=self.page if name.startswith(('tf_test_','mail_test_')) else self.form
        return scope.locator(f'[name="{name}"]')

    def open_details(self):
        self.form.locator('details').evaluate_all('(nodes)=>nodes.forEach(n=>n.open=true)')


class SharedBuildUITests(SharedBuildUIFixture, unittest.TestCase):
    def test_defaults_help_branch_paging_refresh_and_manual_fallback(self):
        p = self.page
        self.assertEqual(self.form.locator('[data-build]:visible').count(), 11)
        for name in ['testfairy_login_url','testfairy_folder_path','outlook_account']:
            self.assertFalse(self.field(name).is_visible())
        self.assertEqual(self.field('country').locator('option').count(), 25)
        self.assertEqual(self.field('country').input_value(), 'UK')
        self.assertEqual(p.locator('#gmx-build-live-form [name=country]').input_value(), 'UK')
        self.assertEqual(self.field('plugin_branch').input_value(), 'develop')
        self.assertEqual(self.field('gmx_branch').input_value(), 'develop')
        self.assertEqual(self.field('project_name').input_value(), 'PayByBank')
        self.assertEqual(self.field('max_parallel_display').input_value(), '10')
        self.assertEqual(self.form.locator('[name=gmx_custom_folder]').count(), 1)
        self.assertEqual(p.locator('#schedules input, #schedules select').count(), 0)
        self.assertEqual(self.form.locator('#gmx-settings [name^=selector_]').count(), 8)
        self.assertEqual(self.form.locator('#gmx-settings [name=upload_success_pattern]').count(),1)
        self.assertEqual(self.form.locator('#advanced-settings [name=upload_success_pattern]').count(),0)
        self.assertTrue(self.field('plugin_parent').input_value().endswith('/job/release-build/job/'))
        self.assertEqual(p.locator('#derived-plugin').text_content(), 'release/v1.3.[timestamp]')
        self.field('plugin_version').fill('2.4')
        expect(p.locator('#derived-plugin')).to_have_text('release/v2.4.[timestamp]')
        self.assertEqual(p.locator('#derived-gmx').text_content(), 'upload/AUTO-[timestamp]')
        self.open_details()
        self.assertIn('0_A_<国家>_<项目>',self.field('gmx_custom_folder').locator('..').inner_text())
        self.assertIn('0_A_<国家>_<项目>',p.locator('#gmx-build-live-form [name=custom_folder]').locator('..').inner_text())
        self.assertEqual(p.locator('input:not([type=hidden]),select').evaluate_all('''nodes=>nodes.filter(n=>{
          const label=n.closest('label');return !label||!label.querySelector('small')||!/[\\u4e00-\\u9fff]/.test(label.textContent);
        }).map(n=>n.name||n.id)'''), [])
        self.assertTrue(self.field('visible').is_checked())
        self.assertEqual(self.field('browser_channel').input_value(),'chrome')
        for name in ['gmx_force', 'gmx_skip_test', 'gmx_sync_upload']:
            self.assertTrue(self.field(name).is_checked())
        self.assertFalse(self.field('gmx_keep_alive').is_checked())
        self.field('plugin_branch').fill('feature/page')
        self.assertEqual(self.field('plugin_branch').get_attribute('list'), 'plugin-branches')
        self.field('plugin_branch').fill('feature/page-two')
        before=len(self.requests)
        p.locator('.branch-refresh[data-kind=plugin]').click()
        expect(p.locator('#plugin-branches option')).to_have_count(101)
        self.assertGreater(len(self.requests), before)
        self.field('plugin_url').fill('https://alm-github.systems.uk.hsbc/mobile/bad')
        self.field('plugin_url').press('Tab')
        expect(p.locator('#plugin-branch-status')).to_contain_text('失败')
        self.assertEqual(p.locator('#plugin-branches option').count(), 0)
        self.field('plugin_branch').fill('manual/fallback')
        self.assertEqual(self.field('plugin_branch').input_value(), 'manual/fallback')
        self.assertFalse(self.errors, self.errors)

    def test_create_edit_save_defaults_and_all_advanced_values_round_trip(self):
        p=self.page;self.open_details()
        self.field('gmx_issue_key').fill('UKPBB-203745')
        self.field('gmx_custom_folder').fill('custom_release')
        self.field('gmx_build_type').fill('Release')
        self.field('gmx_signing_repo_branch').fill('release/sign')
        self.field('poll_seconds').fill('55')
        self.field('browser_channel').select_option('msedge')
        self.field('selector_force').fill('#custom-force')
        self.field('upload_success_pattern').fill('UPLOAD.*SUCCESS')
        self.field('visible').uncheck();self.field('verbose').uncheck()
        self.field('gmx_force').uncheck();self.field('gmx_keep_alive').check()
        self.field('country').select_option('ALL')
        expected=p.evaluate('buildBody()')
        for key in ['github_token','jenkins_user','jenkins_password','testfairy_user','testfairy_password']:expected.pop(key)
        p.locator('#save-as-schedule').check()
        self.field('schedule_name').fill('nightly')
        self.field('schedule_cron').fill('* * * * *')
        expect(p.locator('#cron-preview')).to_contain_text('最小')
        self.field('schedule_cron').fill('0 9 * * 1-5')
        expect(p.locator('#cron-preview')).to_contain_text('接下来 5 次：')
        self.field('schedule_enabled').uncheck()
        p.locator('#submit-build').click()
        expect(p.locator('#schedules-body')).to_contain_text('nightly')
        self.assertEqual(len(schedules.list_schedules()),1)
        row=schedules.list_schedules()[0]
        self.assertEqual(json.loads(row['params']), expected)
        self.assertFalse(row['enabled'])
        p.locator('#schedules-body button', has_text='编辑').click()
        actual=p.evaluate('buildBody()')
        for key in ['github_token','jenkins_user','jenkins_password','testfairy_user','testfairy_password']:actual.pop(key)
        self.assertEqual(actual, expected)
        self.field('schedule_name').fill('renamed')
        p.locator('#submit-build').click()
        expect(p.locator('#schedules-body')).to_contain_text('renamed')
        self.assertEqual(len(schedules.list_schedules()),1)
        self.assertEqual(json.loads(schedules.get_schedule(row['id'])['params']), expected)
        p.locator('#schedules-body button', has_text='编辑').click()
        p.locator('#save-as-schedule').uncheck()
        p.locator('#submit-build').click()
        expect(p.locator('#form-message')).to_contain_text('任务已创建')
        manual={k:v for k,v in self.manual[0].items() if k in expected}
        self.assertEqual(manual,expected)
        # No real worker or scheduler was started by any browser action.
        self.assertEqual(store.task_list(),[])
        self.assertFalse(self.errors,self.errors)

    def test_schedule_list_actions_recent_twenty_runs_and_log_links(self):
        from datetime import datetime, timedelta
        from contextlib import closing
        config=web.load_config()
        template=web.form_defaults(config)
        template.pop('max_parallel');template.pop('gmx_fields')
        row=schedules.create_schedule('actions', '0 9 * * *', template)
        task=store.create_task(config,web.validate_build_parameters(template,config))
        store.update_task(task['id'],status='SUCCESS')
        (store.task_folder(task['id'])/'run.log').write_text('offline build evidence')
        with closing(schedules.connect()) as db,db:
            for i in range(22):
                planned=(datetime(2026,9,1)+timedelta(days=i)).isoformat()
                db.execute("INSERT INTO schedule_runs(schedule_id,planned,status,task_id,note,created) VALUES(?,?,?,?,?,?)",
                           (row['id'],planned,'STARTED',task['id'],'只补最近一次',planned))
        p=self.page;p.reload()
        expect(p.locator('#schedules-body')).to_contain_text('actions')
        p.locator('#schedules-body button',has_text='停用').click()
        expect(p.locator('#schedules-body')).to_contain_text('已停用')
        p.locator('#schedules-body button',has_text='启用').click()
        expect(p.locator('#schedules-body button',has_text='停用')).to_be_visible()
        p.locator('#schedules-body button',has_text='记录').click()
        expect(p.locator('#schedule-runs-body tr')).to_have_count(20)
        p.locator('#schedule-runs-body .sched-link').first.click()
        expect(p.locator('#terminal')).to_contain_text('offline build evidence')
        p.once('dialog',lambda dialog:dialog.accept())
        p.locator('#schedules-body button',has_text='立即运行').click()
        expect(p.locator('#toast')).to_contain_text('已排队')
        expect(p.locator('#schedule-runs')).to_be_visible()
        expect(p.locator('#schedule-runs-body tr').first).to_contain_text('PENDING')
        expect(p.locator('#schedule-runs-body tr').first).to_contain_text('尚未启动')
        self.assertEqual(p.evaluate('state.selected'),task['id'])
        self.assertEqual(schedules.list_runs(row['id'])[0]['status'],'PENDING')
        p.once('dialog',lambda dialog:dialog.accept())
        p.locator('#schedules-body button',has_text='删除').click()
        expect(p.locator('#schedules-body')).to_contain_text('还没有定时任务')
        self.assertEqual(store.task(task['id'])['status'],'SUCCESS')
        self.assertEqual((store.task_folder(task['id'])/'run.log').read_text(),'offline build evidence')
        self.assertFalse(self.errors,self.errors)

    def test_responsive_layout_and_cancel_restores_shared_defaults(self):
        p=self.page;p.locator('#new-schedule').click()
        self.field('schedule_name').fill('cancelled')
        self.field('country').select_option('HK')
        p.locator('#schedule-cancel').click()
        self.assertEqual(self.field('country').input_value(),'UK')
        self.assertFalse(p.locator('#save-as-schedule').is_checked())
        p.set_viewport_size({'width':390,'height':844})
        p.locator('#new-build').scroll_into_view_if_needed()
        box=p.locator('#new-build').bounding_box()
        self.assertLessEqual(box['x']+box['width'],390)
        self.open_details()
        for field in self.form.locator('input:visible,select:visible').all():
            box=field.bounding_box()
            self.assertGreaterEqual(box['x'],0)
            self.assertLessEqual(box['x']+box['width'],390)
        Path('/tmp/mac-app-build-ui').mkdir(exist_ok=True)
        p.screenshot(path='/tmp/mac-app-build-ui/mobile.png',full_page=True)
        p.set_viewport_size({'width':1440,'height':1000})
        p.screenshot(path='/tmp/mac-app-build-ui/desktop.png',full_page=True)
        self.assertFalse(self.errors,self.errors)

    def test_independent_link_and_email_buttons_use_test_apis_without_build(self):
        p=self.page
        self.assertEqual(p.locator('#build-form #tf-test-form, #build-form #mail-test-form').count(),0)
        self.assertEqual(p.locator('#build-form .group-head:has(span)').all_text_contents(),['01 Plugin','02 GMX','03 TestFairy','04 邮件'])
        p.locator('.nav[data-target=testfairy-tests]').click()
        self.assertEqual(p.locator('#tf-test-form input[type=password]').count(),0)
        self.field('tf_test_expected').fill('https://hsbc-mobilex.testfairy.com/join/current')
        p.route('**/api/tasks',lambda route:route.fulfill(json={'tasks':[{'id':'202610111000-abcdef','project':'PayByBank','country':'UK','status':'FAILED','stage':'GMX_JENKINS'}]}))
        p.evaluate("state.tasks=[{id:'202610111000-abcdef',project:'PayByBank',country:'UK',status:'FAILED'}];renderTestTasks()")
        self.field('tf_test_task').select_option('202610111000-abcdef')
        result={'id':'c'*32,'status':'MATCH','result':{'message':'分享链接匹配','expected_share_url':'https://hsbc-mobilex.testfairy.com/join/current','actual_share_url':'https://hsbc-mobilex.testfairy.com/join/current','diagnostics':{'attempt_id':'attempt-1','log_path':'/tmp/trace.jsonl','evidence_path':'/tmp/evidence.json','first_error':{'stage':'navigation','type':'TimeoutError','message':'timeout'},'logging_error':'OSError: disk unavailable'}}}
        with patch('appbuilder.delivery_tests.start',return_value={'id':'c'*32}) as start,patch('appbuilder.delivery_tests.poll',side_effect=lambda _:result):
            p.locator('#test-testfairy').click()
            expect(p.locator('#tf-test-result')).to_contain_text('MATCH')
            for detail in ('attempt-1','/tmp/trace.jsonl','/tmp/evidence.json','navigation','TimeoutError','disk unavailable'):
                expect(p.locator('#tf-test-result')).to_contain_text(detail)
            self.assertEqual(start.call_args.args[0]['task_id'],'202610111000-abcdef')
            self.assertEqual(start.call_args.args[0]['expected_share_url'],'https://hsbc-mobilex.testfairy.com/join/current')
            self.assertNotIn('password',start.call_args.args[0])
            self.assertNotIn('username',start.call_args.args[0])
            self.field('mail_test_task').select_option('202610111000-abcdef')
            self.field('mail_test_to').fill('recipient@example.com')
            result.update(status='BLOCKED',result={'message':'离线模拟：未调用发送','stage':'set_recipients','send_attempted':False,'error_number':-1708,'returncode':1})
            p.locator('#test-outlook-send').click()
            expect(p.locator('#mail-test-result')).to_contain_text('BLOCKED')
            expect(p.locator('#mail-test-result')).to_contain_text('set_recipients')
            expect(p.locator('#mail-test-result')).to_contain_text('send 调用：未调用')
            expect(p.locator('#mail-test-result')).to_contain_text('原生错误号：-1708')
            expect(p.locator('#mail-test-result')).to_contain_text('退出码：1')
            self.assertTrue(start.call_args.args[0]['confirm_send'])
            self.assertFalse(start.call_args.args[0]['allow_repeat_test'])
            self.field('mail_test_allow_repeat').check()
            p.locator('#test-outlook-send').click()
            expect(p.locator('#test-outlook-send')).to_be_enabled()
            self.assertTrue(start.call_args.args[0]['allow_repeat_test'])
            self.assertFalse(self.field('mail_test_allow_repeat').is_checked())
            self.assertLessEqual(self.field('mail_test_allow_repeat').bounding_box()['height'],24)
            self.assertNotIn('allow_repeat_test',p.evaluate('buildBody()'))
            self.assertEqual(start.call_args.args[0]['task_id'],'202610111000-abcdef')
            self.assertEqual(start.call_args.args[0]['recipients'],'recipient@example.com')
        self.assertFalse(self.manual)
        self.assertFalse(self.errors,self.errors)
        destination=Path('/tmp/mac-app-build-ui');destination.mkdir(exist_ok=True)
        p.locator('#testfairy-tests').screenshot(path=str(destination/'testfairy-test.png'))
        p.locator('#mail-tests').screenshot(path=str(destination/'outlook-test.png'))

    def test_delivery_history_search_and_choices_are_independent(self):
        p=self.page
        rows=[{'id':'old-failed','project':'PayByBank','country':'UK','status':'FAILED','stage':'GMX_JENKINS'},
              {'id':'new-success','project':'Wallet','country':'HK','status':'SUCCESS','stage':'GMX_JENKINS'}]
        p.route('**/api/tasks',lambda route:route.fulfill(json={'tasks':rows}))
        p.evaluate("""state.tasks=[
          {id:'old-failed',project:'PayByBank',country:'UK',status:'FAILED'},
          {id:'new-success',project:'Wallet',country:'HK',status:'SUCCESS'}];
          renderTestTasks();""")
        self.field('mail_test_search').fill('failed')
        expect(self.field('mail_test_task').locator('option')).to_have_count(2)
        self.field('mail_test_task').select_option('old-failed')
        self.field('tf_test_search').fill('wallet')
        self.field('tf_test_task').select_option('new-success')
        p.evaluate("state.selected='new-success';renderSelection();renderTestTasks()")
        self.assertEqual(self.field('mail_test_task').input_value(),'old-failed')
        self.assertEqual(self.field('tf_test_task').input_value(),'new-success')
        self.field('mail_test_search').fill('HK')
        # Keep the explicit choice visible during filtering and periodic refresh.
        self.assertEqual(self.field('mail_test_task').input_value(),'old-failed')
        expect(self.field('mail_test_task').locator('option')).to_have_count(3)
        self.field('mail_test_task').select_option('new-success')
        self.assertEqual(self.field('tf_test_task').input_value(),'new-success')
        self.assertIn('信息不足会提示',self.field('tf_test_task').locator('..').inner_text())
        self.assertFalse(self.errors,self.errors)

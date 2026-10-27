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
from chromium_path import CHROMIUM


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
        self.browser = self.engine.chromium.launch(executable_path=CHROMIUM, headless=self.headless,
                                                   args=['--no-sandbox'])
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(f'http://{web.HOST}:{self.server.server_port}')
        expect(self.page.locator('#plugin-branches option')).to_have_count(101)
        self.form = self.page.locator('#build-form')

    def field(self, name):
        scope=self.page if name.startswith('tf_test_') else self.form
        return scope.locator(f'[name="{name}"]')

    def show(self, panel):
        # Panels are collapsed by default; open one the same way a person does. A test
        # tool (old panel id) is a tab inside 测试工具: open that panel, then its tab.
        tools = {'diagnostics', 'live-tests', 'testfairy-tests', 'jira-tools'}
        self.page.locator(f'.nav[data-target={"tools" if panel in tools else panel}]').click()
        if panel in tools:
            self.page.locator(f'[data-tool={panel}]').click()
        expect(self.page.locator('#' + panel)).to_be_visible()

    def show_all(self):
        # Only for tests that drive several tool panels in one flow.
        self.page.evaluate("document.querySelectorAll('.fold').forEach(e=>e.classList.remove('collapsed'));"
                           "['diagnostics','live-tests','testfairy-tests','jira-tools'].forEach(id=>document.getElementById(id).hidden=false)")

    def open_details(self):
        self.form.locator('details').evaluate_all('(nodes)=>nodes.forEach(n=>n.open=true)')


class SharedBuildUITests(SharedBuildUIFixture, unittest.TestCase):
    def test_tool_panels_are_collapsed_and_open_one_at_a_time(self):
        p = self.page
        p.reload()
        for i in ['delivery', 'schedules', 'settings', 'tools']:
            expect(p.locator('#' + i)).to_be_hidden()
        self.assertEqual(p.locator('#mail-tests, [data-target=mail-tests], #jira-tests').count(), 0)
        # Menu: two groups, Chinese labels, the test tools behind one entry.
        self.assertEqual(p.locator('.sidebar-label').all_text_contents(), ['日常', '设置与排查'])
        self.assertEqual([t.split()[-1] for t in p.locator('.nav').all_inner_texts()],
                         ['概览', '新建构建', '运行记录', '交付记录', '定时任务', '设置', '测试工具'])
        p.locator('[data-target=settings]').click()
        expect(p.locator('#settings')).to_be_visible()
        p.locator('[data-target=tools]').click()
        expect(p.locator('#tools')).to_be_visible()
        expect(p.locator('#settings')).to_be_hidden()
        expect(p.locator('#diagnostics')).to_be_visible()          # first tab by default
        expect(p.locator('#testfairy-tests')).to_be_hidden()
        p.locator('[data-tool=testfairy-tests]').click()
        expect(p.locator('#testfairy-tests')).to_be_visible()
        expect(p.locator('#diagnostics')).to_be_hidden()
        expect(p.locator('[data-tool=testfairy-tests]')).to_have_attribute('aria-selected', 'true')
        p.evaluate("nav('jira-tools')")                             # an old panel id opens its tab
        expect(p.locator('#jira-tools')).to_be_visible()
        p.locator('[data-target=tools]').click()
        expect(p.locator('#tools')).to_be_hidden()
        expect(p.locator('input[name=testfairy_enabled]')).to_be_checked()
        expect(p.locator('input[name=jenkins_api]')).to_be_checked()
        # Staff ID and tokens live on the settings page, never part of a build or schedule.
        self.assertEqual(p.locator('#build-form #jenkins-token-box, #build-form #credentials-form').count(), 0)
        self.assertEqual(p.locator('#settings #jenkins-token-box, #settings #credentials-form, #settings #jira-form').count(), 3)
        self.assertEqual(p.locator('#jenkins-token-box [data-build], #credentials-form [data-build]').count(), 0)
        for name in ('jira_token', 'github_token', 'jenkins_user', 'jenkins_password'):
            self.assertEqual(self.form.locator(f'[name={name}]').count(), 0)
        self.assertNotIn('jenkins-token', ' '.join(p.evaluate('Object.keys(buildBody())')))

    def test_defaults_help_branch_paging_refresh_and_manual_fallback(self):
        p = self.page
        self.assertEqual(self.form.locator('[data-build]:visible').count(), 7)  # tokens moved to the settings page
        for name in ['outlook_auto_send','outlook_recipients','outlook_account']:
            self.assertEqual(self.field(name).count(), 0)   # email now goes through Jira
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
        self.show_all();p.locator('#schedules-body button', has_text='编辑').click()
        actual=p.evaluate('buildBody()')

        self.assertEqual(actual, expected)
        self.field('schedule_name').fill('renamed')
        p.locator('#submit-build').click()
        expect(p.locator('#schedules-body')).to_contain_text('renamed')
        self.assertEqual(len(schedules.list_schedules()),1)
        self.assertEqual(json.loads(schedules.get_schedule(row['id'])['params']), expected)
        self.show_all();p.locator('#schedules-body button', has_text='编辑').click()
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
        p=self.page;p.reload();self.show('schedules')
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
        p=self.page;self.show('schedules');p.locator('#new-schedule').click()
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

    def test_independent_link_button_uses_test_api_without_build(self):
        p=self.page
        self.assertEqual(p.locator('#build-form #tf-test-form').count(),0)
        self.assertEqual(p.locator('#build-form .group-head:has(span)').all_text_contents(),['00 Jenkins 登录','01 Plugin','02 GMX','03 App 链接','04 Jira 回填与邮件'])
        self.show('testfairy-tests')
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
        self.assertFalse(self.manual)
        self.assertFalse(self.errors,self.errors)
        destination=Path('/tmp/mac-app-build-ui');destination.mkdir(exist_ok=True)
        p.locator('#testfairy-tests').screenshot(path=str(destination/'testfairy-test.png'))

    def test_overview_shows_running_last_delivery_and_attention(self):
        p=self.page
        data={'running':{'task_id':'T4','created':'2026-10-15T21:42:00','status':'RUNNING','steps':[
                  {'label':'Plugin 构建','state':'done'},{'label':'GMX 构建','state':'current'},{'label':'取 App 链接','state':'waiting'},
                  {'label':'回填 Jira','state':'waiting'},{'label':'发邮件','state':'waiting'}]},
              'last':{'task_id':'T2','created':'2026-10-15T16:56:00','link':'https://tenant.testfairy.com/join/abc',
                      'jira_status':'DONE','tickets':['TRANSACT-1'],'mail_status':'SENT','mail_to':3},
              'attention':[{'kind':'task','task_id':'T3','status':'FAILED','stage':'GMX_JENKINS','text':'<b>boom</b>'},
                           {'kind':'missed','name':'nightly','count':2,'planned':'x'}],
              'schedules':{'online':True,'items':[{'name':'nightly','cron':'0 */6 * * *','enabled':True,'next_run':'2026-10-16T00:00:00'}]},
              'automation':{'jira_enabled':True,'jira_dry_run':True,'mail_enabled':True,'mail_failure_once':True},'counts':{}}
        p.route('**/api/overview',lambda route:route.fulfill(json=data))
        p.reload();expect(p.locator('#plugin-branches option')).to_have_count(101)
        expect(p.locator('#ov-running li')).to_have_count(5)
        expect(p.locator('#ov-running li.current')).to_have_text('●GMX 构建')
        expect(p.locator('#ov-last a')).to_have_attribute('href','https://tenant.testfairy.com/join/abc')
        expect(p.locator('#ov-last')).to_contain_text('TRANSACT-1 → 已更新')
        expect(p.locator('#ov-last')).to_contain_text('已发 3 人')
        expect(p.locator('#ov-attention')).to_be_visible()
        expect(p.locator('#ov-attention')).to_contain_text('T3 失败（GMX 构建）：<b>boom</b>')   # text, not HTML
        expect(p.locator('#ov-attention')).to_contain_text('错过 2 次')
        expect(p.locator('#ov-schedules')).to_contain_text('调度器运行中')
        expect(p.locator('#ov-auto')).to_contain_text('演练模式')
        p.locator('#ov-attention button',has_text='查看 →').click()          # links open the right panel
        expect(p.locator('#schedules')).to_be_visible()
        expect(p.locator('#crumb')).to_have_text('定时任务')
        self.assertFalse(self.errors,self.errors)

    def test_delivery_page_lists_tickets_with_links_and_mails_with_bodies(self):
        p=self.page;seen=[]
        tickets={'jira_base':'https://jira.example.com','items':[
            {'at':'2026-10-15T12:30:00','key':'TRANSACT-1','summary':'Fix <b>login</b>','steps':['comment','assign','transition'],
             'link':'https://tenant.testfairy.com/join/abc','task_id':'T2','build':7,'result':'DONE','note':''},
            {'at':'2026-10-15T12:30:00','key':'TRANSACT-2','summary':'','steps':[],'link':'javascript:alert(1)',
             'task_id':'T2','build':7,'result':'MANUAL','note':'Fix Note 未配置'}]}
        mails={'items':[{'at':'2026-10-15T12:31:00','kind':'failure','task_id':'T3','subject':'[构建失败] UK','to':['a','b'],
                         'status':'SENT','reason':'','body':'失败步骤：GMX 构建'}]}
        def handle(route):
            seen.append(route.request.url)
            route.fulfill(json=mails if '/mails' in route.request.url else tickets)
        p.route('**/api/delivery/**',handle)
        self.show('delivery')
        expect(p.locator('#delivery-body tr')).to_have_count(2)
        first=p.locator('#delivery-body tr').first
        expect(first.locator('a',has_text='TRANSACT-1')).to_have_attribute('href','https://jira.example.com/browse/TRANSACT-1')
        expect(first.locator('a',has_text='打开')).to_have_attribute('href','https://tenant.testfairy.com/join/abc')
        expect(first).to_contain_text('Fix <b>login</b>')                 # shown as text, never as HTML
        second=p.locator('#delivery-body tr').nth(1)
        self.assertEqual(second.locator('a',has_text='打开').count(),0)     # a non-https link is not clickable
        expect(second).to_contain_text('状态需手动改')
        p.locator('[data-delivery=mails]').click()
        expect(p.locator('#delivery-body tr')).to_have_count(1)
        expect(p.locator('#delivery-body')).to_contain_text('构建失败')
        expect(p.locator('#delivery-body')).to_contain_text('a、b')
        p.locator('#delivery-body summary').click()
        expect(p.locator('#delivery-body pre')).to_contain_text('失败步骤：GMX 构建')
        p.locator('#delivery-search').fill('TRANSACT-9')
        p.wait_for_timeout(600)
        self.assertTrue(any('q=TRANSACT-9' in u for u in seen))
        self.assertFalse(self.errors,self.errors)

    def test_settings_page_saves_github_and_browser_login_to_keychain(self):
        p=self.page;saved=[]
        def fake_save(body):
            saved.append(dict(body))
            return {'github':True,'jenkins_user':body.get('jenkins_user') or ''}
        with patch('appbuilder.credentials_api.save',side_effect=fake_save), \
                patch('appbuilder.credentials_api.status',return_value={'github':False,'jenkins_user':''}):
            p.reload();expect(p.locator('#plugin-branches option')).to_have_count(101)
            self.show('settings')
            expect(p.locator('#credentials-status')).to_contain_text('GitHub Token：未保存')
            f=p.locator('#credentials-form')
            f.locator('[name=github_token]').fill('pat-value')
            f.locator('[name=jenkins_user]').fill('george')
            f.locator('[name=jenkins_password]').fill('pw-value')
            p.locator('#credentials-save').click()
            expect(p.locator('#credentials-status')).to_contain_text('浏览器登录：已保存（george）')
        self.assertEqual(saved,[{'github_token':'pat-value','jenkins_user':'george','jenkins_password':'pw-value'}])
        self.assertEqual(f.locator('[name=github_token]').input_value(),'')       # secrets cleared after saving
        self.assertEqual(f.locator('[name=jenkins_password]').input_value(),'')
        self.assertNotIn('github_token',p.evaluate('buildBody()'))
        self.assertFalse(self.errors,self.errors)

    def test_offline_ai_card_installs_switches_and_tries(self):
        p=self.page;posts=[]
        models={'phi4-mini':{'label':'Phi-4-mini-instruct · Q4_K_M · 约 2.5 GB','licence':'MIT（Microsoft）','source':'https://huggingface.co/microsoft/Phi-4-mini-instruct'},
                'qwen3-4b':{'label':'Qwen3-4B · Q4_K_M · 约 2.5 GB','licence':'Apache 2.0','source':'https://huggingface.co/Qwen/Qwen3-4B'}}
        st={'enabled':False,'model':'','model_path':'','model_ok':False,'model_note':'','runtime':'','features':{'tester_note':True,'failure_hint':True},
            'models':models,'model_dir':'/Users/g/.app-builder/models','job':{'running':False,'log':[]}}
        def handle(route):
            path=route.request.url.split('/api/ai/')[1]
            if route.request.method=='POST':
                body=json.loads(route.request.post_data or '{}');posts.append((path,body))
                if path=='install':
                    st['job']={'running':True,'step':'下载模型','progress':42.5,'log':['10:00:00 下载模型 m.gguf（2.5 GB）']}
                elif path=='settings':
                    st['features'].update({k:v for k,v in body.items() if k in st['features']})
                elif path=='try':
                    return route.fulfill(json={'tester_note':'Check the consent screen sends the account id.',
                                               'failure_hint':{'cause':'Signing certificate missing.','fix':'Reinstall it.'}})
            return route.fulfill(json=st)
        p.route('**/api/ai/**',handle)
        p.reload();expect(p.locator('#plugin-branches option')).to_have_count(101)
        self.show('settings')
        expect(p.locator('#ai-status')).to_contain_text('运行库：未安装')
        expect(p.locator('#ai-model option')).to_have_count(2)
        p.locator('#ai-model').select_option('qwen3-4b')
        p.locator('#ai-install').click()
        expect(p.locator('#ai-log')).to_contain_text('进行中：下载模型 42.5%')
        self.assertTrue(p.locator('#ai-install').is_disabled())
        p.locator('#ai-form [name=failure_hint]').uncheck()
        p.locator('#ai-try').click()
        expect(p.locator('#ai-log')).to_contain_text('Try: Reinstall it.')
        self.assertEqual(posts[:2],[('install',{'model':'qwen3-4b'}),('settings',{'failure_hint':False})])
        self.assertFalse(self.errors,self.errors)

    def test_jira_panel_saves_mail_settings_and_sends_a_test_email(self):
        p=self.page;posts={}
        settings={'settings':{'enabled':True,'dry_run':True,'base_url':'https://jira.example.com','transition_name':'In Test',
                   'assign_to_reporter':True,'use_pr_lookup':True,'key_pattern':'x','first_run_commits':0,'max_tickets_per_run':40,
                   'flavor':'server','transition_fields':'','mail_enabled':False,'mail_issue':'','mail_users':''},'token_source':'keychain'}
        def handle(route):
            req=route.request
            if req.method=='POST':
                posts[req.url.rsplit('/',1)[1]]=json.loads(req.post_data)
                if req.url.endswith('/mail-test'):
                    return route.fulfill(json={'ok':True,'issue':'TRANSACT-9','to':['44065253','43123456'],'subject':'[TEST] [新测试包] UK','from_task':'T1','request':'POST https://jira.example.com/rest/api/2/issue/TRANSACT-9/notify'})
                body=posts['settings'];settings['settings'].update({k:body[k] for k in ('mail_enabled','mail_issue','mail_users')})
            route.fulfill(json=settings)
        p.route('**/api/jira/**',handle)
        p.reload();expect(p.locator('#plugin-branches option')).to_have_count(101)
        self.show('settings')
        f=p.locator('#jira-form')
        expect(p.locator('#build-jira-status')).to_contain_text('邮件通知：未开启')
        f.locator('[name=mail_enabled]').check()
        f.locator('[name=mail_issue]').fill('TRANSACT-9')
        f.locator('[name=mail_users]').fill('44065253, 43123456')
        p.locator('#jira-save').click()
        expect(p.locator('#jira-settings-result')).to_contain_text('设置已保存')
        self.assertEqual({k:posts['settings'][k] for k in ('mail_enabled','mail_issue','mail_users','mail_failure_once')},
                         {'mail_enabled':True,'mail_issue':'TRANSACT-9','mail_users':'44065253, 43123456','mail_failure_once':True})
        expect(p.locator('#build-jira-status')).to_contain_text('邮件通知：已开启')
        p.locator('#jira-mail-test').click()
        expect(p.locator('#jira-settings-result')).to_contain_text('收件人：44065253、43123456')
        expect(p.locator('#jira-settings-result')).to_contain_text('POST https://jira.example.com/rest/api/2/issue/TRANSACT-9/notify')
        self.assertEqual(posts['mail-test'],{'mail_issue':'TRANSACT-9','mail_users':'44065253, 43123456','mail_groups':''})
        self.assertFalse(self.errors,self.errors)

    def test_delivery_history_search_and_choices_are_independent(self):
        p=self.page
        rows=[{'id':'old-failed','project':'PayByBank','country':'UK','status':'FAILED','stage':'GMX_JENKINS'},
              {'id':'new-success','project':'Wallet','country':'HK','status':'SUCCESS','stage':'GMX_JENKINS'}]
        p.route('**/api/tasks',lambda route:route.fulfill(json={'tasks':rows}))
        p.evaluate("""state.tasks=[
          {id:'old-failed',project:'PayByBank',country:'UK',status:'FAILED'},
          {id:'new-success',project:'Wallet',country:'HK',status:'SUCCESS'}];
          renderTestTasks();""")
        self.show_all();self.field('tf_test_search').fill('failed')
        expect(self.field('tf_test_task').locator('option')).to_have_count(2)
        self.field('tf_test_task').select_option('old-failed')
        p.evaluate("state.selected='new-success';renderSelection();renderTestTasks()")
        self.assertEqual(self.field('tf_test_task').input_value(),'old-failed')
        self.field('tf_test_search').fill('HK')
        # Keep the explicit choice visible during filtering and periodic refresh.
        self.assertEqual(self.field('tf_test_task').input_value(),'old-failed')
        expect(self.field('tf_test_task').locator('option')).to_have_count(3)
        self.field('tf_test_task').select_option('new-success')
        self.assertEqual(self.field('tf_test_task').input_value(),'new-success')
        self.assertIn('信息不足会提示',self.field('tf_test_task').locator('..').inner_text())
        self.assertFalse(self.errors,self.errors)

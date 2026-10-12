"""Copy is an editor action, never a schedule dispatch; real Chromium in both modes."""
import json
import threading
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch
from playwright.sync_api import expect
from appbuilder import schedules, store, web
import test_shared_build_ui as shared


class CopyScenarios(shared.SharedBuildUIFixture):
    def setUp(self):
        super().setUp()
        auth = patch('appbuilder.testfairy.saved_auth_available', return_value=True)
        auth.start(); self.addCleanup(auth.stop)
        template = {**web.form_defaults(web.load_config()), 'country':'ALL', 'project_name':'CopiedApp',
                    'gmx_issue_key':'UKPBB-77','plugin_branch':'feature/source','visible':False,
                    'testfairy_enabled':True,'testfairy_save_credentials':True,
                    'outlook_auto_send':True,'outlook_recipients':'fixture@example.com'}
        allowed = set(schedules.BASIC_KEYS+schedules.OPTIONAL_TEXT+schedules.OPTIONAL_INT+schedules.OPTIONAL_BOOL)
        template = {k:v for k,v in template.items() if k in allowed}
        self.source = schedules.create_schedule('source', '0 9 * * 1-5', template, stop_after_success=False, enabled=False,
                                                now=datetime(2020,1,1))
        schedules.run_now(self.source['id'], now=datetime(2020,1,1))
        with closing(schedules.connect()) as db, db:
            db.execute("UPDATE schedules SET generation=4,success_task_id='old-success',stopped_at='2020-01-01' WHERE id=?", (self.source['id'],))
        self.page.reload(); expect(self.page.locator('#plugin-branches option')).to_have_count(101)
        self.page.evaluate("nav('schedules')")
        expect(self.page.locator('#schedules-body')).to_contain_text('source')
        self.source = schedules.get_schedule(self.source['id'])

    def copy(self):
        self.page.locator('#schedules-body button', has_text='拷贝').first.click()
        expect(self.field('schedule_name')).to_have_value('source')

    def test_copy_cancel_repeat_click_and_save_new_identity(self):
        # Unsaved credentials from an unrelated draft must not leak into a copy.
        self.field('testfairy_password').fill('draft-secret', force=True)
        self.copy()
        self.assertEqual(self.field('testfairy_password').input_value(), '')
        self.assertEqual(self.field('country').input_value(), 'ALL')
        self.assertEqual(self.field('plugin_branch').input_value(), 'feature/source')
        self.assertTrue(self.field('schedule_stop_after_success').is_checked())
        self.assertFalse(self.field('schedule_enabled').is_checked())
        self.assertTrue(self.field('testfairy_enabled').is_checked())
        self.assertEqual(self.field('outlook_recipients').input_value(),'fixture@example.com')
        self.page.locator('#submit-build').click()
        expect(self.page.locator('#form-message')).to_contain_text('不同名称')
        self.assertEqual(len(schedules.list_schedules()),1)
        self.page.locator('#schedule-cancel').click()
        self.page.evaluate("nav('schedules')"); self.copy()
        self.page.evaluate("nav('schedules')"); self.copy()
        self.assertEqual(len(schedules.list_schedules()),1)
        self.field('schedule_name').fill('copy one')
        self.page.evaluate('scheduleEditor.submit(); scheduleEditor.submit()')
        expect(self.page.locator('#schedules-body')).to_contain_text('copy one')
        rows=schedules.list_schedules(); self.assertEqual(len(rows),2)
        new=next(r for r in rows if r['id']!=self.source['id'])
        # HTML number inputs serialize strings; compare the shared validated
        # build parameters, including derived folder and credential references.
        self.assertEqual(web.validate_build_parameters(json.loads(new['params']),web.load_config()),
                         web.validate_build_parameters(json.loads(self.source['params']),web.load_config()))
        self.assertEqual(new['generation'],1); self.assertIsNone(new['success_task_id']); self.assertIsNone(new['stopped_at'])
        self.assertEqual(schedules.list_runs(new['id']),[])
        self.assertIsNone(new['next_run']); self.assertFalse(new['enabled'])
        self.assertEqual(store.task_list(),[]); self.assertEqual(self.manual,[])
        self.assertEqual(schedules.get_schedule(self.source['id']),self.source)
        self.assertFalse(self.errors,self.errors)

    def test_new_reset_and_copy_default_on_edit_keeps_existing_off(self):
        self.page.locator('#schedules-body button',has_text='编辑').first.click()
        self.assertFalse(self.field('schedule_stop_after_success').is_checked())
        self.field('schedule_stop_after_success').check()
        self.page.locator('#schedule-cancel').click()
        self.page.evaluate("nav('schedules')")
        self.page.locator('#new-schedule').click()
        self.assertTrue(self.field('schedule_stop_after_success').is_checked())
        self.field('schedule_stop_after_success').uncheck()
        self.page.locator('#schedule-cancel').click()
        self.page.evaluate("nav('schedules')"); self.copy()
        self.assertTrue(self.field('schedule_stop_after_success').is_checked())
        self.assertEqual(len(schedules.list_schedules()),1)

    def test_copy_validates_cron_and_recomputes_future_time_on_save(self):
        self.copy(); self.field('schedule_name').fill('future copy'); self.field('schedule_enabled').check()
        self.field('schedule_cron').fill('* * * * *'); self.page.locator('#submit-build').click()
        expect(self.page.locator('#form-message')).to_contain_text('最小')
        self.assertEqual(len(schedules.list_schedules()),1)
        self.field('schedule_cron').fill('0 9 * * 1-5'); before=datetime.now()
        self.page.locator('#submit-build').click(); expect(self.page.locator('#schedules-body')).to_contain_text('future copy')
        new=next(r for r in schedules.list_schedules() if r['id']!=self.source['id'])
        self.assertGreater(datetime.fromisoformat(new['next_run']),before)
        self.assertEqual(schedules.list_runs(new['id']),[]); self.assertFalse(self.manual)

    def test_old_save_response_preserves_new_copy_editor(self):
        self.copy(); self.field('schedule_name').fill('first copy')
        entered=threading.Event(); release=threading.Event(); original=schedules.create_schedule
        def delayed(*args,**kw):
            entered.set()
            if not release.wait(8):raise RuntimeError('fixture response not released')
            return original(*args,**kw)
        with patch('appbuilder.schedules.create_schedule',side_effect=delayed):
            self.page.locator('#submit-build').click()
            self.assertTrue(entered.wait(3))
            try:
                self.page.evaluate("nav('schedules')"); self.copy(); self.field('schedule_name').fill('second draft')
            finally:release.set()
            expect(self.page.locator('#submit-build')).to_be_enabled()
        expect(self.field('schedule_name')).to_have_value('second draft')
        expect(self.page.locator('#build-form')).to_be_visible()
        self.assertEqual(len(schedules.list_schedules()),2)
        self.assertFalse(self.manual)

    def test_stale_edit_cannot_reenable_stopped_cycle(self):
        # Simulate a running enabled cycle, then success while its editor is open.
        schedules.update_schedule(self.source['id'],enabled=True,restart_cycle=True)
        self.page.reload(); self.page.evaluate("nav('schedules')")
        expect(self.page.locator('#schedules-body')).to_contain_text('source')
        self.page.locator('#schedules-body button',has_text='编辑').first.click()
        with closing(schedules.connect()) as db, db:
            db.execute("UPDATE schedules SET enabled=0,next_run=NULL,success_task_id='new-success',revision=revision+1 WHERE id=?", (self.source['id'],))
        self.field('schedule_name').fill('stale name'); self.page.locator('#submit-build').click()
        expect(self.page.locator('#form-message')).to_contain_text('计划已变化')
        self.assertFalse(schedules.get_schedule(self.source['id'])['enabled'])
        self.assertFalse(self.manual)


class CopyHeadlessTests(CopyScenarios, unittest.TestCase):
    headless=True

class CopyHeadedTests(CopyScenarios, unittest.TestCase):
    headless=False

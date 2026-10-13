"""Rollback defaults and previews in actual headless and headed Chromium."""
import unittest
from playwright.sync_api import expect
import test_shared_build_ui as shared


class ReleasePreviewScenario(shared.SharedBuildUIFixture):
    def test_release_defaults_and_preview_share_schedule_editor(self):
        self.assertTrue(self.field('plugin_parent').input_value().endswith('/job/release-build/job/'))
        expect(self.page.locator('#derived-plugin')).to_have_text('release/1.3.[timestamp]')
        self.field('plugin_version').fill('2.4')
        expect(self.page.locator('#derived-plugin')).to_have_text('release/2.4.[timestamp]')
        self.page.locator('#save-as-schedule').check()
        expect(self.page.locator('#schedule-settings')).to_be_visible()
        expect(self.page.locator('#derived-plugin')).to_have_text('release/2.4.[timestamp]')
        self.assertTrue(self.field('schedule_stop_after_success').is_checked())
        self.assertEqual(self.field('country').locator('option').count(), 25)
        self.assertFalse(self.manual)
        self.assertFalse(self.errors)


class ReleaseHeadlessTests(ReleasePreviewScenario, unittest.TestCase):
    headless = True


class ReleaseHeadedTests(ReleasePreviewScenario, unittest.TestCase):
    headless = False

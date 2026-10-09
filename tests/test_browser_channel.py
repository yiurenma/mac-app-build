import unittest
from appbuilder.jenkins import browser_launch_options, NeedsAttention


class TestBrowserChannel(unittest.TestCase):
    def test_managed_chrome(self):
        c = {'jenkins': {'browser_channel': 'chrome'}}
        self.assertEqual(browser_launch_options(c, headless=True), {'headless': True, 'channel': 'chrome'})

    def test_mirrored_chromium(self):
        c = {'jenkins': {'browser_channel': 'chromium'}}
        self.assertEqual(browser_launch_options(c, headless=False), {'headless': False})

    def test_invalid_channel(self):
        with self.assertRaises(NeedsAttention):
            browser_launch_options({'jenkins': {'browser_channel': 'safari'}}, headless=True)

import unittest
from unittest.mock import patch

import browser


class BrowserTests(unittest.TestCase):
    def test_open_port_reuses_existing_browser_without_launch(self):
        with patch.object(browser, 'port_open', return_value=True), patch.object(browser, 'launch') as launch:
            browser.ensure_browser()
            launch.assert_not_called()

    def test_closed_port_launches_existing_profile_once_and_waits(self):
        states = iter([False, False, True])
        with patch.object(browser, 'port_open', side_effect=lambda *a, **k: next(states)), \
             patch.object(browser, 'launch') as launch, patch.object(browser.time, 'sleep'):
            browser.ensure_browser()
            launch.assert_called_once()

    def test_launch_failure_gives_specific_guidance(self):
        with patch.object(browser, 'port_open', return_value=False), \
             patch.object(browser, 'launch', side_effect=RuntimeError()):
            with self.assertRaisesRegex(ValueError, 'setup'):
                browser.ensure_browser()

    def test_connect_timeout_gives_specific_guidance(self):
        with patch.object(browser, 'port_open', return_value=False), \
             patch.object(browser, 'launch'), patch.object(browser.time, 'sleep'), \
             patch.object(browser.time, 'time', side_effect=[0, 100]):
            with self.assertRaisesRegex(ValueError, '연결되지 않았습니다'):
                browser.ensure_browser()

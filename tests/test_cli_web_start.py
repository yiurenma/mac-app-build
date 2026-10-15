"""./app-build web lifts a stop pause by itself and explains a busy port in one line."""
import errno
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from appbuilder import cli, lifecycle


class WebStartTests(unittest.TestCase):
    def run_web(self, mode, serve_effect=None):
        with patch('appbuilder.handover.hand_over') as hand_over, \
                patch.object(cli, 'doctor_check'), patch.object(cli, 'load_config', return_value={}), \
                patch.object(lifecycle, 'state', return_value={'mode': mode}), \
                patch.object(lifecycle, 'resume') as resume, \
                patch('appbuilder.web.serve', side_effect=serve_effect) as serve:
            cli.main(['web', '--no-open'])
        hand_over.assert_called_once_with()
        return resume, serve

    def test_paused_project_is_resumed_before_serving(self):
        resume, serve = self.run_web('STOPPED')
        resume.assert_called_once_with()
        serve.assert_called_once()

    def test_running_project_is_not_touched(self):
        resume, serve = self.run_web('RUNNING')
        resume.assert_not_called()
        serve.assert_called_once()

    def test_busy_port_is_one_clear_line_not_a_traceback(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            self.run_web('RUNNING', OSError(errno.EADDRINUSE, 'Address already in use'))
        self.assertEqual(stop.exception.code, 1)
        self.assertIn('./app-build stop', err.getvalue())


class UpdateCommandTests(unittest.TestCase):
    def test_shell_wrapper_stops_then_starts_web_with_update(self):
        text = open('app-build', encoding='utf-8').read()
        block = text[text.index('if [ "$COMMAND" = update ]'):]
        self.assertLess(block.index('app-build" stop'), block.index('web --update'))


if __name__ == '__main__':
    unittest.main()

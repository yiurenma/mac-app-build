"""Regression for long-running Web Console: every SQLite connection is closed."""
import unittest
from unittest.mock import MagicMock, patch
from appbuilder import store


class ConnectionLifecycleTests(unittest.TestCase):
    def test_task_list_closes_connection_on_success(self):
        db=MagicMock()
        db.__enter__.return_value=db
        db.execute.return_value=[]
        with patch.object(store,'connect',return_value=db):
            self.assertEqual(store.task_list(),[])
        db.close.assert_called_once()

    def test_task_list_closes_connection_on_sql_error(self):
        db=MagicMock()
        db.__enter__.return_value=db
        db.execute.side_effect=RuntimeError('simulated db problem')
        with patch.object(store,'connect',return_value=db):
            with self.assertRaisesRegex(RuntimeError,'simulated'):
                store.task_list()
        db.close.assert_called_once()


if __name__=='__main__':unittest.main()

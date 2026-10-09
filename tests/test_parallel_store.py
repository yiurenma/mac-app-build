import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from appbuilder import store
from appbuilder.config import load_config


class TestSeparateTasks(unittest.TestCase):
    def test_unique_branches_and_parallel_limit(self):
        cfg = load_config()
        cfg['build']['max_parallel'] = 2
        params = {'plugin_version_input': '1.3', 'plugin_source': 'develop',
                  'gmx_source': 'develop', 'project': 'Demo', 'country': 'UK',
                  'plugin_url': cfg['github']['plugin_url'], 'gmx_url': cfg['github']['gmx_url']}
        with tempfile.TemporaryDirectory() as tmp, patch.object(store, 'DATA', Path(tmp)):
            a = store.create_task(cfg, params)
            b = store.create_task(cfg, params)
            self.assertNotEqual(a['id'], b['id'])
            self.assertNotEqual(a['stamp'], b['stamp'])
            self.assertNotEqual(a['plugin_branch'], b['plugin_branch'])
            self.assertNotEqual(a['gmx_branch'], b['gmx_branch'])
            self.assertTrue((Path(tmp) / 'runs' / a['id'] / 'state.json').exists())
            with self.assertRaises(RuntimeError):
                store.create_task(cfg, params)
            store.update_task(a['id'], status='SUCCESS')
            store.create_task(cfg, params)


if __name__ == '__main__':
    unittest.main()

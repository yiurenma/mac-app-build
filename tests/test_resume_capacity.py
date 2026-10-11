"""Resumes reserve the same ten slots atomically; history remains intact."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from appbuilder import store, web
from appbuilder.config import load_config, parallel_limit


class ResumeCapacityTests(unittest.TestCase):
    def test_resumes_race_for_one_slot_and_preserve_task_data(self):
        config=load_config()
        params=web.validate_build_parameters({'plugin_branch':'develop','gmx_branch':'develop',
            'plugin_version':'1.3','country':'UK','project_name':'PayByBank'},config)
        with tempfile.TemporaryDirectory() as temp,patch.object(store,'DATA',Path(temp)):
            ids=[]
            for i in range(11):
                row=store.create_task(config,params);ids.append(row['id'])
                store.update_task(row['id'],status='FAILED')
            for ident in ids[:9]:store.update_task(ident,status='RUNNING')
            original=store.task(ids[-1])['params']
            def resume(ident):
                try:store.reserve_resume(config,ident);return True
                except ValueError:return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(resume,ids[-2:]))
            self.assertEqual(sum(results),1)
            self.assertEqual(store.active_count(),10)
            self.assertEqual(store.task(ids[-1])['params'],original)
            self.assertEqual(len(store.task_list()),11)
            self.assertEqual(store.read_state(ids[-1]),{'completed':[],'runtime':{}})

    def test_limit_is_ten_even_if_old_configuration_requests_more(self):
        self.assertEqual(parallel_limit({}),10)
        self.assertEqual(parallel_limit({'build':{'max_parallel':50}}),10)
        self.assertEqual(parallel_limit({'build':{'max_parallel':2}}),2)

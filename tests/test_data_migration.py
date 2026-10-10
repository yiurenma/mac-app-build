import tempfile
import unittest
from pathlib import Path
from appbuilder.migrate import import_state

class ZIPReplacementTests(unittest.TestCase):
    def test_previous_state_copied_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as folder:
            old,new=(Path(folder)/'old',Path(folder)/'new')
            (old/'.appbuilder').mkdir(parents=True);new.mkdir()
            (old/'.appbuilder'/'state.sqlite').write_bytes(b'old sqlite bytes')
            copied=import_state(old,new)
            self.assertEqual((copied/'state.sqlite').read_bytes(),b'old sqlite bytes')
            self.assertTrue((old/'.appbuilder'/'state.sqlite').is_file())
    def test_never_overwrites_existing_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            old,new=(Path(folder)/'old',Path(folder)/'new')
            (old/'.appbuilder').mkdir(parents=True);(new/'.appbuilder').mkdir(parents=True)
            with self.assertRaises(ValueError):import_state(old,new)
    def test_old_folder_must_have_data(self):
        with tempfile.TemporaryDirectory() as folder:
            old,new=(Path(folder)/'old',Path(folder)/'new');old.mkdir();new.mkdir()
            with self.assertRaises(ValueError):import_state(old,new)

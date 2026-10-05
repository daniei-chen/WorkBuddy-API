import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('release_state',Path(__file__).parents[2]/'deploy/release_state.py')
state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)

@unittest.skipIf(os.name=='nt','Directory symlink and directory fsync contract is Linux-specific')
class AtomicReleaseContracts(unittest.TestCase):
    def test_failed_readiness_restores_pointer_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'old').mkdir();(root/'new').mkdir()
            database=root/'database';database.write_text('latest records and keys')
            pointer=root/'current';pointer.symlink_to('old',target_is_directory=True)
            actions=[];checks=iter([False,True])
            with self.assertRaisesRegex(RuntimeError,'candidate readiness failed'):
                state.activate(pointer,'new',lambda:actions.append('stop'),lambda:actions.append('start'),lambda:next(checks))
            self.assertEqual(os.readlink(pointer),'old')
            self.assertEqual(actions,['stop','start','stop','start'])
            self.assertEqual(database.read_text(),'latest records and keys')
    def test_success_and_unfinished_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'old').mkdir();(root/'new').mkdir()
            pointer=root/'current';pointer.symlink_to('old',target_is_directory=True)
            self.assertEqual(state.activate(pointer,'new',lambda:None,lambda:None,lambda:True),'old')
            (root/'current.next').symlink_to('old',target_is_directory=True)
            with self.assertRaises(RuntimeError):state.point(pointer,'old')

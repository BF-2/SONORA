"""Signal-contract and restricted-loading regression tests; no GPU inference."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from pitch.engine import apply_edits, resample_voiced
import numpy as np


class PitchContracts(unittest.TestCase):
    def test_unvoiced_gap_is_never_interpolated(self):
        out=resample_voiced([100,200,None,None,400,800],[0,1,2,3,4,5],[.5,1.5,2.5,3.5,4.5])
        np.testing.assert_allclose(out,[np.sqrt(20000),0,0,0,np.sqrt(320000)])

    def test_edit_uses_ticks_and_preserves_original(self):
        original={'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[100,None,100,100], 'voiced':[True,False,True,True]}
        result=apply_edits(original,[{'start_tick':960,'end_tick':1440,'operation':'transpose','semitones':12,'locked':True}])
        self.assertEqual(result['f0_hz'],[100,None,200,100])
        self.assertEqual(original['f0_hz'],[100,None,100,100])
        self.assertEqual(len(result['locked_regions']),1)

    def test_unsupported_control_is_rejected(self):
        data={'sample_rate':16000,'hop':160,'f0_hz':[100],'voiced':[True]}
        with self.assertRaises(ValueError):
            apply_edits(data,[{'start_tick':0,'end_tick':480,'operation':'invented_vibrato'}])

    def test_locked_snapshot_survives_new_estimator_and_global_transpose(self):
        original={'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[100,100,100], 'voiced':[True,True,True]}
        frozen={'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[220,None,220]}
        result=apply_edits(original,[{'start_tick':0,'end_tick':960,'operation':'lock','locked':True,'snapshot':frozen}],transpose=12)
        self.assertEqual(result['f0_hz'],[220,None,200])

    def test_note_override_can_preserve_unvoiced_consonants(self):
        original={'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':[100,None,100], 'voiced':[True,False,True]}
        result=apply_edits(original,[{'start_tick':0,'end_tick':1440,'operation':'set_hz','hz':220,'preserve_unvoiced':True}])
        self.assertEqual(result['f0_hz'],[220,None,220])


class SecurityContracts(unittest.TestCase):
    def test_malicious_pickle_rejected_without_execution(self):
        import torch
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            marker=Path(temp)/'executed.txt'; checkpoint=Path(temp)/'malicious.pth'
            class Payload:
                def __reduce__(self): return (os.system,(f'echo forbidden > "{marker}"',))
            torch.save({'weight':Payload()},checkpoint)
            result=subprocess.run([sys.executable,str(ROOT/'workers/rvc/probe_model.py'),str(checkpoint)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            response=json.loads(result.stdout)[0]
            self.assertFalse(response['ok'])
            self.assertRegex(response['error'].lower(),r'unsupported.*global')
            self.assertFalse(marker.exists())

    def test_explicit_unsafe_load_cannot_override_guard(self):
        from common import safe_torch
        torch=safe_torch()
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            marker=Path(temp)/'executed.txt'; checkpoint=Path(temp)/'malicious.pth'
            class Payload:
                def __reduce__(self): return (os.system,(f'echo forbidden > "{marker}"',))
            torch.save(Payload(),checkpoint)
            with self.assertRaises(Exception): torch.load(checkpoint,weights_only=False)
            self.assertFalse(marker.exists())


class FaissPathContracts(unittest.TestCase):
    def test_unicode_index_load_keeps_search_results(self):
        import faiss
        from faiss_io import enable_unicode_paths
        with tempfile.TemporaryDirectory(prefix='sonora-index-中文-') as directory:
            source=faiss.IndexFlatL2(8)
            vectors=np.arange(128,dtype=np.float32).reshape(16,8)
            source.add(vectors)
            path=Path(directory)/'配套模型.index'
            path.write_bytes(faiss.serialize_index(source).tobytes())
            enable_unicode_paths(faiss)
            reader=faiss.read_index
            enable_unicode_paths(faiss)
            self.assertIs(reader,faiss.read_index)
            loaded=faiss.read_index(str(path))
            expected=source.search(vectors[:3],2)
            actual=loaded.search(vectors[:3],2)
            np.testing.assert_array_equal(actual[1],expected[1])
            np.testing.assert_allclose(actual[0],expected[0])


if __name__=='__main__': unittest.main()

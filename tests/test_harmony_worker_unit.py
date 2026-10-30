"""Harmony role contracts and complementary PCM regression checks."""
import copy
import tempfile
import unittest
from pathlib import Path
import sys
import types
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
sys.path.insert(0,str(ROOT/'scripts'))
from separation.run import validate_semantics,harmony_demix
from prepare_harmony_model import registry_manifest,download,FILES


class HarmonyContracts(unittest.TestCase):
    def setUp(self):
        self.manifest=registry_manifest()
        self.config={'training':{'instruments':['Vocals','Instrumental'],'target_instrument':'Vocals'}}

    def test_explicit_mapping_is_required_even_for_vocals_label(self):
        general={'task':'vocals_instrumental','stem_roles':{'Vocals':'vocal','Instrumental':'instrumental'}}
        with self.assertRaisesRegex(ValueError,'different separation task'):
            validate_semantics(general,self.config,'lead_backing')

    def test_harmony_target_cannot_silently_swap(self):
        wrong=copy.deepcopy(self.manifest);wrong['native_stem_roles']={'Vocals':'backing_vocal','Instrumental':'lead_vocal'}
        with self.assertRaisesRegex(ValueError,'direct lead estimate'):
            validate_semantics(wrong,self.config,'lead_backing')

    def test_semantic_evidence_is_required(self):
        wrong=copy.deepcopy(self.manifest);wrong['semantic_sources']=[]
        with self.assertRaisesRegex(ValueError,'source-backed'):
            validate_semantics(wrong,self.config,'lead_backing')

    def test_original_task_mapping_is_unchanged(self):
        general={'stem_roles':{'Vocals':'vocal','Instrumental':'instrumental'}}
        self.assertEqual(validate_semantics(general,self.config,'vocals_instrumental')[0],general['stem_roles'])

    def test_float_decomposition_preserves_stereo_and_excess_peaks(self):
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);source=np.column_stack([np.linspace(-1.2,1.2,4410),np.linspace(.9,-.9,4410)]).astype('float32')
            sf.write(p/'source.wav',source,44100,subtype='FLOAT')
            instance=types.SimpleNamespace(segment_size=64,demix=lambda mix:{'Vocals':mix*.4,'Instrumental':mix*.6})
            outputs,metadata=harmony_demix(types.SimpleNamespace(model_instance=instance),{'input_path':str(p/'source.wav'),'output_dir':str(p)},self.manifest,'Vocals')
            lead,sr=sf.read(outputs[0],dtype='float32',always_2d=True);backing,_=sf.read(outputs[1],dtype='float32',always_2d=True)
            self.assertEqual(lead.shape,source.shape);self.assertEqual(sr,44100)
            np.testing.assert_allclose(lead+backing,source,rtol=0,atol=1.2e-7)
            self.assertFalse(metadata['independent_stem_normalization'])

    def test_cancel_marker_is_checked_before_network(self):
        # A distinct synthetic filename avoids consulting an existing model.
        entry={'name':'unit-cancel-only.ckpt','bytes':1,'sha256':'0'*64}
        with tempfile.TemporaryDirectory() as directory:
            cancel=Path(directory)/'cancel';cancel.write_text('cancel')
            with self.assertRaises(KeyboardInterrupt):download(entry,cancel)


if __name__=='__main__':unittest.main()

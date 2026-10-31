"""Targeted parameter, capability, and pure F0 transformation regression tests."""
from pathlib import Path
import copy
import importlib.util
import json
import math
import sys
import unittest
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workers'))
from rvc_parameters import validate_rvc_parameters,parameter_catalog,parameter_defaults,pitch_analysis_parameters,parameter_hash,load_parameter_schema,ParameterValidationError
from pitch.engine import apply_edits,apply_target_parameters,contour_hash,shift_pitch_to_project,resample_voiced


def manifest():
    return {'id':'verified-example','speaker_ids_trained':[0],'speaker_ids':[0],'speaker_metadata_status':'checkpoint',
            'embedding_speaker_capacity':109,'embedder_model':'contentvec','embedder_verified':True,'content_dim':768,
            'index_id':'index-example','index_path':'registered-only.index','index_hash':'a'*64,
            'index_match':{'status':'verified','evidence':['named_training_run'],'content_dim':768,'embedder_model':'contentvec'},'f0_conditioned':True}


def curve(values):
    return {'sample_rate':16000,'hop':160,'frame_center_offset':0,'f0_hz':values,'voiced':[v is not None for v in values],
            'curve_layer':'detected','estimator':'rmvpe','source_hash':'b'*64}


class ParameterContracts(unittest.TestCase):
    def test_schema_has_help_mapping_and_default_for_every_control(self):
        for key,item in load_parameter_schema()['properties'].items():
            self.assertIn('default',item)
            for field in ('id','label','label_en','unit','group','help','risk','recommendation','dependencies','native_mapping','recompute_scope','support_reason'):
                self.assertIn(field,item['x-sonora'],(key,field))

    def test_defaults_and_boundary_values(self):
        checked=validate_rvc_parameters({'transpose':36,'protect':.5,'volume_envelope':0},manifest())
        self.assertEqual(checked['accepted_parameters']['transpose'],36)
        self.assertEqual(parameter_defaults()['volume_envelope'],1)
        self.assertEqual(parameter_defaults()['protect'],.33)

    def test_finite_numeric_and_type_validation(self):
        for values in [{'index_rate':True},{'transpose':float('nan')},{'seed':1.5},{'protect':-.1},{'volume_envelope':1.1},{'transpose':37}]:
            with self.assertRaises(ParameterValidationError):validate_rvc_parameters(values)

    def test_unknown_and_unsupported_cannot_succeed(self):
        for values in [{'conversion_strength':.5},{'formant_shifting':True},{'proposed_pitch':True},{'split_audio':True},{'crossfade_ms':10},{'precision':'float16'},{'hop_length':320}]:
            with self.assertRaises(ParameterValidationError):validate_rvc_parameters(values)
        checked=validate_rvc_parameters({'formant_shifting':False})
        self.assertIn('formant_shifting',checked['inactive_parameters'])
        self.assertNotIn('formant_shifting',checked['effective_parameters'])

    def test_109_embedding_rows_do_not_create_109_speakers(self):
        with self.assertRaisesRegex(ParameterValidationError,'未训练'):validate_rvc_parameters({'speaker_id':1},manifest())
        catalog=parameter_catalog(manifest())
        row=next(x for x in catalog['parameters'] if x['id']=='speaker_id')
        self.assertEqual(row['enum'],[0]);self.assertTrue(row['read_only'])

    def test_index_requires_match_not_folder_guess(self):
        model=manifest();model['index_match']['status']='unknown'
        with self.assertRaisesRegex(ParameterValidationError,'索引未验证'):validate_rvc_parameters({'index_rate':.5},model)
        validate_rvc_parameters({'index_rate':0},model)
        model=manifest();model['index_match']['content_dim']=256
        with self.assertRaisesRegex(ParameterValidationError,'维度'):validate_rvc_parameters({'index_rate':.5},model)
        with self.assertRaisesRegex(ParameterValidationError,'所选索引'):validate_rvc_parameters({'index_rate':.5,'index_id':'wrong'},manifest())

    def test_unknown_encoder_is_not_assumed_from_dimension(self):
        model=manifest();model['embedder_verified']=False
        with self.assertRaisesRegex(ParameterValidationError,'编码器未验证'):validate_rvc_parameters({},model)

    def test_protect_noop_and_volume_direction_are_explicit(self):
        result=validate_rvc_parameters({'index_rate':0,'protect':0},manifest())
        self.assertEqual(result['inactive_parameters']['protect']['code'],'no_op_condition')
        rows={x['id']:x for x in parameter_catalog(manifest(),{'index_rate':0})['parameters']}
        self.assertIn('0.5为关闭',rows['protect']['help'])
        self.assertIn('0偏向原始',rows['volume_envelope']['help'])
        self.assertFalse(rows['protect']['supported'])

    def test_algorithm_thresholds_and_unknown_native_score(self):
        self.assertEqual(pitch_analysis_parameters({'pitch_algorithm':'fcpe'})['threshold'],.006)
        self.assertIsNone(pitch_analysis_parameters({'pitch_algorithm':'harvest'})['threshold'])
        with self.assertRaisesRegex(ParameterValidationError,'没有可比较'):pitch_analysis_parameters({'pitch_algorithm':'dio','threshold':.2})

    def test_context_is_not_effective_inference(self):
        checked=validate_rvc_parameters({'model_id':'legacy','voice_part':'backing_vocal','name':'output','sample_rate':48000})
        self.assertIn('voice_part',checked['context_parameters'])
        self.assertNotIn('name',checked['effective_parameters'])
        self.assertNotIn('sample_rate',checked['effective_parameters'])
        signed=validate_rvc_parameters({'source_timeline_offset_ticks':-48000})
        self.assertEqual(signed['context_parameters']['source_timeline_offset_ticks'],-48000)

    def test_raw_analysis_hash_ignores_model_and_target_controls(self):
        a=pitch_analysis_parameters({'pitch_algorithm':'rmvpe'})
        b=pitch_analysis_parameters({'pitch_algorithm':'rmvpe','model_id':'another','index_rate':.8,'transpose':12,'filter_radius':3,'f0_autotune':True})
        self.assertEqual(parameter_hash(a),parameter_hash(b))
        changed=pitch_analysis_parameters({'pitch_algorithm':'rmvpe','f0_min':80})
        self.assertNotEqual(parameter_hash(a),parameter_hash(changed))

    def test_clean_audio_missing_dependency_is_blocked(self):
        with self.assertRaisesRegex(ParameterValidationError,'noisereduce'):validate_rvc_parameters({'clean_audio':True},manifest(),clean_audio_available=False)
        checked=validate_rvc_parameters({'clean_audio':True,'clean_strength':.5},manifest(),clean_audio_available=True)
        self.assertTrue(checked['effective_parameters']['clean_audio'])


class TargetF0Contracts(unittest.TestCase):
    def test_smoothing_does_not_cross_unvoiced_boundaries(self):
        raw=curve([100,100,900,100,100,None,400,400,20,400,400]);before=copy.deepcopy(raw)
        target=apply_target_parameters(raw,{'filter_radius':1})
        self.assertAlmostEqual(target['f0_hz'][2],100)
        self.assertIsNone(target['f0_hz'][5]);self.assertAlmostEqual(target['f0_hz'][8],400)
        self.assertEqual(raw,before)

    def test_autotune_is_cents_blend_and_keeps_unvoiced(self):
        raw=curve([440*2**(.3/12),None])
        half=apply_target_parameters(raw,{'f0_autotune':True,'f0_autotune_strength':.5})
        self.assertAlmostEqual(half['f0_hz'][0],440*2**(.15/12),places=8)
        self.assertIsNone(half['f0_hz'][1])

    def test_declared_order_manual_then_absolute_lock(self):
        raw=curve([450,900,450,None,220])
        snapshot=curve([500,None,None,None,None])
        edits=[{'start_tick':0,'end_tick':480,'locked':True,'operation':'lock','snapshot':snapshot},
               {'start_tick':0,'end_tick':1440,'operation':'set_hz','hz':330}]
        target=apply_target_parameters(raw,{'filter_radius':1,'f0_autotune':True,'f0_autotune_strength':1,'transpose':12,'pitch_edits':edits})
        self.assertEqual(target['f0_hz'][:3],[500,330,330])
        self.assertIsNone(target['f0_hz'][3]);self.assertAlmostEqual(target['f0_hz'][4],440)
        self.assertEqual(target['target_processing_order'][-1],'locked_absolute_snapshots')

    def test_target_cannot_be_applied_twice(self):
        target=apply_target_parameters(curve([220,None,220]),{'transpose':12})
        with self.assertRaisesRegex(ValueError,'twice'):apply_target_parameters(target,{'transpose':12})

    def test_locked_snapshot_survives_all_auto_controls(self):
        raw=curve([225,230,None,240])
        locked={'start_tick':0,'end_tick':1920,'operation':'lock','locked':True,'snapshot':curve([220,221,None,222])}
        target=apply_target_parameters(raw,{'filter_radius':7,'f0_autotune':True,'f0_autotune_strength':1,'transpose':12,'pitch_edits':[locked]})
        self.assertEqual(target['f0_hz'],[220,221,None,222])

    def test_timeline_fraction_is_not_rounded_twice(self):
        shifted=shift_pitch_to_project(curve([220,220]),1)
        self.assertEqual(shifted['frame_center_offset_rational'],[1,3])
        target=apply_target_parameters(shifted,{})
        self.assertEqual(target['frame_center_offset'],shifted['frame_center_offset'])

    def test_long_timeline_single_voiced_frame_does_not_fill_neighbours(self):
        result=resample_voiced([220,None],[1_000_000_000,1_000_000_480],[1_000_000_000,1_000_000_240,1_000_000_480])
        self.assertEqual(result.tolist(),[220,0,0])

    def test_inactive_strength_does_not_change_target_fingerprint(self):
        raw=curve([225,230,None])
        a=apply_target_parameters(raw,{'f0_autotune':False,'f0_autotune_strength':0})
        b=apply_target_parameters(raw,{'f0_autotune':False,'f0_autotune_strength':1})
        self.assertEqual(a['target_parameters_hash'],b['target_parameters_hash'])
        self.assertEqual(a['target_hash'],b['target_hash'])


class CleanAudioCPU(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('noisereduce'),'noisereduce is not in this test interpreter')
    def test_existing_noisereduce_is_a_real_length_preserving_dsp(self):
        import numpy as np
        import noisereduce as nr
        sr=16000;t=np.arange(sr)/sr;rng=np.random.default_rng(21)
        source=(.1*np.sin(2*np.pi*220*t)+.03*rng.standard_normal(sr)).astype('float32')
        output=nr.reduce_noise(y=source,sr=sr,prop_decrease=.5,stationary=False,use_torch=False,n_jobs=1)
        self.assertEqual(len(output),len(source));self.assertTrue(np.isfinite(output).all())
        self.assertGreater(float(np.sqrt(np.mean((output-source)**2))),1e-7)


if __name__=='__main__':unittest.main()

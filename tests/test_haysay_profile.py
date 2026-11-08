from services.orchestrator.model_catalog import trained_metadata
def test_hash_pinned_inference_profile_limits_speakers():
    raw={'embedding_speaker_capacity':109,'model_hash':'a'*64,'f0_conditioned':True}
    run={'model_info':{'inference_profile':{'model_sha256':'a'*64,'status':'verified_local_inference','supported_speaker_ids':[0],'embedder_model':'contentvec'}},'metadata_path':'model_info.json'}
    m=trained_metadata(raw,run)
    assert m['speaker_ids']==[0] and m['embedding_speaker_capacity']==109
    assert m['speaker_metadata_status']=='verified_inference_profile' and m['embedder_verified']
    assert m['embedder_evidence']['source']=='hash_pinned_inference_profile'
    from workers.rvc_parameters import _trained_speakers
    m['inference_profile']=run['model_info']['inference_profile']
    assert _trained_speakers(m)==[0]
    m['inference_profile']={**m['inference_profile'],'model_sha256':'b'*64}
    assert _trained_speakers(m)==[]
def test_inference_profile_cannot_bypass_hash_or_capacity():
    raw={'embedding_speaker_capacity':109,'model_hash':'a'*64}
    profile={'model_sha256':'b'*64,'status':'verified_local_inference','supported_speaker_ids':[0],'embedder_model':'contentvec'}
    assert trained_metadata(raw,{'model_info':{'inference_profile':profile}})['speaker_ids']==[]
    profile.update(model_sha256='a'*64,supported_speaker_ids=[109])
    assert trained_metadata(raw,{'model_info':{'inference_profile':profile}})['speaker_ids']==[]

from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import worker_main, resource_config, write_json, environment, ADAPTER_VERSION
from pitch.engine import analyze, apply_edits


def probe():
    import importlib.util
    result=environment('pitch')
    algorithms=['rmvpe','fcpe','crepe','crepe-tiny','pyin']
    if importlib.util.find_spec('pyworld'): algorithms+=['harvest','dio']
    result['capabilities']={'algorithms':algorithms,'separate_analysis':True,'native_scores':True,'cancel_mode':'terminate_process'}
    return result


def render(request):
    config=resource_config(request); params=request.get('parameters',{})
    detected=analyze(request['input_path'],config,params)
    target_basis=dict(detected)
    target_basis['frame_center_offset']=detected.get('frame_center_offset',0)+round(int(params.get('source_timeline_offset_ticks',0))*detected['sample_rate']/48000)
    target=apply_edits(target_basis,params.get('pitch_edits',[]),params.get('transpose',0))
    target['time_reference']='project_timeline'
    # Raw detection is authoritative input; target layer is explicitly separate.
    # RVC applies the immutable job edit snapshot once, never on an already shifted curve.
    result={**detected,'target_layer':target}
    output=Path(request['output_dir'])/'pitch.json'
    write_json(output,result)
    original=Path(request['output_dir'])/'pitch-detected.json'
    write_json(original,detected)
    return {'outputs':[{'path':str(output),'role':'pitch','media_type':'application/json','processing_kind':'model_inference' if detected['model_hash'] else 'dsp',
                        'sample_rate':16000,'sample_count':detected['sample_count'],'channels':1,'frame_hop':160,'frame_center_offset':0,
                        'engine':detected['estimator'],'model_hash':detected['model_hash'],'worker':ADAPTER_VERSION,
                        'valid_range':[0,detected['sample_count']], 'time_map':detected['time_map']}],
            'metadata':{'algorithm':detected['estimator'],'score_kind':detected['score_kind'],'detected_path':str(original),'target_hash':target['target_hash']},'warnings':[]}


if __name__=='__main__': raise SystemExit(worker_main('pitch',render,probe))

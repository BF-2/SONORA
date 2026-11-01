"""Independent lead/backing bindings and audition/mix selection rules."""
from __future__ import annotations
import copy

VOICE_PARTS=('lead_vocal','backing_vocal')
PART_NAMES={'lead_vocal':'主唱','backing_vocal':'和声'}
PART_DEFAULTS={'pitch_algorithm':'rmvpe','transpose':0,'index_rate':0,'protect':0.33}

def voice_part(value):
    part=value.get('voice_part')
    if part in VOICE_PARTS:return part
    return value.get('stem_role',value.get('role')) if value.get('stem_role',value.get('role')) in VOICE_PARTS else None

def new_binding(artifact,previous=None):
    binding=copy.deepcopy(previous or {})
    same=binding.get('source_artifact_id')==artifact['id']
    parameters={**PART_DEFAULTS,**binding.get('parameters',{})}
    if not same:
        parameters.pop('pitch_artifact_id',None)
        parameters.pop('accepted_preview_id',None)
    return {'source_artifact_id':artifact['id'],'mode':binding.get('mode','convert'),
            'parameters':parameters,
            'pitch_edits':binding.get('pitch_edits',[]) if same else [],
            'notes':binding.get('notes',[]) if same else [],
            'selected_candidate_id':binding.get('selected_candidate_id') if same else None}

def part_track_ids(project,part):
    return {t['id'] for t in project['tracks'] if voice_part(t)==part}

def sync_part_tracks(project,parts=None):
    """Choose exactly one full source/candidate per group without altering other groups."""
    bindings=project.get('voice_parts',{})
    for part in parts or bindings:
        if part not in VOICE_PARTS or part not in bindings:continue
        binding=bindings[part];selected=binding.get('source_artifact_id')
        if binding['mode']=='convert' and binding.get('selected_candidate_id'):
            candidate=next((c for c in project['candidates'] if c['id']==binding['selected_candidate_id']),None)
            if candidate and candidate.get('voice_part')==part:selected=candidate['artifact_id']
        matching=[t for t in project['tracks'] if voice_part(t)==part and t['artifact_id']==selected]
        preferred=[t for t in matching if binding.get('selected_candidate_id') and t.get('candidate_id')==binding['selected_candidate_id']]
        selected_track=(preferred or matching)[-1]['id'] if matching else None
        for candidate in project['candidates']:
            if candidate.get('voice_part')==part:candidate['selected']=binding['mode']=='convert' and candidate['id']==binding.get('selected_candidate_id')
        for track in project['tracks']:
            if voice_part(track)==part:track['mute']=binding['mode']=='mute' or track['id']!=selected_track
    if bindings:
        for track in project['tracks']:
            if not voice_part(track) and track['role'] in ('original','vocal','guide','synthesized','converted','mix'):
                track['mute']=True

def resolve_part(project,part,overrides=None):
    if part not in VOICE_PARTS:raise ValueError('未知的声部。')
    binding=project.get('voice_parts',{}).get(part)
    if not binding:raise ValueError(f'请先分离或导入{PART_NAMES[part]}轨。')
    source=next((a for a in project['assets'] if a['id']==binding['source_artifact_id']),None)
    if not source or source.get('stem_role')!=part:raise ValueError('声部绑定源不存在或角色不匹配。')
    # Only common execution choices cross scopes. Model/F0/edit values never inherit
    # the legacy combined-vocal workspace.
    common={k:project['parameters'][k] for k in ('device','seed','cpu_threads') if k in project['parameters']}
    parameters={**common,**PART_DEFAULTS,**copy.deepcopy(binding.get('parameters',{})),**copy.deepcopy(overrides or {})}
    parameters['voice_part']=part
    parameters['pitch_edits']=copy.deepcopy((overrides or {}).get('pitch_edits',binding.get('pitch_edits',[])))
    return binding,source,parameters

def part_unchanged(current,snapshot,part):
    if current.get('voice_parts',{}).get(part)!=snapshot.get('voice_parts',{}).get(part):return False
    before={t['id']:t for t in snapshot['tracks'] if voice_part(t)==part}
    after={t['id']:t for t in current['tracks'] if t['id'] in before}
    return before==after

def remap_parts(parts,mapping,candidate_ids):
    """Keep portable bindings, but never silently attach an unresolved audio ID."""
    result={}
    for part,b in (parts or {}).items():
        if part not in VOICE_PARTS or b.get('source_artifact_id') not in mapping:continue
        result[part]={**copy.deepcopy(b),'source_artifact_id':mapping[b['source_artifact_id']]}
        if b.get('selected_candidate_id') not in candidate_ids:result[part]['selected_candidate_id']=None
    return result

def invalidate_mix(project,reason):
    roles={a['id']:a.get('stem_role') for a in project['assets']}
    for node in project['graph']:
        if node['type'] in ('mix','export','export_stems') or any(roles.get(ref) in ('mix','stem_export') for ref in node.get('artifact_refs',[])):
            node.update(state='stale',invalidation_scope='mix_and_export',reason=reason)
    project['warnings']=[w for w in project['warnings'] if w.get('code')!='pending_mix']
    project['warnings'].append({'code':'pending_mix','message':reason+'；请重新混音后导出当前版本。'})

def mix_signature(project):
    enabled=[t for t in project['tracks'] if not t.get('mute') and t['role']!='mix']
    if any(t.get('solo') for t in enabled):enabled=[t for t in enabled if t.get('solo')]
    return enabled

def covers_source(output,source,offset_ticks=0):
    from fractions import Fraction
    if int(output.get('timeline_offset_ticks',offset_ticks))!=int(offset_ticks):return False
    duration=Fraction(output['sample_count'],output['sample_rate'])
    original=Fraction(source['sample_count'],source['sample_rate'])
    return abs(duration-original)<=Fraction(1,min(output['sample_rate'],source['sample_rate']))

"""Standard production workflow, layered over existing artifacts and workers.

Source bytes are immutable. Pitch versions own edits; candidates own frozen
generation recipes; final entries reference accepted candidates and mixer tracks.
"""
from __future__ import annotations
import copy
from .store import uid, now
from .config import TIMEBASE
from .voices import VOICE_PARTS, invalidate_mix
from workers.rvc_parameters import F0_TARGET_KEYS, F0_ANALYSIS_KEYS
from workers.pitch.voicing import normalize_voicing_settings

LOCKED_ROLES = {'original', 'vocal', 'instrumental', 'lead_vocal', 'backing_vocal', 'guide', 'synthesized'}
PITCH_KEYS = set(F0_TARGET_KEYS) | (set(F0_ANALYSIS_KEYS) - {'device'}) | {'pitch_artifact_id', 'voicing_settings'}
COMMON_KEYS = {'device', 'seed', 'cpu_threads'}
# Routing/preferences belong to the project; everything else belongs to an input.
PROJECT_KEYS = COMMON_KEYS | {'active_voice_part', 'source_artifact_id', 'task_confirmed', 'import_role',
    'skip_separation', 'separation_model_id', 'harmony_model_id', 'separation_settings', 'harmony_settings',
    'harmony_source_artifact_id', 'timeline_duration_ticks'}
DEFAULTS = {'pitch_algorithm': 'rmvpe', 'transpose': 0, 'index_rate': 0, 'protect': .33}


def enabled(project):
    return project.get('production_version') == 1


def source_context(project, source_id=None):
    if source_id:
        for binding in project.get('voice_parts', {}).values():
            if binding['source_artifact_id'] == source_id:
                return source_id, binding
        if source_id == project['parameters'].get('source_artifact_id'):
            return source_id, project
        return source_id, project.setdefault('input_workspaces', {}).setdefault(source_id,
            {'parameters': copy.deepcopy(DEFAULTS), 'pitch_edits': [], 'notes': []})
    part = project['parameters'].get('active_voice_part')
    binding = project.get('voice_parts', {}).get(part)
    if binding:
        return binding['source_artifact_id'], binding
    chosen = project['parameters'].get('source_artifact_id')
    if not chosen:
        roles = ('guide', 'synthesized') if project['task'] == 'rewrite' else ('vocal', 'guide', 'synthesized', 'original')
        chosen = next((a['id'] for role in roles for a in reversed(project['assets']) if a.get('stem_role') == role), None)
    return chosen, project


def curve_state(context):
    return {key: copy.deepcopy(context.get(key, [])) for key in ('pitch_edits', 'notes')} | {
        'parameters': {k: copy.deepcopy(v) for k, v in context.get('parameters', {}).items() if k in PITCH_KEYS}}


def active_version(project, source_id):
    identifier = project.get('active_pitch_versions', {}).get(source_id)
    return next((v for v in project.get('pitch_versions', []) if v['id'] == identifier and v['source_artifact_id'] == source_id), None)


def ensure_version(project, source_id, context):
    version = active_version(project, source_id)
    if version is None:
        version = {'id': uid('f0'), 'source_artifact_id': source_id, 'name': 'Edit 1',
                   'revision': 1, 'created_at': now(), 'updated_at': now(), 'undo': [], 'redo': [], **curve_state(context)}
        project.setdefault('pitch_versions', []).append(version)
        project.setdefault('active_pitch_versions', {})[source_id] = version['id']
    return version


def save_versions(project):
    if not enabled(project):
        return
    contexts = [(b['source_artifact_id'], b) for b in project.get('voice_parts', {}).values()]
    source_id = project['parameters'].get('source_artifact_id')
    if source_id and source_id not in {item[0] for item in contexts}:
        contexts.append((source_id, project))
    for source_id, context in contexts:
        view=context
        if context is project:
            from .model_selection import resolve_binding
            parameters,_=resolve_binding(project,{**context['parameters'],'ignore_clip_bindings':True},source_id)
            view={**context,'parameters':parameters}
        version = ensure_version(project, source_id, view)
        value = curve_state(view)
        previous = curve_state(version)
        if previous != value:
            version['undo'] = (version.get('undo', []) + [previous])[-100:]
            version.update(value, revision=version['revision'] + 1, updated_at=now(), redo=[])


def lock_sources(project):
    for asset in project['assets']:
        if asset.get('stem_role') in LOCKED_ROLES:
            asset['locked'] = True
    locked = {a['id'] for a in project['assets'] if a.get('locked')}
    for track in project['tracks']:
        if track['artifact_id'] in locked:
            track['locked'] = True


def initialize(project):
    """Idempotent migration. Historical auto-selected results require review again."""
    if enabled(project):
        lock_sources(project)
        return
    project.update(production_version=1, pitch_versions=[], active_pitch_versions={}, final_list=[], input_workspaces={})
    project.setdefault('view_preferences', {})['auto_archive_candidates'] = False
    source_id, _ = source_context(project)
    if source_id:
        project['parameters']['source_artifact_id'] = source_id
    for candidate in project['candidates']:
        candidate.update(review_status='pending', selected=False)
    project['selected_candidate_id'] = None
    for binding in project.get('voice_parts', {}).values():
        binding['selected_candidate_id'] = None
        binding['mode'] = 'convert'
    for track in project['tracks']:
        if track['role'] != 'instrumental':
            track['mute'] = True
    project['background_track_id'] = next((t['id'] for t in reversed(project['tracks']) if t['role'] == 'instrumental' and not t.get('mute')), None)
    lock_sources(project)
    save_versions(project)
    if project['candidates'] or any(a.get('stem_role')=='mix' for a in project['assets']):
        invalidate_mix(project,'旧工程已迁入人工验收流程，历史候选保留待确认')


def synchronize(project):
    """After worker/import commits, protect sources and keep one Background."""
    if not enabled(project):
        return
    lock_sources(project)
    for candidate in project['candidates']:
        candidate.setdefault('review_status','pending')
    backgrounds = [t for t in project['tracks'] if t['role'] == 'instrumental']
    selected_run = next((r for r in project.get('separation_runs', []) if r['id'] == project.get('selected_separations', {}).get('separate')), None)
    if selected_run:
        track = next((t for t in backgrounds if t['artifact_id'] in selected_run['artifact_ids']), None)
        if track:
            project['background_track_id'] = track['id']
    elif not any(t['id'] == project.get('background_track_id') for t in backgrounds):
        project['background_track_id'] = backgrounds[-1]['id'] if backgrounds else None
    for track in backgrounds:
        if track['id'] != project.get('background_track_id'):
            track.update(mute=True, solo=False)
    source_id, _ = source_context(project)
    if source_id and not project['parameters'].get('source_artifact_id'):
        project['parameters']['source_artifact_id'] = source_id
    save_versions(project)


def switch_source(project, source_id):
    source = next((a for a in project['assets'] if a['id'] == source_id), None)
    if not source or source.get('stem_role') not in LOCKED_ROLES - {'instrumental'}:
        raise ValueError('请选择当前工程的原始人声输入。')
    save_versions(project)
    old = project['parameters'].get('source_artifact_id')
    if old:
        project['input_workspaces'][old] = {key: copy.deepcopy(project[key]) for key in ('pitch_edits', 'notes')} | {
            'parameters': {k: copy.deepcopy(v) for k, v in project['parameters'].items() if k not in PROJECT_KEYS}}
    saved = project['input_workspaces'].get(source_id, {'parameters': DEFAULTS, 'pitch_edits': [], 'notes': []})
    project['parameters'] = {k: v for k, v in project['parameters'].items() if k in PROJECT_KEYS} | copy.deepcopy(saved['parameters']) | {
        'source_artifact_id': source_id, 'track_id': None, 'clip_id': None, 'pitch_artifact_id': saved['parameters'].get('pitch_artifact_id')}
    for key in ('pitch_edits', 'notes'):
        project[key] = copy.deepcopy(saved[key])
    ensure_version(project, source_id, project)


def pitch_action(project, action, source_id, version_id=None, name=None):
    initialize(project)
    if source_id not in {a['id'] for a in project['assets'] if a.get('stem_role') in LOCKED_ROLES - {'instrumental'}}:
        raise ValueError('音高版本必须属于当前工程的人声源。')
    save_versions(project)
    _, context = source_context(project, source_id)
    current = ensure_version(project, source_id, context)
    version = next((v for v in project['pitch_versions'] if v['id'] == version_id and v['source_artifact_id'] == source_id), None)
    if action == 'create':
        count = sum(v['source_artifact_id'] == source_id for v in project['pitch_versions']) + 1
        version = {**copy.deepcopy(current), 'id': uid('f0'), 'name': f'Edit {count}', 'revision': 1,
                   'created_at': now(), 'updated_at': now(), 'undo': [], 'redo': []}
        project['pitch_versions'].append(version)
    elif version is None:
        raise ValueError('音高版本不属于当前输入，不能跨轨道使用。')
    if action == 'rename':
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError('版本名称需要 1–120 个字符。')
        version['name'] = name.strip()
        return
    if action not in ('create', 'select', 'save', 'undo', 'redo'):
        raise ValueError('未知音高版本操作。')
    if action in ('undo', 'redo'):
        stack = version.get(action, [])
        if stack:
            inverse = 'redo' if action == 'undo' else 'undo'
            version.setdefault(inverse, []).append(curve_state(version))
            value = stack.pop()
            version.update(value, revision=version['revision'] + 1, updated_at=now())
    project['active_pitch_versions'][source_id] = version['id']
    for key in ('pitch_edits', 'notes'):
        context[key] = copy.deepcopy(version[key])
    context['parameters'] = {k: v for k, v in context['parameters'].items() if k not in PITCH_KEYS} | copy.deepcopy(version['parameters'])
    if context is project:
        for track in project['tracks']:
            binding=project.get('model_bindings',{}).get('tracks',{}).get(track['id'])
            if track['artifact_id']==source_id and binding:
                binding['parameters']={k:v for k,v in binding.get('parameters',{}).items() if k not in PITCH_KEYS} | copy.deepcopy(version['parameters'])


def bind_pitch(project, source_id, artifact, snapshot):
    """Attach completed detection to the version that requested it, never new focus."""
    if not enabled(project):
        return
    version_id = snapshot.get('active_pitch_versions', {}).get(source_id)
    version = next((v for v in project['pitch_versions'] if v['id'] == version_id), None)
    if not version:
        return
    before = active_version(snapshot, source_id)
    if before and version['revision'] != before['revision']:
        return  # Analysis still exists in assets; concurrent edits remain intact.
    version['parameters']['pitch_artifact_id'] = artifact['id']
    version['updated_at'] = now()
    if active_version(project, source_id) is version:
        _, context = source_context(project, source_id)
        context['parameters']['pitch_artifact_id'] = artifact['id']


def pin_generation(project, source_id, parameters):
    version = active_version(project, source_id)
    if not version:
        raise ValueError('请先选择输入并保存音高版本。')
    if parameters.get('f0_version_id') not in (None, version['id']):
        raise ValueError('所选音高版本与当前输入不匹配，请刷新后重试。')
    from .editing import note_edits
    parameters['pitch_edits']=note_edits(version['notes'])+copy.deepcopy(version['pitch_edits'])
    parameters['voicing_settings'] = normalize_voicing_settings(version['parameters'].get('voicing_settings'))
    parameters.update(f0_version_id=version['id'], f0_version_revision=version['revision'],
                      f0_version_snapshot={k: copy.deepcopy(v) for k, v in version.items() if k not in ('undo', 'redo')}, adopt_result=False)
    parameters['input_edit_snapshot']={'parameters':{k:copy.deepcopy(v) for k,v in parameters.items() if k in PITCH_KEYS},
                                       'pitch_edits':copy.deepcopy(parameters['pitch_edits'])}


def accept(project, candidate_id):
    candidate = next((c for c in project['candidates'] if c['id'] == candidate_id), None)
    if not candidate or candidate.get('review_status') == 'discarded' or candidate.get('archived'):
        raise ValueError('候选不存在或已丢弃。')
    if candidate.get('preview_only') or candidate.get('state') != 'succeeded':
        raise ValueError('只有成功的完整候选可以接受为成品。')
    from .media import validate_artifact
    output = next(a for a in project['assets'] if a['id'] == candidate['artifact_id'])
    validate_artifact(output)
    if any(f['candidate_id'] == candidate_id for f in project['final_list']):
        return
    track = next((t for t in project['tracks'] if t.get('candidate_id') == candidate_id), None)
    if track is None:
        track = {'id': uid('track'), 'name': candidate['name'], 'role': 'converted', 'candidate_id': candidate_id,
                 'artifact_id': output['id'], 'voice_part': candidate.get('voice_part'), 'gain_db': 0, 'pan': 0,
                 'offset_ticks': output.get('timeline_offset_ticks', 0), 'effects': [], 'solo': False}
        project['tracks'].append(track)
    entry = {'id': uid('final'), 'candidate_id': candidate_id, 'track_id': track['id'], 'accepted_at': now()}
    project['final_list'].append(entry)
    track.update(mute=False, final_id=entry['id'])
    candidate.update(review_status='accepted', reviewed_at=now(), selected=True)
    invalidate_mix(project, 'Final List 已添加人工验收的人声')


def review(project, candidate_id, action):
    initialize(project)
    candidate = next((c for c in project['candidates'] if c['id'] == candidate_id), None)
    if not candidate:
        raise ValueError('候选不存在。')
    if action == 'accept':
        accept(project, candidate_id)
        return
    if any(f['candidate_id'] == candidate_id for f in project['final_list']):
        raise ValueError('此候选已在 Final List 中；请先移除成品条目。')
    if action not in ('retry', 'discard'):
        raise ValueError('验收操作必须为接受、重试或丢弃。')
    candidate.update(review_status='retry' if action == 'retry' else 'discarded', reviewed_at=now(), selected=False)
    if action == 'discard':
        candidate.update(archived=True, archive_reason='manual', archived_at=now())
    else:
        part = candidate.get('voice_part')
        if part and project.get('voice_parts', {}).get(part, {}).get('source_artifact_id') != candidate['source_artifact_id']:
            raise ValueError('源轨绑定已改变，请先重新选择候选的源轨。')
        if not part:
            switch_source(project, candidate['source_artifact_id'])
        project['parameters']['active_voice_part'] = part or 'vocal'
        project['view_preferences'].update(stage=3, generation_step='edit')
    # Never submit a job here. User edits configuration then explicitly generates.


def remove_final(project, identifier):
    entry = next((f for f in project['final_list'] if f['id'] == identifier), None)
    if not entry:
        raise ValueError('成品条目不存在。')
    project['final_list'].remove(entry)
    for track in project['tracks']:
        if track['id'] == entry['track_id']:
            track.update(mute=True, solo=False)
            track.pop('final_id', None)
    candidate = next(c for c in project['candidates'] if c['id'] == entry['candidate_id'])
    candidate.update(selected=False, review_status='pending')
    invalidate_mix(project, 'Final List 已移除人声')


def routed_tracks(project):
    ids = {f['track_id'] for f in project.get('final_list', [])}
    background = project.get('background_track_id')
    if background:
        ids.add(background)
    return [copy.deepcopy(t) for t in project['tracks'] if t['id'] in ids]


def timeline_end(project):
    # Preserve original duration even if the Background is muted; allow positive tails.
    return max((round(a['sample_count'] * TIMEBASE / a['sample_rate']) + a.get('timeline_offset_ticks', 0)
                for a in project['assets'] if a.get('stem_role') in LOCKED_ROLES), default=0)


def restore_state(project, source, mapping):
    if not enabled(source):
        initialize(project)
        return
    project.update(production_version=1, pitch_versions=[], active_pitch_versions={}, final_list=[], input_workspaces={})
    for version in source.get('pitch_versions', []):
        if version['source_artifact_id'] not in mapping:
            continue
        value = copy.deepcopy(version)
        value['source_artifact_id'] = mapping[value['source_artifact_id']]
        for state in [value, *value.get('undo', []), *value.get('redo', [])]:
            params = state['parameters']
            if params.get('pitch_artifact_id'):
                params['pitch_artifact_id'] = mapping.get(params['pitch_artifact_id'])
        project['pitch_versions'].append(value)
    valid = {v['id'] for v in project['pitch_versions']}
    project['active_pitch_versions'] = {mapping[s]: v for s, v in source.get('active_pitch_versions', {}).items() if s in mapping and v in valid}
    candidates = {c['id'] for c in project['candidates'] if c.get('state') == 'succeeded'}
    tracks = {t['id'] for t in project['tracks']}
    project['final_list'] = [copy.deepcopy(f) for f in source.get('final_list', []) if f['candidate_id'] in candidates and f['track_id'] in tracks]
    accepted={f['candidate_id'] for f in project['final_list']}
    for candidate in project['candidates']:
        if candidate.get('review_status')=='accepted' and candidate['id'] not in accepted:
            candidate.update(review_status='pending',selected=False)
    project['background_track_id'] = source.get('background_track_id') if source.get('background_track_id') in tracks else None
    for key, workspace in source.get('input_workspaces', {}).items():
        if key in mapping:
            value = copy.deepcopy(workspace)
            if value['parameters'].get('pitch_artifact_id'):
                value['parameters']['pitch_artifact_id'] = mapping.get(value['parameters']['pitch_artifact_id'])
            project['input_workspaces'][mapping[key]] = value
    synchronize(project)

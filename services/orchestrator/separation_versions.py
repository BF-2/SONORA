"""Project-owned separation sets and reversible selection of their tracks."""
import copy
from pathlib import Path
from .store import now, uid
from .voices import VOICE_PARTS, new_binding, sync_part_tracks, invalidate_mix, voice_part

ROLES = {'separate': ('vocal', 'instrumental'), 'separate_harmony': VOICE_PARTS}
NAMES = {'separate': '人声与伴奏', 'separate_harmony': '主唱与和声'}


def materialize(project):
    """Recover sets from older project graphs without changing existing IDs."""
    runs = project.setdefault('separation_runs', [])
    known = {(run['operation'], tuple(run['artifact_ids'])) for run in runs}
    assets = {asset['id']: asset for asset in project['assets']}
    for node in project.get('graph', []):
        refs = [ref for ref in node.get('artifact_refs', []) if ref in assets]
        for operation, roles in ROLES.items():
            pair = [ref for role in roles for ref in refs if assets[ref].get('stem_role') == role]
            key = (operation, tuple(pair))
            if len(pair) != 2 or key in known:
                continue
            runs.append({'id': node['id'], 'operation': operation,
                         'name': NAMES[operation] + ' · 历史结果',
                         'artifact_ids': pair, 'input_artifact_id': (assets[pair[0]].get('input_artifact_ids') or [None])[0],
                         'parameters': copy.deepcopy(node.get('parameters', {})),
                         'created_at': assets[pair[0]].get('created_at', project['created_at']),
                         'archived': False, 'job_id': node.get('job_id')})
            known.add(key)
    selected = project.setdefault('selected_separations', {})
    for operation, roles in ROLES.items():
        if operation in selected:
            continue
        if operation == 'separate_harmony':
            active = {part.get('source_artifact_id') for part in project.get('voice_parts', {}).values()}
        else:
            active = {track['artifact_id'] for track in project['tracks'] if track['role'] == 'instrumental' and not track.get('mute')}
        match = next((run for run in reversed(runs) if run['operation'] == operation and active.intersection(run['artifact_ids'])), None)
        selected[operation] = match['id'] if match else None
    return runs


def remember_active(project):
    selected = project.get('selected_separations', {})
    for run in project.get('separation_runs', []):
        if selected.get(run['operation']) == run['id']:
            run['voice_parts_snapshot'] = copy.deepcopy(project.get('voice_parts', {}))


def adopt(project, run_id, user_edit=True):
    runs = materialize(project)
    run = next((run for run in runs if run['id'] == run_id), None)
    if run is None:
        raise ValueError('分离版本不属于当前项目。')
    if run.get('archived'):
        raise ValueError('请先恢复这个分离版本。')
    assets = {asset['id']: asset for asset in project['assets']}
    pair = [assets[ref] for ref in run['artifact_ids']]
    if any(not Path(asset['path']).is_file() for asset in pair):
        raise ValueError('该分离版本有素材缺失，请先重新链接。')
    remember_active(project)
    operation = run['operation']
    if operation == 'separate_harmony':
        parent = next((item for item in reversed(runs) if item['operation'] == 'separate' and run['input_artifact_id'] in item['artifact_ids']), None)
        if parent and project['selected_separations'].get('separate') != parent['id']:
            if parent.get('archived'):
                raise ValueError('此声部版本的上游分离已归档，请先恢复上游版本。')
            adopt(project, parent['id'], user_edit=False)
        saved = run.get('voice_parts_snapshot', {})
        project['voice_parts'] = {asset['stem_role']: new_binding(asset, saved.get(asset['stem_role']) or project.get('voice_parts', {}).get(asset['stem_role'])) for asset in pair}
        for binding in project['voice_parts'].values():
            candidate = next((c for c in project['candidates'] if c['id'] == binding.get('selected_candidate_id')), None)
            if candidate and candidate.get('archived'):
                binding['selected_candidate_id'] = None
        sync_part_tracks(project)
    else:
        vocal = next(asset for asset in pair if asset['stem_role'] == 'vocal')
        old_source = project['parameters'].get('source_artifact_id')
        if old_source != vocal['id']:
            project['voice_parts'] = {}
            project['selected_separations']['separate_harmony'] = None
            project['selected_candidate_id'] = None
            project['parameters']['active_voice_part'] = 'vocal'
            project['parameters']['pitch_artifact_id'] = None
            for candidate in project['candidates']:
                candidate['selected'] = False
        project['parameters']['source_artifact_id'] = vocal['id']
        project['parameters']['harmony_source_artifact_id'] = vocal['id']
        selected_tracks = {role: next((track['id'] for track in reversed(project['tracks']) if track['artifact_id'] == asset['id'] and track['role'] == role), None) for role in ROLES[operation] for asset in pair if asset['stem_role'] == role}
        for track in project['tracks']:
            if track['role'] in selected_tracks:
                track['mute'] = track['id'] != selected_tracks[track['role']]
                if track['mute']:
                    track['solo'] = False
            elif old_source != vocal['id'] and (voice_part(track) or track['role'] in ('converted', 'original', 'mix')):
                track['mute'] = True
                track['solo'] = False
        if project.get('voice_parts'):
            sync_part_tracks(project)
    project['selected_separations'][operation] = run['id']
    invalidate_mix(project, '已采用' + run['name'])
    if user_edit:
        project['last_user_audio_edit'] = uid('edit')


def register(project, job, artifacts):
    materialize(project)
    operation = 'separate_harmony' if artifacts[0]['stem_role'] in VOICE_PARTS else 'separate'
    remember_active(project)
    for asset in artifacts:
        if not any(existing['id'] == asset['id'] for existing in project['assets']):
            project['assets'].append(asset)
        if not any(track['artifact_id'] == asset['id'] and track['role'] == asset['stem_role'] for track in project['tracks']):
            project['tracks'].append({'id': uid('track'), 'name': asset['name'], 'role': asset['stem_role'],
                                      'voice_part': voice_part(asset), 'artifact_id': asset['id'],
                                      'gain_db': 0, 'pan': 0, 'mute': True, 'solo': False,
                                      'offset_ticks': asset.get('timeline_offset_ticks', 0), 'effects': []})
    count = sum(run['operation'] == operation for run in project['separation_runs']) + 1
    run = {'id': uid('separation'), 'operation': operation, 'name': f'{NAMES[operation]} · 第 {count} 次',
           'artifact_ids': [asset['id'] for asset in artifacts], 'input_artifact_id': (artifacts[0].get('input_artifact_ids') or [job.get('input_artifact_id')])[0],
           'job_id': job['id'], 'created_at': now(), 'archived': False,
           'parameters': copy.deepcopy(job['parameters'])}
    project['separation_runs'].append(run)
    before = job['project_snapshot']
    unchanged = (
        project.get('voice_parts', {}) == before.get('voice_parts', {})
        and project.get('selected_separations', {}) == before.get('selected_separations', project.get('selected_separations', {}))
        and project.get('last_user_audio_edit') == before.get('last_user_audio_edit')
    )
    if unchanged:
        adopt(project, run['id'], user_edit=False)
    else:
        project['warnings'].append({'code': 'separation_previous_revision', 'message': '分离期间项目选择已变化，新结果已保留，请在分离版本中采用。'})
    project['graph'].append({'id': uid('node'), 'job_id': job['id'], 'type': operation,
                             'input_refs': [run['input_artifact_id']], 'artifact_refs': run['artifact_ids'],
                             'state': 'succeeded', 'project_revision': job['project_revision'],
                             'parameters': copy.deepcopy(job['parameters'])})


def protected(project, run):
    if run['id'] in project.get('selected_separations', {}).values():
        return True
    refs = set(run['artifact_ids'])
    if any(not track.get('mute') and track['artifact_id'] in refs for track in project['tracks']):
        return True
    return any(binding.get('source_artifact_id') in refs for binding in project.get('voice_parts', {}).values())


def list_runs(project):
    view = copy.deepcopy(project)
    runs = materialize(view)
    assets = {asset['id']: asset for asset in view['assets']}
    return [{**run, 'selected': view['selected_separations'].get(run['operation']) == run['id'],
             'model_id': assets.get(run['artifact_ids'][0], {}).get('worker_metadata', {}).get('model_id'),
             'protected': protected(view, run),
             'outputs': [{key: asset.get(key) for key in ('id', 'name', 'stem_role', 'sample_rate', 'sample_count', 'channels', 'sha256', 'diagnostics')} for asset in view['assets'] if asset['id'] in run['artifact_ids']]}
            for run in reversed(runs)]


def restore_runs(project, source, mapping):
    """Restore only complete pairs, while preserving archive and saved voice edits."""
    from .voices import remap_parts
    candidate_ids = {candidate['id'] for candidate in project['candidates']}
    restored = []
    for run in source.get('separation_runs', []):
        if not all(ref in mapping for ref in run['artifact_ids']):
            continue
        restored.append({**copy.deepcopy(run),
                         'artifact_ids': [mapping[ref] for ref in run['artifact_ids']],
                         'input_artifact_id': mapping.get(run.get('input_artifact_id')),
                         'voice_parts_snapshot': remap_parts(run.get('voice_parts_snapshot', {}), mapping, candidate_ids)})
    project['separation_runs'] = restored
    valid_ids = {run['id'] for run in restored}
    project['selected_separations'] = {operation: identifier for operation, identifier in source.get('selected_separations', {}).items() if identifier in valid_ids}


def change_run(project, run_id, action):
    materialize(project)
    run = next((item for item in project['separation_runs'] if item['id'] == run_id), None)
    if not run:
        raise ValueError('分离版本不属于当前项目。')
    if action == 'adopt':
        adopt(project, run_id)
    elif action == 'archive':
        if protected(project, run):
            raise ValueError('此分离版本正在使用，请先采用其他结果。')
        run.update(archived=True, archived_at=now())
    elif action == 'restore':
        run.update(archived=False, archived_at=None)
    else:
        raise ValueError('未知分离版本操作。')

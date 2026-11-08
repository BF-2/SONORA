"""One routing contract for full renders and selection monitoring."""
import copy
from . import media

def mix_tracks(project):
    from .production import enabled, routed_tracks
    if enabled(project):return routed_tracks(project)
    previews={c['artifact_id'] for c in project['candidates'] if c.get('preview_only')}
    tracks=[copy.deepcopy(t) for t in project['tracks'] if t['role'] not in ('original','mix','mix_preview','pitch') and t['artifact_id'] not in previews]
    if project.get('voice_parts'):
        tracks=[t for t in tracks if t['role'] not in ('vocal','vocals','guide','synthesized') and not(t['role']=='converted' and not t.get('voice_part'))]
    return tracks or [copy.deepcopy(t) for t in project['tracks'] if t['role']=='original']

def preview_key(project,region,monitor_track_id=None):
    tracks=mix_tracks(project)
    assets={a['id']:a for a in project['assets']}
    ids={t['artifact_id'] for t in tracks}
    for t in tracks:
        if any(fx.get('enabled',True) and fx.get('type')=='vocal_restore' for fx in t.get('effects',[])):
            ids.update(assets.get(t['artifact_id'],{}).get('input_artifact_ids',[]))
    return media.hash_json({'mix_preview_version':3,'tracks':tracks,'source_hashes':{i:assets.get(i,{}).get('sha256') for i in ids},'range_ticks':region,'monitor_track_id':monitor_track_id})

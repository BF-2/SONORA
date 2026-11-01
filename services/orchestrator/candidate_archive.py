"""Reversible candidate housekeeping. Media and provenance are never deleted."""
from .store import now

def archive_older(project):
    candidates = project.get('candidates', [])
    protected = {project.get('selected_candidate_id'), project.get('parameters', {}).get('comp_base_candidate_id'),
                 project.get('view_preferences', {}).get('ab_reference_candidate_id')}
    protected.update(b.get('selected_candidate_id') for b in project.get('voice_parts', {}).values())
    protected.update(s.get('candidate_id') for s in project.get('parameters', {}).get('comp_segments', []))
    live_audio = {t['artifact_id'] for t in project.get('tracks', []) if not t.get('mute') or t.get('solo')}
    groups = {}
    for c in candidates:
        scope = (c.get('voice_part', 'vocal'), c.get('part_source_artifact_id') or c.get('source_artifact_id'))
        groups.setdefault(scope, []).append(c)
    archived = []
    for group in groups.values():
        # Keep a newly generated comparison batch together so an 8-version A/B
        # experiment cannot hide itself while it is still being generated.
        recent = group[-2:]
        keep = {c['id'] for c in recent}
        comparisons = {c.get('comparison_id') for c in recent if c.get('comparison_id')}
        for c in group:
            if c.get('archived') or c['id'] in keep | protected or c.get('favorite') or c.get('archive_retained'):
                continue
            if c['artifact_id'] in live_audio or c.get('comparison_id') in comparisons:
                continue
            c.update(archived=True, archive_reason='auto', archived_at=now())
            archived.append(c['id'])
    return archived

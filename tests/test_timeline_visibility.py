import copy
import pytest
from tests.test_core import fixture_project, req, store


def test_timeline_archive_is_persisted_view_state_and_keeps_audio_history():
    project, _ = fixture_project()
    track_id = project['tracks'][0]['id']
    before = copy.deepcopy(project)
    with store.connect() as db:
        history_before = db.execute('SELECT count(*) FROM history WHERE project_id=?', (project['id'],)).fetchone()[0]
    changed = req('PATCH', '/projects/' + project['id'], json={
        'revision': project['revision'],
        'patch': {'view_preferences': {'stage': 0, 'timeline_archived_track_ids': [track_id], 'timeline_included_track_ids': []}},
    })
    saved = req('GET', '/projects/' + project['id'])
    assert saved['view_preferences']['timeline_archived_track_ids'] == [track_id]
    for field in ('assets', 'tracks', 'parameters', 'candidates', 'voice_parts', 'exports', 'graph'):
        assert saved[field] == before[field]
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM history WHERE project_id=?', (project['id'],)).fetchone()[0] == history_before
    restored = req('PATCH', '/projects/' + project['id'], json={
        'revision': changed['revision'],
        'patch': {'view_preferences': {'stage': 0, 'timeline_archived_track_ids': [], 'timeline_included_track_ids': [track_id]}},
    })
    assert restored['view_preferences']['timeline_included_track_ids'] == [track_id]
    assert restored['tracks'] == before['tracks']


@pytest.mark.parametrize('identifiers', [['foreign_track'], [123], 'track_id'])
def test_timeline_visibility_rejects_unknown_or_invalid_track_ids(identifiers):
    from services.orchestrator.app import validate_patch
    project, _ = fixture_project()
    with pytest.raises(ValueError, match='时间轴'):
        validate_patch(project, {'view_preferences': {'timeline_archived_track_ids': identifiers}})


def test_timeline_visibility_accepts_owned_pending_recipe_tracks():
    from services.orchestrator.app import validate_patch
    project, _ = fixture_project()
    project['restore_pending'] = {'source': {'tracks': [{'id': 'missing_owned_track'}]}}
    validate_patch(project, {'view_preferences': {'timeline_archived_track_ids': ['missing_owned_track']}})

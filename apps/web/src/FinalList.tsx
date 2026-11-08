import { Badge, Card, Field } from './components';
import { seconds, ticks } from './types';
import type { Bag, Project } from './types';

export function finalMixTracks(project?: Project | null) {
  if (!project) return [];
  const ids = new Set((project.final_list || []).map((f: Bag) => f.track_id));
  if (project.background_track_id) ids.add(project.background_track_id);
  return project.tracks.filter(t => ids.has(t.id));
}

export function FinalList({ project, busy, onTrack, onListen, onRemove, onBackground }: {
  project: Project; busy: boolean; onTrack: (id: string, fields: Bag) => void;
  onListen: (id: string) => void; onRemove: (id: string) => void; onBackground: (id: string) => void;
}) {
  return <Card title={`Final List · ${project.final_list?.length || 0} 条已验收人声`} icon="layers">
    <div className="card-pad generation-manager">
      <Field label="Background · 只加入一条锁定伴奏" hint="干声变声可没有伴奏。混音只使用这里的 Background 和下列未静音成品。">
        <select aria-label="混音 Background" value={project.background_track_id || ''} disabled={busy}
          onChange={e => e.target.value && onBackground(e.target.value)}>
          <option value="">无 Background · 干声工程</option>
          {project.tracks.filter(t => t.role === 'instrumental').map(t => <option key={t.id} value={t.id}>{t.name} · Lock</option>)}
        </select>
      </Field>
      {!project.final_list?.length && <p className="info-inline">试听候选并点击“接受”后添加到这里。可保留同一输入的多个版本。</p>}
      {(project.final_list || []).map((entry: Bag) => {
        const track = project.tracks.find(t => t.id === entry.track_id);
        const candidate = project.candidates.find(c => c.id === entry.candidate_id);
        if (!track || !candidate) return null;
        return <article key={entry.id} className="generation-row" data-final-id={entry.id}>
          <div className="generation-header">
            <Field label="轨道名称"><input aria-label="成品轨道名称" value={track.name} maxLength={120} onChange={e => onTrack(track.id, { name: e.target.value })}/></Field>
            <Badge>人工接受</Badge>
          </div>
          <p>{candidate.model_snapshot?.name || candidate.model_id} · Epoch {candidate.model_snapshot?.epoch ?? '未提供'} · checkpoint {candidate.checkpoint_id || candidate.model_id}</p>
          <p>F0 {candidate.f0_version_snapshot?.name || '历史曲线'} · r{candidate.f0_version_revision ?? '—'} · {candidate.f0_version_id || candidate.pitch_artifact_id}</p>
          <div className="row wrap">
            <button className="button small" onClick={() => onListen(track.id)}>独立试听</button>
            <label><input type="checkbox" checked={track.mute} onChange={e => onTrack(track.id, { mute: e.target.checked })}/> 静音</label>
            <button className="text-button" disabled={busy} onClick={() => onRemove(entry.id)}>删除成品条目</button>
          </div>
          <div className="field-grid">
            <Field label="音量（dB）"><input type="number" min={-96} max={24} step={.5} value={track.gain_db} onChange={e => onTrack(track.id, { gain_db: +e.target.value })}/></Field>
            <Field label="声像（左 -1 / 右 +1）"><input type="number" min={-1} max={1} step={.05} value={track.pan} onChange={e => onTrack(track.id, { pan: +e.target.value })}/></Field>
            <Field label="时间偏移（秒）"><input type="number" step={.001} value={seconds(track.offset_ticks)} onChange={e => onTrack(track.id, { offset_ticks: ticks(+e.target.value) })}/></Field>
          </div>
        </article>;
      })}
    </div>
  </Card>;
}

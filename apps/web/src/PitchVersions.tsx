import { Badge, Card, Field, Slider } from './components';
import type { Bag, Project } from './types';

export function PitchVersions({ project, sourceId, busy, onAction, values, onParameters }: {
  project: Project; sourceId?: string; busy: boolean;
  values: Bag; onParameters: (changes: Bag) => void;
  onAction: (action: string, versionId?: string, name?: string) => void;
}) {
  const versions: Bag[] = (project.pitch_versions || []).filter((v: Bag) => v.source_artifact_id === sourceId);
  const current = versions.find(v => v.id === project.active_pitch_versions?.[sourceId || '']);
  const settings = { enabled: true, min_silence_ms: 0, fade_ms: 3, ...values.voicing_settings };
  const setSettings = (changes: Bag) => onParameters({ voicing_settings: { ...settings, ...changes } });
  return <Card title="F0 编辑版本" icon="branch" aside={<Badge>源音频 Lock</Badge>}>
    <div className="card-pad">
      <div className="row wrap">
        <Field label="当前版本"><select aria-label="当前 F0 编辑版本" value={current?.id || ''} disabled={busy || !sourceId}
          onChange={e => onAction('select', e.target.value)}>
          {!versions.length && <option value="">导入并选择人声后建立 Edit 1</option>}
          {versions.map(v => <option key={v.id} value={v.id}>{v.name} · r{v.revision}</option>)}
        </select></Field>
        <button className="button small" disabled={!sourceId || busy} onClick={() => onAction('create')}>+ 新建 Edit {versions.length + 1}</button>
        <button className="button small" disabled={!current || busy} onClick={() => onAction('save', current?.id)}>保存版本</button>
        <button className="button small" disabled={!current?.undo?.length || busy} onClick={() => onAction('undo', current?.id)}>撤销音高编辑</button>
        <button className="button small" disabled={!current?.redo?.length || busy} onClick={() => onAction('redo', current?.id)}>重做</button>
      </div>
      <label className="toggle-row">
        <span>{current?.name || '当前 Edit'} · 无音高区域静音</span>
        <input aria-label="当前 Edit 无音高区域静音" type="checkbox" checked={settings.enabled}
          disabled={!current || busy} onChange={event => setSettings({ enabled: event.target.checked })}/>
      </label>
      <fieldset aria-label="静音区间与平滑设置" disabled={!current || busy || !settings.enabled} style={{ border: 0, padding: 0, margin: 0 }}>
        <Slider label="最小静音区间" value={settings.min_silence_ms} min={0} max={2000} step={1} suffix="ms"
          onChange={value => setSettings({ min_silence_ms: value })} hint="连续无音高区达到此长度才静音。0 表示处理全部无音高区，较短的间隙保留转换声音。"/>
        <Slider label="静音边界平滑长度" value={settings.fade_ms} min={0} max={200} step={1} suffix="ms"
          onChange={value => setSettings({ fade_ms: value })} hint="每侧淡入或淡出的长度。0 为直接切换，较短的有声段会自动缩短平滑长度。"/>
      </fieldset>
      <p className="info-inline">此设置随当前 Edit 保存，切换版本会恢复各自的配置，重新生成候选后生效。</p>
      <p className="info-inline">每个输入分别保存 F0、无声帧和编辑版本。新版本复制当前曲线；保存与撤销都不会修改 Lock 原音频。</p>
      {current && <small>{current.id} · {current.pitch_edits.length} 项编辑 · {current.parameters.pitch_artifact_id ? '已绑定独立分析' : '等待提取 F0'}</small>}
    </div>
  </Card>;
}

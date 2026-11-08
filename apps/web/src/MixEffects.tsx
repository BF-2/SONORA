import { useEffect, useState } from 'react';
import { Field } from './components';
import { seconds, ticks } from './types';
import type { Bag, Project, Track } from './types';

type Control = { key: string; label: string; min: number; max: number; step: number; unit?: string };
const spaceControls: Control[] = [
  { key: 'room_seconds', label: '衰减时间', min: .1, max: 3, step: .05, unit: '秒' },
  { key: 'predelay_ms', label: '预延迟', min: 0, max: 100, step: 1, unit: 'ms' },
  { key: 'width', label: '立体声宽度', min: 0, max: 1.5, step: .05 },
  { key: 'ducking', label: '演唱时压低湿声', min: 0, max: .8, step: .05 },
  { key: 'lowcut_hz', label: '湿声低切', min: 40, max: 1200, step: 10, unit: 'Hz' },
  { key: 'damping_hz', label: '湿声高频截止', min: 1500, max: 16000, step: 100, unit: 'Hz' },
];
const roomDefaults = { room_seconds: .4, predelay_ms: 18, width: 1, ducking: .3, lowcut_hz: 220, damping_hz: 6200 };
const definitions: { type: string; name: string; defaults: Bag; controls: Control[] }[] = [
  { type: 'vocal_restore', name: '人声厚度修复', defaults: { strength: 100 }, controls: [{ key: 'strength', label: '修复强度', min: 0, max: 150, step: 5, unit: '%' }] },
  { type: 'vocal_space', name: 'C 人声空间', defaults: { strength: 100, ...roomDefaults }, controls: [{ key: 'strength', label: '空间强度', min: 0, max: 350, step: 5, unit: '%' }, ...spaceControls] },
  { type: 'reverb', name: '房间混响', defaults: { algorithm: 'diffuse-v1', wet: .15, ...roomDefaults, room_seconds: .65 }, controls: [{ key: 'wet', label: '湿声量', min: 0, max: .5, step: .01 }, ...spaceControls] },
  { type: 'highpass', name: '高通滤波', defaults: { frequency: 80 }, controls: [{ key: 'frequency', label: '截止频率', min: 20, max: 22000, step: 10, unit: 'Hz' }] },
  { type: 'lowpass', name: '低通滤波', defaults: { frequency: 16000 }, controls: [{ key: 'frequency', label: '截止频率', min: 20, max: 22000, step: 10, unit: 'Hz' }] },
  { type: 'compressor', name: '压缩器', defaults: { threshold_db: -18, ratio: 3 }, controls: [{ key: 'threshold_db', label: '阈值', min: -60, max: 0, step: 1, unit: 'dB' }, { key: 'ratio', label: '压缩比', min: 1, max: 20, step: .1 }] },
  { type: 'delay', name: '延迟', defaults: { delay_ms: 120, decay: .2 }, controls: [{ key: 'delay_ms', label: '延迟时间', min: 1, max: 2000, step: 1, unit: 'ms' }, { key: 'decay', label: '回声量', min: 0, max: .9, step: .01 }] },
  { type: 'limiter', name: '限制器', defaults: { limit_db: -1 }, controls: [{ key: 'limit_db', label: '峰值上限', min: -18, max: 0, step: .1, unit: 'dB' }] },
];

function EffectControl({ control, value, prefix, onCommit }: { control: Control; value: number; prefix: string; onCommit: (v: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const n = Number(draft);
    if (draft.trim() === '' || !Number.isFinite(n)) { setDraft(String(value)); return; }
    const bounded = Math.max(control.min, Math.min(control.max, n)); setDraft(String(bounded));
    if (bounded !== value) onCommit(bounded);
  };
  return <Field label={`${control.label}${control.unit ? ' · ' + control.unit : ''}`}>
    <div className="fx-control">
      <input aria-label={`${prefix} ${control.label} 滑块`} type="range" min={control.min} max={control.max} step={control.step}
        value={Number(draft) || 0} onChange={e => setDraft(e.target.value)} onPointerUp={commit} onKeyUp={commit} onBlur={commit} />
      <input aria-label={`${prefix} ${control.label}`} type="number" min={control.min} max={control.max} step={control.step}
        value={draft} onChange={e => setDraft(e.target.value)} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
    </div>
  </Field>;
}

export function MixEffects({ track, project, onChange }: { track: Track; project: Project; onChange: (fields: Bag) => void }) {
  const effects = track.effects || [];
  const artifact = project.assets.find(a => a.id === track.artifact_id);
  const inputs = project.assets.filter(a => artifact?.input_artifact_ids?.includes(a.id));
  const canRestore = inputs.some(a => a.media_type?.startsWith('audio/')) && inputs.some(a => a.stem_role === 'pitch');
  const [newType, setNewType] = useState('vocal_space');
  const update = (index: number, patch: Bag) => onChange({ effects: effects.map((fx, i) => i === index ? { ...fx, ...patch } : fx) });
  const move = (index: number, delta: number) => {
    const next = [...effects]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; onChange({ effects: next });
  };
  const applyC = () => {
    const next = effects.filter(fx => !['vocal_restore', 'vocal_space'].includes(fx.type)).map(fx => fx.type === 'reverb' ? { ...fx, enabled: false } : fx);
    if (canRestore) next.push({ type: 'vocal_restore', name: '人声厚度修复', strength: 100, enabled: true });
    next.push({ type: 'vocal_space', name: 'C 人声空间', strength: 100, ...roomDefaults, enabled: true });
    onChange({ effects: next });
  };
  return <div className="mix-effects" aria-label="混音效果设置">
    <strong className="track-heading">{track.name}</strong>
    <Field label="对齐偏移（秒）"><input type="number" step={.001} value={seconds(track.offset_ticks || 0)}
      onChange={e => onChange({ offset_ticks: ticks(+e.target.value) })} /></Field>
    <div className="fx-feature">
      <button className="button primary small" onClick={applyC}>{canRestore ? '应用 C 方案' : '添加 C 空间'}</button>
      <p>{canRestore ? '加入参考引导的厚度修复与 C 空间，停用房间混响。强度可分别调节。' : '为当前轨道添加可调立体声空间，停用房间混响。厚度修复用于有原人声与音高来源的转换候选。'}</p>
    </div>
    {effects.map((saved, index) => {
      const def = definitions.find(d => d.type === saved.type);
      if (!def) return <p key={index}>未知效果：{saved.type}</p>;
      const fx = { ...def.defaults, ...saved };
      const legacy = saved.type === 'reverb' && (saved.algorithm || 'legacy') === 'legacy';
      const controls = legacy ? def.controls.filter(c => ['wet', 'room_seconds', 'predelay_ms'].includes(c.key)) : def.controls;
      return <section className={`fx-item ${saved.enabled === false ? 'fx-bypassed' : ''}`} key={`${track.id}-${index}-${saved.type}`} data-effect={saved.type}>
        <div className="fx-title"><label className="toggle-row"><input type="checkbox" aria-label={`启用${def.name}`}
          checked={saved.enabled !== false} onChange={e => update(index, { enabled: e.target.checked })} /><strong>{index + 1}. {def.name}</strong></label>
          <div className="fx-actions"><button className="button small ghost" aria-label={`${def.name}前移`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
            <button className="button small ghost" aria-label={`${def.name}后移`} disabled={index === effects.length - 1} onClick={() => move(index, 1)}>↓</button>
            <button className="button small ghost" aria-label={`移除${def.name}`} onClick={() => onChange({ effects: effects.filter((_, i) => i !== index) })}>×</button></div>
        </div>
        {legacy && <div className="fx-legacy"><span>旧版延迟混响 · 保留原声音</span><button className="button small" onClick={() => update(index, { ...roomDefaults, room_seconds: fx.room_seconds, predelay_ms: fx.predelay_ms, algorithm: 'diffuse-v1' })}>升级为立体声混响</button></div>}
        {saved.enabled !== false && <>
          {saved.type === 'vocal_space' && <div className="fx-presets">{[['C · 轻空间', 100], ['C+ · 增强', 200], ['C++ · 更强', 316]].map(([name, strength]) =>
            <button key={name} className={`button small ${fx.strength === strength ? 'primary' : ''}`} onClick={() => update(index, { strength })}>{name}</button>)}</div>}
          {controls.filter(c => legacy || !['vocal_space', 'reverb'].includes(saved.type) || ['strength', 'wet', 'room_seconds', 'width'].includes(c.key)).map(c =>
            <EffectControl key={c.key} prefix={def.name} control={c} value={Number(fx[c.key])} onCommit={value => update(index, { [c.key]: value })} />)}
          {!legacy && ['vocal_space', 'reverb'].includes(saved.type) && <details className="fx-advanced"><summary>更多空间参数</summary>
            {controls.filter(c => !['strength', 'wet', 'room_seconds', 'width'].includes(c.key)).map(c =>
              <EffectControl key={c.key} prefix={def.name} control={c} value={Number(fx[c.key])} onCommit={value => update(index, { [c.key]: value })} />)}
          </details>}
          {saved.type === 'vocal_restore' && <p className="info-inline">根据该候选的原人声和音高计算补偿；不混回原歌手声音。</p>}
          {saved.type === 'vocal_space' && <p className="info-inline">100% 对应原 C，200% 对应 C+，316% 对应 C++。0% 旁路空间；宽度 0 保留居中混响。</p>}
        </>}
      </section>;
    })}
    <div className="fx-add"><select aria-label="添加效果类型" value={newType} onChange={e => setNewType(e.target.value)}>
      {definitions.map(d => <option key={d.type} value={d.type} disabled={d.type === 'vocal_restore' && !canRestore}>{d.name}</option>)}</select>
      <button className="button small" disabled={effects.length >= 16 || (newType === 'vocal_restore' && !canRestore)} onClick={() => {
        const def = definitions.find(d => d.type === newType)!; onChange({ effects: [...effects, { type: def.type, name: def.name, ...def.defaults, enabled: true }] });
      }}>添加</button></div>
    <p className="info-inline">效果按上方顺序处理。松开滑块后更新试听；预览与导出使用同一处理链。</p>
  </div>;
}

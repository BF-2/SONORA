import { Field, Slider } from "./components";
import type { Bag } from "./types";

export function SeparationPanel({ parameters, onChange, harmony = false, models = [] }: {
  parameters: Bag; onChange: (p: Bag) => void; harmony?: boolean; models?: Bag[];
}) {
  const key = harmony ? "harmony_settings" : "separation_settings";
  const settings = { segment_size: parameters.segment_size ?? (harmony ? 801 : 256),
    overlap_step_seconds: parameters.overlap_step_seconds ?? parameters.overlap ?? 4,
    separation_algorithm: parameters.separation_algorithm ?? (harmony ? "ensemble" : "single"), primary_gain: parameters.primary_gain ?? 1,
    cpu_threads: parameters.cpu_threads ?? 4, secondary_weight: .5, backing_suppression: 0, spatial_protection: .8, secondary_model_id: '', ...parameters[key] };
  const update = (p: Bag) => onChange({ [key]: { ...settings, ...p } });
  const chunk = (settings.segment_size - 1) * .01;
  const step = Math.min(chunk, settings.overlap_step_seconds);
  return <div className="separation-panel">
    {!harmony && <Field label="人声 / 伴奏分离模型"><select value={parameters.separation_model_id || models[0]?.id || ""} onChange={e => onChange({ separation_model_id: e.target.value })}>{models.map(m => <option value={m.id} key={m.id}>{m.name}</option>)}</select></Field>}
    <div className="row wrap separation-presets"><span>参数起点</span><button className="button small" onClick={() => update({ segment_size: 256, overlap_step_seconds: 2.5, separation_algorithm: "single", primary_gain: 1 })}>快速检查</button><button className="button small" onClick={() => update({ segment_size: 801, overlap_step_seconds: 2, separation_algorithm: "single", primary_gain: 1 })}>均衡</button><button className="button small" onClick={() => update({ segment_size: 801, overlap_step_seconds: 1, separation_algorithm: "tta", primary_gain: 1 })}>精细对照</button></div>
    <div className="field-grid">
      <Field label={harmony ? "主唱 / 和声推理算法" : "人声 / 伴奏推理算法"} hint={harmony ? "双模型各进行三次错位推理，再对齐融合。约六次推理，使用已安装的 Karaoke 权重。" : "增强平均使用三次推理。"}>
        <select value={settings.separation_algorithm} onChange={e => update({ separation_algorithm: e.target.value })}><option value="single">RoFormer · 单次推理</option><option value="tta">RoFormer · 三次增强平均</option>{harmony && <option value="ensemble">增强和声分离 · 双模型与时间错位融合</option>}</select>
      </Field>
      <Field label={harmony ? "主唱 / 和声运行设备" : "人声 / 伴奏运行设备"}><select value={parameters.device || "auto"} onChange={e => onChange({ device: e.target.value })}><option value="auto">自动（优先 GPU）</option><option value="cuda:0">GPU · CUDA 0</option><option value="cpu">CPU</option></select></Field>
    </div>
    {harmony && settings.separation_algorithm === 'ensemble' && <>
      <Field label="辅助和声模型"><select value={settings.secondary_model_id || ''} onChange={e => update({ secondary_model_id: e.target.value })}>
        <option value="">自动选择另一套已安装模型</option>
        {models.filter(model => model.id !== parameters.harmony_model_id).map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
      </select></Field>
      <Slider label="辅助模型融合比例" value={settings.secondary_weight} min={0} max={1} step={.05}
        onChange={value => update({ secondary_weight: value })} hint="推荐从 0.5 开始，同时参考两套模型的主唱估计。" />
      <Slider label="伴唱残留抑制" value={settings.backing_suppression} min={0} max={1} step={.05}
        onChange={value => update({ backing_suppression: value })} hint="默认 0 保留融合结果。增大时衰减两套模型都判为主唱占优的频段，请试听弱伴唱是否受损。" />
      <Slider label="立体声伴唱保护" value={settings.spatial_protection} min={0} max={1} step={.05}
        onChange={value => update({ spatial_protection: value })} hint="推荐 0.8。当主唱居中且伴唱的左右声道关系稳定时，利用声像差异保护伴唱。" />
    </>}
    <details><summary>调节分离细节 · 窗口 {chunk.toFixed(2)} 秒 / 步进 {step.toFixed(2)} 秒</summary>
      <Slider label={harmony ? "和声上下文窗口" : "人声上下文窗口"} value={settings.segment_size} min={64} max={1024} step={1} suffix="帧" onChange={v => update({ segment_size: v })} hint="窗口更大需要更多显存；不保证一定更清晰。" />
      <Slider label={harmony ? "和声窗口步进" : "人声窗口步进"} value={settings.overlap_step_seconds} min={.25} max={16} step={.25} suffix="秒" onChange={v => update({ overlap_step_seconds: v })} hint="步进越小，重叠计算越多；超过窗口长度时按窗口长度处理。" />
      <Slider label={harmony ? "主唱分配强度" : "人声分配强度"} value={settings.primary_gain} min={.5} max={1.5} step={.01} onChange={v => update({ primary_gain: v })} hint="1.00 为模型原估计。改变强度会把更多/更少内容分配到目标轨，互补轨同步调整；不是去串音保证。" />
      <Slider label={harmony ? "和声 CPU 线程" : "分离 CPU 线程"} value={settings.cpu_threads} min={1} max={8} step={1} onChange={v => update({ cpu_threads: v })} />
    </details>
    <p className="info-inline">窗口重叠约 {(100 * (1 - step / chunk)).toFixed(0)}% · {settings.separation_algorithm === "ensemble" ? "6" : settings.separation_algorithm === "tta" ? "3" : "1"} 次推理。分离结果可在版本列表中与先前结果试听对比。</p>
  </div>;
}

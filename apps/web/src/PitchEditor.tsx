import { useEffect, useMemo, useRef, useState } from "react";
import { post } from "./api";
import { seconds, ticks } from "./types";
import type { Bag, PitchData } from "./types";
import { CANVAS_PALETTES } from "./canvasPalette";

const midi = (hz: number) => 69 + 12 * Math.log2(hz / 440);
const hzAt = (note: number) => Math.min(2000, Math.max(20, 440 * 2 ** ((note - 69) / 12)));
const time = (s: number) => `${Math.floor(s / 60).toString().padStart(2, "0")}:${(s % 60).toFixed(3).padStart(6, "0")}`;
const noteName = (n: number) => `${["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"][((Math.round(n) % 12) + 12) % 12]}${Math.floor(Math.round(n) / 12) - 1}`;
const frameTime = (d: PitchData, i: number) => (i * d.hop + d.frame_center_offset) / d.sample_rate + seconds(d.timeline_offset_ticks || 0);

export function PitchCanvas({ data, outputData, selection, edits, notes, playhead, onSelect, onSeek, onStroke, tool,
  timelineDuration, projectId, pitchId, sourceId, parameters, view, onView, stale, onTargetHash, light = false }: {
  data: PitchData; outputData?: PitchData | null; selection: { start: number; end: number }; edits: Bag[]; notes: Bag[];
  playhead: number; onSelect: (s: { start: number; end: number }) => void; onSeek: (t: number) => void;
  onStroke: (edits: Bag[]) => void; tool: string; timelineDuration: number; projectId: string; pitchId: string;
  sourceId: string; parameters: Bag; view: Bag; onView: (v: Bag) => void;
  light?: boolean; stale?: boolean; onTargetHash: (hash: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const palette = CANVAS_PALETTES[light ? "soft" : "classic"];
  const wheel = useRef<(event: WheelEvent) => void>(() => {});
  const [target, setTarget] = useState<PitchData | null>(null);
  const [targetKey, setTargetKey] = useState("");
  const [error, setError] = useState("");
  const [hover, setHover] = useState<{ t: number; note: number } | null>(null);
  const [localSelection, setLocalSelection] = useState<typeof selection | null>(null);
  const [stroke, setStroke] = useState<{ t: number; note: number }[]>([]);
  const drag = useRef<{ t: number; note: number }[] | null>(null);
  const dur = Math.max(.05, timelineDuration);
  const span = Math.min(dur, Math.max(.05, Number(view.span) || dur));
  const start = Math.max(0, Math.min(dur - span, Number(view.start) || 0));
  const end = start + span;
  const center = Number(view.center) || 60, range = Number(view.range) || 36;
  const lo = center - range / 2, hi = center + range / 2;
  const hop = data.hop / data.sample_rate;
  const key = JSON.stringify([projectId, pitchId, sourceId, parameters, edits, notes]);
  const currentTarget = key === targetKey ? target : null;
  useEffect(() => {
    let live = true;
    setError("");
    onTargetHash("");
    const timer = setTimeout(() => {
      post<PitchData>(`/projects/${projectId}/pitch-target`, { pitch_artifact_id: pitchId, source_artifact_id: sourceId,
        parameters, pitch_edits: edits, notes }).then(result => { if (live) { setTarget(result); setTargetKey(key); onTargetHash(result.target_hash); } })
        .catch(e => { if (live) setError(e.message); });
    }, 180);
    return () => { live = false; clearTimeout(timer); };
  }, [key]);
  const moveView = (at: number, length = span) => onView({ ...view, span: Math.max(.05, Math.min(dur, length)), start: Math.max(0, Math.min(dur - Math.min(dur, length), at)) });
  const zoom = (factor: number) => {
    const anchor = playhead >= start && playhead <= end ? playhead : (start + end) / 2;
    const next = Math.min(dur, Math.max(.05, span / factor));
    moveView(anchor - (anchor - start) / span * next, next);
  };
  wheel.current = e => {
    if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoom(e.deltaY < 0 ? 1.25 : .8); }
    else if (e.shiftKey) { e.preventDefault(); moveView(start + Math.sign(e.deltaY || e.deltaX) * span / 10); }
  };
  useEffect(() => {
    const c = canvas.current; if (!c) return;
    const handle = (event: WheelEvent) => wheel.current(event);
    c.addEventListener("wheel", handle, { passive: false });
    return () => c.removeEventListener("wheel", handle);
  }, []);
  const fitPitch = () => {
    const values: number[] = [];
    for (const d of [data, currentTarget, outputData]) d?.f0_hz.forEach((f, i) => {
      if (f && frameTime(d, i) >= start && frameTime(d, i) <= end) values.push(midi(f));
    });
    if (!values.length) return;
    const low = values.reduce((a, b) => Math.min(a, b)), high = values.reduce((a, b) => Math.max(a, b));
    onView({ ...view, center: Math.round((low + high) / 2), range: [12, 24, 36, 60, 84].find(v => v >= high - low + 4) || 84 });
  };
  const stats = useMemo(() => {
    if (!currentTarget) return null;
    let changed = 0, voiced = 0, sum = 0;
    currentTarget.f0_hz.forEach((f, i) => {
      const t = ticks(frameTime(currentTarget, i));
      if (t < selection.start || t >= selection.end) return;
      const original = data.f0_hz[i];
      if (f && original) { const cents = 1200 * Math.log2(f / original); if (Math.abs(cents) > .01) changed++; sum += cents; voiced++; }
      else if (!!f !== !!original) changed++;
    });
    return { changed, mean: voiced ? sum / voiced : 0 };
  }, [currentTarget, data, selection]);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const draw = () => {
      const r = c.getBoundingClientRect(), dpr = devicePixelRatio || 1;
      c.width = r.width * dpr; c.height = r.height * dpr;
      const ctx = c.getContext("2d")!; ctx.scale(dpr, dpr);
      const left = 48, top = 28, w = r.width - left, h = r.height - top;
      const x = (t: number) => left + (t - start) / span * w;
      const y = (n: number) => top + (hi - n) / range * h;
      ctx.font = "11px system-ui";
      for (let n = Math.ceil(lo); n <= hi; n++) {
        ctx.strokeStyle = n % 12 === 0 ? palette.majorGrid : palette.grid;
        ctx.beginPath(); ctx.moveTo(left, y(n)); ctx.lineTo(r.width, y(n)); ctx.stroke();
        if (range <= 24 || n % 3 === 0) { ctx.fillStyle = palette.text; ctx.fillText(noteName(n), 3, y(n) - 2); }
      }
      const ideal = span / Math.max(2, Math.floor(w / 100));
      const power = 10 ** Math.floor(Math.log10(ideal));
      const step = ([1, 2, 5, 10].find(v => v * power >= ideal) || 10) * power;
      for (let t = Math.ceil(start / step) * step; t <= end + .00001; t += step) {
        ctx.strokeStyle = palette.grid; ctx.beginPath(); ctx.moveTo(x(t), top); ctx.lineTo(x(t), r.height); ctx.stroke();
        ctx.fillStyle = palette.axis; ctx.fillText(time(Math.max(0, t)), Math.max(left, Math.min(r.width - 78, x(t) + 3)), 17);
      }
      ctx.save(); ctx.beginPath(); ctx.rect(left, top, w, h); ctx.clip();
      for (const [d, color] of [[data, palette.source], [currentTarget, palette.target], [outputData, palette.output]] as const) {
        if (!d) continue;
        ctx.strokeStyle = color; ctx.lineWidth = d === currentTarget ? 2 : 1.5; ctx.beginPath();
        let pen = false;
        const first = Math.max(0, Math.floor(((start - seconds(d.timeline_offset_ticks || 0)) * d.sample_rate - d.frame_center_offset) / d.hop) - 1);
        for (let i = first; i < d.f0_hz.length; i++) {
          const t = frameTime(d, i); if (t > end + d.hop / d.sample_rate) break;
          const f = d.f0_hz[i]; if (!f) { pen = false; continue; }
          if (pen) ctx.lineTo(x(t), y(midi(f))); else ctx.moveTo(x(t), y(midi(f))); pen = true;
        }
        ctx.stroke();
      }
      for (const note of notes) { ctx.fillStyle = palette.notes; ctx.fillRect(x(seconds(note.start)), y(note.pitch_midi_float) - 4, x(seconds(note.end)) - x(seconds(note.start)), 8); }
      const selected = localSelection || selection;
      ctx.fillStyle = palette.selection; ctx.fillRect(x(seconds(selected.start)), top, (seconds(selected.end - selected.start) / span) * w, h);
      ctx.strokeStyle = palette.selectionEdge;
      for (const edge of [selected.start, selected.end]) { ctx.beginPath(); ctx.moveTo(x(seconds(edge)), top); ctx.lineTo(x(seconds(edge)), r.height); ctx.stroke(); }
      ctx.strokeStyle = palette.playhead; ctx.beginPath(); ctx.moveTo(x(playhead), top); ctx.lineTo(x(playhead), r.height); ctx.stroke();
      if (stroke.length && tool === 'erase') {
        const first = Math.min(...stroke.map(point => point.t));
        const last = Math.max(...stroke.map(point => point.t));
        ctx.fillStyle = light ? '#b3384e33' : '#ff859644';
        ctx.fillRect(x(first), top, Math.max(3, x(last + hop) - x(first)), h);
      } else if (stroke.length) { ctx.strokeStyle = palette.stroke; ctx.lineWidth = 3; ctx.beginPath(); stroke.forEach((p, i) => i ? ctx.lineTo(x(p.t), y(p.note)) : ctx.moveTo(x(p.t), y(p.note))); ctx.stroke(); }
      ctx.restore();
    };
    draw(); const ro = new ResizeObserver(draw); ro.observe(c); return () => ro.disconnect();
  }, [data, currentTarget, outputData, notes, selection, localSelection, start, span, hi, lo, range, playhead, stroke, palette, tool, light, hop]);
  const point = (clientX: number, clientY: number) => {
    const r = canvas.current!.getBoundingClientRect();
    return { t: Math.max(start, Math.min(end, start + (clientX - r.left - 48) / (r.width - 48) * span)),
      note: Math.max(midi(20), Math.min(midi(2000), hi - (clientY - r.top - 28) / (r.height - 28) * range)) };
  };
  const finishStroke = (points: { t: number; note: number }[]) => {
    const index = (t: number) => Math.round((t - frameTime(data, 0)) / hop);
    if (tool === 'erase') {
      let first = data.f0_hz.length;
      let last = -1;
      for (const point of points) {
        const frame = index(point.t);
        first = Math.min(first, frame);
        last = Math.max(last, frame);
      }
      first = Math.max(0, first);
      last = Math.min(data.f0_hz.length - 1, last);
      if (last < first) return;
      onStroke([{ start_tick: Math.max(0, ticks(frameTime(data, first))),
        end_tick: Math.max(0, ticks(frameTime(data, last))) + Math.max(1, ticks(hop)),
        operation: 'unvoiced' }]);
      return;
    }
    const frames = new Map<number, number>();
    for (let p = 0; p < points.length; p++) {
      const a = points[Math.max(0, p - 1)], b = points[p], ai = index(a.t), bi = index(b.t);
      for (let i = Math.max(0, Math.min(ai, bi)); i <= Math.min(data.f0_hz.length - 1, Math.max(ai, bi)); i++) frames.set(i, ai === bi ? b.note : a.note + (b.note - a.note) * (i - ai) / (bi - ai));
    }
    if (!frames.size) return;
    onStroke([...frames].sort((a, b) => a[0] - b[0]).map(([i, n]) => ({ start_tick: Math.max(0, ticks(frameTime(data, i))),
      end_tick: Math.max(0, ticks(frameTime(data, i))) + Math.max(1, ticks(hop)), operation: "set_hz", hz: +hzAt(n).toFixed(3) })));
  };
  return <div className="pitch-editor">
    <div className="pitch-navigation">
      <div className="row wrap">
        <button className="button small" aria-label="时间轴缩小" onClick={() => zoom(.5)}>−</button>
        <button className="button small" aria-label="时间轴放大" onClick={() => zoom(2)}>＋</button>
        <button className="button small" onClick={() => moveView(0, dur)}>全曲</button>
        <button className="button small" disabled={selection.end <= selection.start} onClick={() => { const length = Math.max(.1, seconds(selection.end - selection.start)); moveView(seconds(selection.start) - length * .05, length * 1.1); }}>缩放到选区</button>
        <button className="button small" onClick={() => moveView(playhead - span / 2)}>定位播放头</button>
        <button className="button small" onClick={fitPitch}>适配音域</button>
        <span className="mini-note">{time(start)}–{time(end)} · {(dur / span).toFixed(1)}× · 每帧 {(hop * 1000).toFixed(0)} ms</span>
      </div>
      <div className="pitch-view-fields">
        <label>窗口起点（秒）<input aria-label="音高窗口起点" type="number" min="0" max={dur - span} step="0.01" value={+start.toFixed(3)} onChange={e => moveView(+e.target.value)} /></label>
        <label>窗口长度（秒）<input aria-label="音高窗口长度" type="number" min="0.05" max={dur} step="0.1" value={+span.toFixed(3)} onChange={e => moveView(start, +e.target.value)} /></label>
        <label>音域范围<select aria-label="音域范围" value={range} onChange={e => onView({ ...view, range: +e.target.value })}><option value={12}>1 个八度</option><option value={24}>2 个八度</option><option value={36}>3 个八度</option><option value={60}>5 个八度</option><option value={84}>7 个八度</option></select></label>
        <label>音域中心（MIDI）<input aria-label="音域中心" type="number" min={24} max={96} value={center} onChange={e => onView({ ...view, center: Math.max(24, Math.min(96, +e.target.value)) })} /></label>
      </div>
      <label className="pitch-pan"><span>时间轴平移</span><button className="quiet-button" aria-label="向前平移时间轴" onClick={() => moveView(start - span / 2)}>‹</button><input aria-label="时间轴平移" type="range" min={0} max={Math.max(0, dur - span)} step={hop} value={start} disabled={span >= dur} onChange={e => moveView(+e.target.value)} /><button className="quiet-button" aria-label="向后平移时间轴" onClick={() => moveView(start + span / 2)}>›</button></label>
    </div>
    <canvas ref={canvas} className={`pitch-canvas ${tool === 'erase' ? 'pitch-eraser' : ''}`} tabIndex={0} aria-label="音高时间轴：拖动框选、绘制或擦除，左右键移动播放头，加减键缩放" role="application"
      onKeyDown={e => { if (["ArrowLeft", "ArrowRight", "+", "=", "-"].includes(e.key)) { e.preventDefault(); e.stopPropagation(); if (e.key === "-") zoom(.5); else if (["+", "="].includes(e.key)) zoom(2); else onSeek(Math.max(0, Math.min(dur, playhead + (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? .1 : hop)))); } }}
      onPointerDown={e => { if (e.button !== 0) return; const p = point(e.clientX, e.clientY); if (e.clientY - e.currentTarget.getBoundingClientRect().top < 28) { onSeek(p.t); return; } drag.current = [p]; e.currentTarget.setPointerCapture(e.pointerId); if (tool !== "select") setStroke([p]); }}
      onPointerMove={e => { const p = point(e.clientX, e.clientY); setHover(p); if (drag.current) { if (tool !== "select") { drag.current.push(p); setStroke([...drag.current]); } else setLocalSelection({ start: ticks(Math.min(drag.current[0].t, p.t)), end: ticks(Math.max(drag.current[0].t, p.t)) }); } }}
      onPointerUp={e => { if (!drag.current) return; const p = point(e.clientX, e.clientY); if (tool !== "select") finishStroke([...drag.current, p]); else if (Math.abs(p.t - drag.current[0].t) < hop / 2) onSeek(p.t); else onSelect({ start: ticks(Math.min(drag.current[0].t, p.t)), end: ticks(Math.max(drag.current[0].t, p.t)) }); drag.current = null; setStroke([]); setLocalSelection(null); }}
      onPointerCancel={() => { drag.current = null; setStroke([]); setLocalSelection(null); }} onPointerLeave={() => setHover(null)} />
    <div className="pitch-legend"><span><i style={{ background: palette.source }} />原始检测</span><span><i style={{ background: palette.target }} />{currentTarget ? stale ? "旧检测上的目标预览 · 需重新检测" : "实际转换目标 F0" : "目标计算中…"}</span>{outputData && <span><i style={{ background: palette.output }} />已选候选输出复测</span>}</div>
    <div className="pitch-readout" role="status">{error ? `目标计算失败：${error}` : stats ? `选区 ${time(seconds(selection.start))}–${time(seconds(selection.end))} · ${stats.changed} 帧发生变化 · 有声帧平均 ${stats.mean >= 0 ? "+" : ""}${stats.mean.toFixed(1)} 音分` : "正在计算与转换一致的目标曲线…"}{hover && <span>光标 {time(hover.t)} · {noteName(hover.note)} · {hzAt(hover.note).toFixed(1)} Hz</span>}</div>
    <p className="info-inline">点击时间尺定位，拖动框选；绘制可改写曲线，擦除可将拖过的时间段设为无音高。转换时按当前 Edit 的静音开关、最小区间和平滑长度处理，整笔操作可撤销。</p>
  </div>;
}

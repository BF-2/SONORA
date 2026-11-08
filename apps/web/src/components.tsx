import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { request } from "./api";
import { clock, duration, seconds, ticks } from "./types";
import type { Artifact, Bag } from "./types";
import { CANVAS_PALETTES } from "./canvasPalette";

const paths: Record<string, ReactNode> = {
  wave: <path d="M3 10v4m4-8v12m5-16v20m5-17v14m4-9v4" />,
  import: <path d="M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5" />,
  layers: <path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" />,
  note: (
    <>
      <path d="M9 18V5l12-2v13M9 9l12-2" />
      <ellipse cx="5.5" cy="18" rx="3.5" ry="3" />
      <ellipse cx="17.5" cy="16" rx="3.5" ry="3" />
    </>
  ),
  spark: (
    <path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z" />
  ),
  sliders: (
    <path d="M5 3v8m0 4v6M12 3v2m0 4v12M19 3v12m0 4v2M2 11h6M9 5h6m4 10h3m-6 0h3" />
  ),
  export: <path d="M12 16V3m-4 4 4-4 4 4M4 14v7h16v-7" />,
  play: <path d="m8 4 13 8-13 8V4Z" />,
  pause: <path d="M8 4v16M16 4v16" />,
  check: <path d="m5 12 4 4L19 6" />,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  undo: <path d="M3 9h11a6 6 0 1 1 0 12M8 4 3 9l5 5" />,
  redo: <path d="M21 9H10a6 6 0 1 0 0 12m6-17 5 5-5 5" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 6v6l4 3" />
    </>
  ),
  box: <path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 10 9-5M3 8l9 5v9M7 5.5l10 5.5" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7v1" />
    </>
  ),
  close: <path d="m6 6 12 12M18 6 6 18" />,
  repeat: <path d="M5 7h13l-3-3m3 3-3 3M19 17H6l3 3m-3-3 3-3M3 14V9m18 1v5" />,
  mic: (
    <>
      <rect x="8" y="2" width="8" height="13" rx="4" />
      <path d="M5 11v2a7 7 0 0 0 14 0v-2M12 20v3M8 23h8" />
    </>
  ),
  branch: (
    <>
      <circle cx="6" cy="4" r="2" />
      <circle cx="6" cy="20" r="2" />
      <circle cx="19" cy="5" r="2" />
      <path d="M6 6v12M6 13h7a6 6 0 0 0 6-6" />
    </>
  ),
  save: <path d="M4 3h13l4 4v14H3V3h1ZM7 3v6h9V3M7 21v-8h10v8" />,
  headphones: (
    <path d="M4 14v-3a8 8 0 0 1 16 0v3M4 12H2v8h5v-8H4Zm16 0h2v8h-5v-8h3Z" />
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3h3l2 3 3-1 1-3 3-1 2-3-2-2 1-3-2-3h-3l-2-3-3 1Z" />
    </>
  ),
};
export function Icon({ name, ...props }: { name: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" {...props}>
      {paths[name] || paths.wave}
    </svg>
  );
}
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return (
    <span className={`badge ${tone}`}>
      <span className="pill-dot" />
      {children}
    </span>
  );
}
export function Empty({
  title,
  children,
  icon = "wave",
}: {
  title: string;
  children?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="empty">
      <Icon name={icon} />
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  );
}
export function Card({
  title,
  icon,
  aside,
  children,
  className = "",
}: {
  title?: string;
  icon?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {title && (
        <div className="card-top">
          <h2 className="card-title">
            {icon && <Icon name={icon} />} {title}
          </h2>
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  suffix = "",
  onChange,
  hint,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (v: number) => void;
  hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <div className="slider-with-value">
        <input
          aria-label={label}
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(+e.target.value)}
        />
        <input
          className="numeric"
          aria-label={`${label}数值`}
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) =>
            onChange(Math.min(max, Math.max(min, +e.target.value)))
          }
        />
        <span>{suffix}</span>
      </div>
    </Field>
  );
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const active = document.activeElement as HTMLElement;
    ref.current?.showModal();
    return () => {
      active?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="modal-top">
        <h2>{title}</h2>
        <button className="icon-button" aria-label="关闭弹窗" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}

export function Waveform({
  artifact,
  selection,
  playhead,
  onSelect,
  onSeek,
  color = "#c5eb85",
  zoom = 1,
  timelineDuration,
  offset = 0,
  light = false,
}: {
  artifact: Artifact;
  selection: { start: number; end: number };
  playhead: number;
  onSelect: (s: { start: number; end: number }) => void;
  onSeek: (s: number) => void;
  color?: string;
  zoom?: number;
  timelineDuration?: number;
  offset?: number;
  light?: boolean;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [peaks, setPeaks] = useState<number[][]>([]);
  const [error, setError] = useState("");
  const [local, setLocal] = useState<{ start: number; end: number } | null>(
    null,
  );
  const drag = useRef<number | null>(null);
  const clipDuration = duration(artifact);
  const dur = Math.max(0.001, timelineDuration || clipDuration + offset);
  const palette = CANVAS_PALETTES[light ? "soft" : "classic"];
  useEffect(() => {
    let live = true;
    setPeaks([]);
    setError("");
    request<Bag>(`/artifacts/${artifact.id}/waveform`)
      .then((d) => {
        if (live) setPeaks(d.peaks || d.levels?.[0]?.peaks || []);
      })
      .catch((e) => {
        if (live) setError(e.message);
      });
    return () => {
      live = false;
    };
  }, [artifact.id]);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const draw = () => {
      const r = c.getBoundingClientRect(),
        dpr = devicePixelRatio || 1;
      c.width = r.width * dpr;
      c.height = r.height * dpr;
      const ctx = c.getContext("2d")!;
      ctx.scale(dpr, dpr);
      const w = r.width,
        h = r.height;
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = palette.grid;
      ctx.lineWidth = 1;
      for (let i = 0; i <= 10; i++) {
        ctx.beginPath();
        ctx.moveTo((w * i) / 10, 0);
        ctx.lineTo((w * i) / 10, h);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, (w / Math.max(1, peaks.length)) * 0.7);
      const step = Math.max(1, Math.floor(peaks.length / w));
      for (let i = 0; i < peaks.length; i += step) {
        const v = peaks[i];
        const lo = Array.isArray(v) ? v[0] : -Math.abs(Number(v));
        const hi = Array.isArray(v) ? v[1] : Math.abs(Number(v));
        ctx.beginPath();
        const x = ((offset + (i / peaks.length) * clipDuration) / dur) * w;
        ctx.moveTo(x, h / 2 - lo * h * 0.43);
        ctx.lineTo(x, h / 2 - hi * h * 0.43);
        ctx.stroke();
      }
      const s = local || selection;
      const x = (seconds(s.start) / dur) * w,
        end = (seconds(s.end) / dur) * w;
      ctx.fillStyle = palette.selection;
      ctx.fillRect(x, 0, end - x, h);
      ctx.strokeStyle = palette.selectionEdge;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.moveTo(end, 0);
      ctx.lineTo(end, h);
      ctx.stroke();
      const p = (playhead / dur) * w;
      ctx.strokeStyle = palette.playhead;
      ctx.beginPath();
      ctx.moveTo(p, 0);
      ctx.lineTo(p, h);
      ctx.stroke();
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(c);
    return () => ro.disconnect();
  }, [
    peaks,
    selection,
    local,
    playhead,
    color,
    dur,
    zoom,
    offset,
    clipDuration,
    palette,
  ]);
  const xAt = (clientX: number) => {
    const r = canvas.current!.getBoundingClientRect();
    return Math.max(0, Math.min(dur, ((clientX - r.left) / r.width) * dur));
  };
  return (
    <div className="wave-scroll">
      <div style={{ width: `${zoom * 100}%` }}>
        <div className="wave-ruler">
          {Array.from({ length: 6 }, (_, i) => (
            <span key={i}>{clock((dur * i) / 5)}</span>
          ))}
        </div>
        <canvas
          ref={canvas}
          className="wave-canvas"
          role="slider"
          aria-label={`${artifact.name} 波形；左右键移动播放头，拖动选择区间`}
          aria-valuemin={0}
          aria-valuemax={dur}
          aria-valuenow={playhead}
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
              e.preventDefault();
              onSeek(
                Math.min(
                  dur,
                  Math.max(
                    0,
                    playhead +
                      (e.key === "ArrowRight" ? 1 : -1) *
                        (e.shiftKey ? 1 : 0.1),
                  ),
                ),
              );
            }
          }}
          onPointerDown={(e) => {
            drag.current = xAt(e.clientX);
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (drag.current !== null) {
              const a = drag.current,
                b = xAt(e.clientX);
              setLocal({
                start: ticks(Math.min(a, b)),
                end: ticks(Math.max(a, b)),
              });
            }
          }}
          onPointerUp={(e) => {
            if (drag.current !== null) {
              const a = drag.current,
                b = xAt(e.clientX);
              if (Math.abs(a - b) < 0.04) onSeek(b);
              else
                onSelect({
                  start: ticks(Math.min(a, b)),
                  end: ticks(Math.max(a, b)),
                });
              drag.current = null;
              setLocal(null);
            }
          }}
          onPointerCancel={() => {
            drag.current = null;
            setLocal(null);
          }}
        />
        {error && <small className="error-text">{error}</small>}
      </div>
    </div>
  );
}

export { PitchCanvas } from "./PitchEditor";

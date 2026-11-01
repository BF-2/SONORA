import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { ModelSelector, CatalogManager, versionLabel } from "./ModelSelector";
import { RvcParameterPanel } from "./RvcParameterPanel";
import type { RvcCatalog } from "./RvcParameterPanel";
import { SeparationPanel } from "./SeparationPanel";
import { SeparationVersions } from "./SeparationVersions";
import { CandidateVersions } from "./CandidateVersions";
import { WorkflowStrip } from "./WorkflowStrip";
import {
  ApiError,
  download,
  initConnection,
  jsonDownload,
  media,
  patch,
  post,
  request,
  setConnection,
  getConnectionEndpoint,
} from "./api";
import {
  Badge,
  Card,
  Empty,
  Field,
  Icon,
  Modal,
  PitchCanvas,
  Slider,
  Waveform,
} from "./components";
import {
  activeJob,
  clock,
  duration,
  seconds,
  stateLabel,
  ticks,
  uid,
} from "./types";
import type {
  Artifact,
  Bag,
  Engine,
  Job,
  Model,
  PitchData,
  Project,
  Track,
  VoicePartKey,
} from "./types";

const steps = [
  [
    "import",
    "导入诊断",
    "从一条音频，开始新的声音。",
    "导入素材，选择任务。原始文件始终保留。",
  ],
  [
    "layers",
    "人声整理",
    "分开处理，保留每一层。",
    "先确认素材与声部，再让模型专注于声音。",
  ],
  [
    "note",
    "音高与歌词",
    "保留表达，修好值得修的地方。",
    "检测、目标编辑与实际输出分别保存。",
  ],
  [
    "spark",
    "声音转换",
    "找到属于这段作品的声音。",
    "每个新候选默认从同一干净源生成。",
  ],
  [
    "sliders",
    "混音",
    "让每一层，各得其所。",
    "人声、伴奏与空间，共同组成最后的作品。",
  ],
  [
    "export",
    "导出",
    "把这一版，带到工作台之外。",
    "验证、导出，并保留可继续编辑的工程。",
  ],
];
const roleNames: Record<string, string> = {
  original: "原始素材",
  vocal: "人声",
  vocals: "人声",
  instrumental: "伴奏",
  guide: "新词导唱",
  converted: "转换人声",
  mix: "最终混音",
  pitch: "音高分析",
  harmony: "和声",
  synthesized: "合成新演唱",
  lead_vocal: "主唱",
  backing_vocal: "和声",
};
const kindNames: Record<string, string> = {
  decode: "解码素材",
  dsp: "音频处理",
  model_inference: "模型推理",
  demo: "合成技术样本",
};
function normal(p: Project): Project {
  return {
    ...p,
    timebase: p.timebase || 48000,
    assets: p.assets || [],
    tracks: p.tracks || [],
    candidates: p.candidates || [],
    parameters: p.parameters || {},
    selection: p.selection || { start: 0, end: 0 },
    pitch_edits: p.pitch_edits || [],
    lyrics: p.lyrics || [],
    notes: p.notes || [],
    exports: p.exports || [],
    warnings: p.warnings || [],
    graph: p.graph || [],
    mode: p.mode === "detail" ? "expert" : p.mode || "quick",
  };
}
function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}
function stalePitchJob(j: Job) {
  return (
    j.error?.code === "stale_pitch_binding" ||
    !!j.error?.message.includes("指定曲线是旧分析")
  );
}
function mergeEditorPatch(base: Bag, delta: Bag): Bag {
  const merged = { ...base, ...delta };
  if (Array.isArray(base.candidates) && Array.isArray(delta.candidates))
    merged.candidates = [
      ...base.candidates.map((candidate: Bag) => ({ ...candidate, ...delta.candidates.find((change: Bag) => change.id === candidate.id) })),
      ...delta.candidates.filter((change: Bag) => !base.candidates.some((candidate: Bag) => candidate.id === change.id)),
    ];
  if (delta.voice_parts) {
    merged.voice_parts = { ...base.voice_parts };
    for (const [part, raw] of Object.entries(delta.voice_parts)) {
      const change = raw as Bag,
        previous = base.voice_parts?.[part] || {};
      merged.voice_parts[part] = {
        ...previous,
        ...change,
        ...(change.parameters
          ? { parameters: { ...previous.parameters, ...change.parameters } }
          : {}),
      };
    }
  }
  if (delta.track_updates) {
    merged.track_updates = { ...base.track_updates };
    for (const [id, raw] of Object.entries(delta.track_updates))
      merged.track_updates[id] = {
        ...base.track_updates?.[id],
        ...(raw as Bag),
      };
    if (Array.isArray(base.tracks))
      merged.tracks = base.tracks.map((t: Track) =>
        delta.track_updates[t.id] ? { ...t, ...delta.track_updates[t.id] } : t,
      );
  }
  if (delta.view_preferences)
    merged.view_preferences = {
      ...base.view_preferences,
      ...delta.view_preferences,
    };
  if (delta.model_parameter_updates) {
    merged.model_parameter_updates = { ...base.model_parameter_updates };
    merged.model_bindings = { ...base.model_bindings };
    for (const scope of ["tracks", "clips"])
      if (delta.model_parameter_updates[scope]) {
        merged.model_parameter_updates[scope] = {
          ...base.model_parameter_updates?.[scope],
        };
        merged.model_bindings[scope] = { ...base.model_bindings?.[scope] };
        for (const [id, raw] of Object.entries(
          delta.model_parameter_updates[scope],
        )) {
          const changes = raw as Bag,
            oldDelta = base.model_parameter_updates?.[scope]?.[id] || {},
            entry = base.model_bindings?.[scope]?.[id] || {};
          merged.model_parameter_updates[scope][id] = {
            ...oldDelta,
            ...changes,
            parameter_sources: {
              ...oldDelta.parameter_sources,
              ...changes.parameter_sources,
            },
          };
          merged.model_bindings[scope][id] = {
            ...entry,
            parameters: {
              ...entry.parameters,
              ...changes,
              parameter_sources: {
                ...entry.parameters?.parameter_sources,
                ...changes.parameter_sources,
              },
            },
          };
        }
      }
  }
  return merged;
}
function sourceForProject(p: Project): Artifact | undefined {
  if (p.parameters.source_artifact_id)
    return p.assets.find(
      (a) => a.id === p.parameters.source_artifact_id && isAudio(a),
    );
  const roles =
    p.task === "rewrite"
      ? ["guide", "synthesized"]
      : ["vocal", "vocals", "guide", "synthesized"];
  return (
    p.assets
      .slice()
      .reverse()
      .find((a) => roles.includes(a.stem_role || "") && isAudio(a)) ||
    (p.task === "speech" ||
    (p.task !== "rewrite" && p.parameters.skip_separation)
      ? p.assets.find((a) => a.stem_role === "original" && isAudio(a))
      : undefined)
  );
}
function runtimePins(value: Bag = {}) {
  return Object.fromEntries(
    [
      "model_id",
      "model_artifact_id",
      "model_hash",
      "voice_id",
      "run_id",
      "index_id",
      "index_hash",
      "speaker_id",
      "preset_id",
    ]
      .filter((k) => value[k] !== undefined)
      .map((k) => [k, value[k]]),
  );
}
function editorContext(
  p: Project | null,
  part: string,
  ignoreClips = true,
): {
  parameters: Bag;
  scope: string;
  targetId?: string;
  track?: Track;
  source?: Artifact;
  clip?: Bag;
} {
  if (!p) return { parameters: {}, scope: "project" };
  if (part !== "vocal") {
    const b = p.voice_parts?.[part as VoicePartKey];
    return {
      parameters: {
        ...Object.fromEntries(
          ["device", "seed", "cpu_threads"]
            .filter((key) => p.parameters[key] !== undefined)
            .map((key) => [key, p.parameters[key]]),
        ),
        ...b?.parameters,
      },
      scope: "voice_part",
      targetId: part,
      source: p.assets.find((a) => a.id === b?.source_artifact_id),
    };
  }
  const source = sourceForProject(p);
  const track =
    p.tracks.find(
      (t) => t.id === p.parameters.track_id && t.artifact_id === source?.id,
    ) || p.tracks.find((t) => t.artifact_id === source?.id);
  let parameters = { ...p.parameters },
    scope = "project",
    targetId: string | undefined;
  const trackBinding = track ? p.model_bindings?.tracks?.[track.id] : null;
  if (trackBinding) {
    parameters = {
      ...parameters,
      ...trackBinding.parameters,
      ...runtimePins(trackBinding.selection),
    };
    scope = "track";
    targetId = track!.id;
  }
  const clip = p.parameters.clip_id
    ? p.model_bindings?.clips?.[p.parameters.clip_id]
    : undefined;
  if (
    clip &&
    !ignoreClips &&
    !p.parameters.ignore_clip_bindings &&
    clip.source_artifact_id === source?.id
  ) {
    parameters = {
      ...parameters,
      ...clip.parameters,
      ...runtimePins(clip.selection),
      range_ticks: clip.range_ticks,
    };
    scope = "clip";
    targetId = p.parameters.clip_id;
  }
  return { parameters, scope, targetId, track, source, clip };
}
function isAudio(a: Artifact) {
  return (
    a.sample_count > 0 &&
    !String(a.media_type).includes("json") &&
    a.stem_role !== "pitch"
  );
}

export default function App() {
  const [uiTheme, setUiTheme] = useState(() =>
    localStorage.getItem("sonora-ui-theme") === "soft" ? "soft" : "classic",
  );
  const [projectSearch, setProjectSearch] = useState("");
  const [blindLabels, setBlindLabels] = useState<Record<string, string> | null>(
    null,
  );
  const [showHistoricalTracks, setShowHistoricalTracks] = useState(false);
  useEffect(() => {
    document.documentElement.dataset.ui = uiTheme;
    localStorage.setItem("sonora-ui-theme", uiTheme);
  }, [uiTheme]);
  const [connection, setConnectionState] = useState("connecting");
  const [project, setProject] = useState<Project | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [device, setDevice] = useState<Bag>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [step, setStepState] = useState(0);
  const [catalogRevision, setCatalogRevision] = useState(0);
  const [rvcCatalog, setRvcCatalog] = useState<RvcCatalog | null>(null);
  const [workflow, setWorkflow] = useState<Bag | null>(null);
  const [managerScope, setManagerScope] = useState<VoicePartKey | "vocal">(
    "vocal",
  );
  const [modal, setModal] = useState("");
  const [error, setError] = useState("");
  const [notices, setNotices] = useState<{ id: string; text: string }[]>([]);
  const [toastPaused, setToastPaused] = useState(false);
  const [busy, setBusy] = useState("");
  const [saveState, setSaveState] = useState("已保存");
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [volume, setVolume] = useState(0.75);
  const [ab, setAB] = useState<"A" | "B">("A");
  const [listenId, setListenId] = useState("");
  const [auditionCandidateIds, setAuditionCandidateIds] = useState<
    Record<string, string>
  >({});
  const [matched, setMatched] = useState(true);
  const [zoom, setZoomState] = useState(1);
  const [pitchTab, setPitchTab] = useState("pitch");
  const [loadedPitchData, setPitchData] = useState<PitchData | null>(null);
  const [loadedOutputPitchData, setOutputPitchData] =
    useState<PitchData | null>(null);
  const [relinkHash, setRelinkHash] = useState("");
  const [pitchTool, setPitchTool] = useState("select");
  const [previewTargetHash, setPreviewTargetHash] = useState("");
  const seenCandidate = useRef({ scope: "", id: "" });
  const [targetHz, setTargetHz] = useState(220);
  const [semitones, setSemitones] = useState(0);
  const [lock, setLock] = useState(true);
  const [selectedTrack, setSelectedTrack] = useState("");
  const [format, setFormat] = useState("wav");
  const [wavSubtype, setWavSubtype] = useState("pcm_s24le");
  const [mixPreview, setMixPreview] = useState<Bag | null>(null);
  const [mixPreviewState, setMixPreviewState] = useState("");
  const [mixMonitor, setMixMonitor] = useState("");
  const [abMeasurements, setAbMeasurements] = useState<Bag | null>(null);
  const [exportMeasurements, setExportMeasurements] = useState<Bag | null>(
    null,
  );
  const [meterDb, setMeterDb] = useState(-120);
  const [stemSemantics, setStemSemantics] = useState("post_fader_fx");
  const [exportSelection, setExportSelection] = useState(false);
  const [exportId, setExportId] = useState("");
  const [exportName, setExportName] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [endpointDraft, setEndpointDraft] = useState(getConnectionEndpoint());
  const [harmonySourceId, setHarmonySourceId] = useState("");
  const [harmonyModelId, setHarmonyModelId] = useState("");
  const [pathDraft, setPathDraft] = useState("");
  const [newName, setNewName] = useState("新的声音作品");
  const [recording, setRecording] = useState(false);
  const pRef = useRef<Project | null>(null);
  const pending = useRef<Bag>({});
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const saveChain = useRef<Promise<void>>(Promise.resolve());
  const audio = useRef<HTMLAudioElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  const recipeInput = useRef<HTMLInputElement>(null);
  const lyricInput = useRef<HTMLInputElement>(null);
  const midiInput = useRef<HTMLInputElement>(null);
  const roleRef = useRef("original");
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const playRef = useRef(0);
  const eventCursor = useRef(0);
  const refreshNeeded = useRef(false);
  const jobSignature = useRef("");
  const mediaCache = useRef(new Map<string, Promise<string>>());
  const measurementCache = useRef(new Map<string, Promise<Bag>>());
  const previewRun = useRef(0);
  const loopPending = useRef<ReturnType<typeof setTimeout> | null>(null);
  const audioGraph = useRef<{ context: AudioContext; gain: GainNode; analyser: AnalyserNode } | null>(null);
  const applyProject = useCallback((p: Project) => {
    const n = normal(p);
    if (pRef.current?.id === n.id && pRef.current.revision > n.revision) return;
    if (pRef.current?.id !== n.id) {
      if (audio.current) audio.current.dataset.projectId = "";
      audio.current?.pause();
      if (audio.current) { audio.current.removeAttribute("src"); audio.current.load(); }
      if (loopPending.current) clearTimeout(loopPending.current);
      loopPending.current = null;
      previewRun.current++;
      for (const url of mediaCache.current.values()) void url.then(value => URL.revokeObjectURL(value)).catch(() => {});
      mediaCache.current.clear(); measurementCache.current.clear();
      setPlaying(false); setLoop(false); setListenId(""); setAB("A");
      setMixPreview(null); setMixMonitor(""); setExportId(""); setSelectedTrack("");
      const end = Math.max(0, n.parameters.timeline_duration_ticks || 0,
        ...n.assets.filter(isAudio).map(a => ticks(duration(a)) + (n.tracks.find(t => t.artifact_id === a.id)?.offset_ticks ?? a.timeline_offset_ticks ?? 0)),
        ...n.notes.map(note => note.end));
      n.playhead = Math.max(0, Math.min(n.playhead || 0, end));
      playRef.current = seconds(n.playhead); setPlayhead(playRef.current);
      setStepState(n.view_preferences?.stage ?? 0);
      setZoomState(n.view_preferences?.zoom ?? 1);
      setWorkflow(null);
    }
    pRef.current = n;
    setProject(n);
    setProjects(previous => [n, ...previous.filter(item => item.id !== n.id)]);
    localStorage.setItem("sonora-project", n.id);
  }, []);
  const tell = useCallback((msg: string) => {
    setNotices(previous => [...previous, { id: uid(), text: msg }].slice(-5));
  }, []);
  useEffect(() => {
    if (!notices.length || toastPaused) return;
    const timer = setTimeout(() => setNotices(items => items.slice(1)), 5500);
    return () => clearTimeout(timer);
  }, [notices[0]?.id, toastPaused]);
  const guard = useCallback(async (fn: () => Promise<unknown>, label = "") => {
    if (label) setBusy(label);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 401)
        setConnectionState("unauthorized");
    } finally {
      if (label) setBusy("");
    }
  }, []);
  const flush = useCallback(async () => {
    clearTimeout(saveTimer.current);
    saveChain.current = saveChain.current
      .catch(() => {})
      .then(async () => {
        const current = pRef.current;
        if (!current || !Object.keys(pending.current).length) return;
        const changes = { ...pending.current };
        pending.current = {};
        setSaveState("正在保存");
        try {
          let result: Project;
          try {
            result = await patch<Project>(`/projects/${current.id}`, {
              revision: current.revision,
              patch: changes,
            });
          } catch (e) {
            if (e instanceof ApiError && e.status === 409) {
              const latest = await request<Project>(`/projects/${current.id}`);
              result = await patch<Project>(`/projects/${current.id}`, {
                revision: latest.revision,
                patch: changes,
              });
            } else throw e;
          }
          applyProject(mergeEditorPatch(result, pending.current) as Project);
          setSaveState(
            Object.keys(pending.current).length ? "待保存" : "已保存",
          );
        } catch (e) {
          pending.current = mergeEditorPatch(changes, pending.current);
          setSaveState("保存失败");
          setError(errorText(e));
          throw e;
        }
      });
    return saveChain.current;
  }, [applyProject]);
  const update = useCallback(
    (changes: Bag) => {
      if (!pRef.current) return;
      const part = pRef.current.parameters.active_voice_part as VoicePartKey;
      const binding = pRef.current.voice_parts?.[part];
      if (binding) {
        const scoped: Bag = {};
        for (const key of ["pitch_edits", "notes", "selected_candidate_id"])
          if (key in changes) {
            scoped[key] = changes[key];
            delete changes[key];
          }
        if (Object.keys(scoped).length)
          changes.voice_parts = {
            ...changes.voice_parts,
            [part]: { ...changes.voice_parts?.[part], ...scoped },
          };
      }
      pending.current = mergeEditorPatch(pending.current, changes);
      applyProject(mergeEditorPatch(pRef.current, changes) as Project);
      setSaveState("待保存");
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        void flush().catch(() => {});
      }, 450);
    },
    [applyProject, flush],
  );
  const globalParam = (changes: Bag) =>
    update({ parameters: { ...pRef.current?.parameters, ...changes } });
  const updateView = (changes: Bag) => {
    if (pRef.current)
      update({
        view_preferences: { ...pRef.current.view_preferences, ...changes },
      });
  };
  const setStep = (value: number) => {
    audio.current?.pause(); setListenId(""); setMixMonitor("");
    setStepState(value);
    updateView({ stage: value });
  };
  const setZoom = (value: number | ((old: number) => number)) => {
    const next = typeof value === "function" ? value(zoom) : value;
    setZoomState(next);
    updateView({ zoom: next });
  };
  const patchPart = (part: VoicePartKey, changes: Bag) => {
    if (pRef.current?.voice_parts?.[part])
      update({ voice_parts: { [part]: changes } });
  };
  const param = (changes: Bag, source = "user_override", presetId?: string) => {
    const part = pRef.current?.parameters.active_voice_part as VoicePartKey,
      binding = pRef.current?.voice_parts?.[part];
    const context = editorContext(pRef.current, binding ? part : "vocal");
    const sources = {
      ...context.parameters.parameter_sources,
      ...Object.fromEntries(Object.keys(changes).map((k) => [k, source])),
    };
    const patchValues = {
      ...changes,
      parameter_sources: sources,
      ...(presetId ? { preset_id: presetId } : {}),
    };
    if (binding) patchPart(part, { parameters: patchValues });
    else if (
      context.scope !== "project" &&
      context.targetId &&
      Object.keys(changes).every(
        (k) =>
          ![
            "active_voice_part",
            "source_artifact_id",
            "track_id",
            "clip_id",
            "ignore_clip_bindings",
            "model_id",
            "model_hash",
            "model_artifact_id",
            "index_id",
            "index_hash",
            "voice_id",
            "run_id",
            "comp_segments",
            "comp_base_candidate_id",
            "timeline_duration_ticks",
            "svs_model_id",
          ].includes(k),
      )
    )
      update({
        model_parameter_updates: {
          [context.scope === "clip" ? "clips" : "tracks"]: {
            [context.targetId]: patchValues,
          },
        },
      });
    else globalParam(patchValues);
  };
  const refresh = useCallback(async () => {
    const [caps, list, ps] = await Promise.all([
      request<Bag>("/capabilities"),
      request<Bag>("/models"),
      request<Bag>("/projects"),
    ]);
    if (caps.protocol_version !== 1)
      throw new Error("服务协议版本不匹配，请同时更新桌面端与本地服务。");
    setEngines(caps.engines || []);
    setDevice(caps.device || {});
    setModels(list.models || []);
    setProjects(ps.projects || []);
    setConnectionState("ready");
    if (!pRef.current) {
      const recent =
        ps.projects?.find(
          (p: Project) => p.id === localStorage.getItem("sonora-project"),
        ) || ps.projects?.[0];
      if (recent) {
        const p = await request<Project>(`/projects/${recent.id}`);
        applyProject(p);
      }
    }
  }, [applyProject]);
  useEffect(() => {
    let alive = true;
    initConnection()
      .then(() => refresh())
      .catch((e) => {
        if (alive) {
          setConnectionState(
            e instanceof ApiError && e.status === 401
              ? "unauthorized"
              : "disconnected",
          );
          setError(errorText(e));
        }
      });
    return () => {
      alive = false;
    };
  }, [refresh]);
  useEffect(() => {
    if (connection !== "ready") return;
    let cancelled = false;
    let fetching = false;
    let failures = 0;
    const poll = async () => {
      if (fetching) return;
      fetching = true;
      try {
        const event = await request<Bag>(
          `/events?after=${eventCursor.current}`,
        );
        if (cancelled) return;
        eventCursor.current = event.last_sequence || eventCursor.current;
        const id = pRef.current?.id;
        if (id) {
          const result = await request<Bag>(`/jobs?project_id=${id}`);
          if (cancelled) return;
          setJobs(result.jobs || []);
          const signature =
            id +
            JSON.stringify(
              (result.jobs || []).map((j: Job) => [j.id, j.state]),
            );
          if (
            signature !== jobSignature.current ||
            event.events?.some((e: Bag) => e.project_id === id)
          )
            refreshNeeded.current = true;
          jobSignature.current = signature;
          if (refreshNeeded.current && !Object.keys(pending.current).length) {
            await saveChain.current.catch(() => {});
            if (!Object.keys(pending.current).length) {
              const latest = await request<Project>(`/projects/${id}`);
              if (
                pRef.current?.id === id &&
                !Object.keys(pending.current).length
              ) {
                applyProject(latest);
                refreshNeeded.current = false;
              }
            }
          }
        }
        failures = 0;
      } catch (e) {
        if (!cancelled && e instanceof ApiError && e.status === 401)
          setConnectionState("unauthorized");
        else if (!cancelled && ++failures >= 3)
          setConnectionState("disconnected");
      } finally {
        fetching = false;
      }
    };
    void poll();
    const timer = setInterval(poll, 1400);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [connection, applyProject]);
  const source =
    project?.assets.find((a) => a.stem_role === "original" && isAudio(a)) ||
    project?.assets.find(isAudio);
  const legacyVocal = project ? sourceForProject(project) : undefined;
  const activePart: VoicePartKey | "vocal" = [
    "lead_vocal",
    "backing_vocal",
  ].includes(project?.parameters.active_voice_part)
    ? project!.parameters.active_voice_part
    : "vocal";
  const partBinding =
    activePart === "vocal" ? undefined : project?.voice_parts?.[activePart];
  const hasVoiceParts = Object.keys(project?.voice_parts || {}).length > 0;
  const currentContext = editorContext(project, activePart);
  const editorParameters = currentContext.parameters;
  const editorPitchEdits =
    activePart === "vocal"
      ? project?.pitch_edits || []
      : partBinding?.pitch_edits || [];
  const editorEditGroups = Object.values(editorPitchEdits.reduce((groups: Record<string, Bag>, e: Bag) => {
    const id = e.stroke_id || e.id;
    if (!groups[id]) groups[id] = { ...e, member_ids: [e.id] };
    else { groups[id].member_ids.push(e.id); groups[id].start_tick = Math.min(groups[id].start_tick, e.start_tick); groups[id].end_tick = Math.max(groups[id].end_tick, e.end_tick); }
    return groups;
  }, {})) as Bag[];
  const editorNotes =
    activePart === "vocal" ? project?.notes || [] : partBinding?.notes || [];
  const vocal =
    activePart === "vocal"
      ? legacyVocal
      : project?.assets.find((a) => a.id === partBinding?.source_artifact_id);
  const scopedCandidates =
    project?.candidates.filter((c) =>
      activePart === "vocal" ? !c.voice_part : c.voice_part === activePart,
    ) || [];
  const candidates = scopedCandidates.filter((c) => !c.archived);
  const selectedCandidateId =
    activePart === "vocal"
      ? project?.selected_candidate_id
      : partBinding?.selected_candidate_id;
  const auditionScope = `${project?.id || ""}:${activePart}`;
  const newestCandidate = candidates[candidates.length - 1];
  useEffect(() => {
    if (
      seenCandidate.current.scope === auditionScope &&
      newestCandidate?.id &&
      newestCandidate.id !== seenCandidate.current.id
    ) {
      setAuditionCandidateIds(previous => ({ ...previous, [auditionScope]: newestCandidate.id }));
      if (step === 2) { setListenId(newestCandidate.artifact_id); setAB("B"); }
    }
    seenCandidate.current = {
      scope: auditionScope,
      id: newestCandidate?.id || "",
    };
  }, [auditionScope, newestCandidate?.id]);
  const adoptedCandidate = candidates.find((c) => c.id === selectedCandidateId);
  const candidate =
    candidates.find((c) => c.id === auditionCandidateIds[auditionScope]) ||
    adoptedCandidate ||
    candidates
      .slice()
      .reverse()
      .find((c) => c.artifact_id);
  const candidateCompatible = (c: Bag) =>
    (activePart === "vocal" && c.source_artifact_id === vocal?.id) ||
    (!!vocal?.sha256 &&
      c.voice_part === activePart &&
      project?.assets.find(
        (a) => a.id === (c.part_source_artifact_id || c.source_artifact_id),
      )?.sha256 === vocal?.sha256);
  const referenceCandidate = candidates.find(c=>c.id===project?.view_preferences?.ab_reference_candidate_id && c.id!==candidate?.id && c.source_artifact_id===candidate?.source_artifact_id);
  const comparisonA = referenceCandidate ? project?.assets.find(a=>a.id===referenceCandidate.artifact_id) : candidate
    ? project?.assets.find((a) => a.id === (candidate.source_preview_artifact_id || candidate.source_artifact_id))
    : vocal || source;
  const comparisonB = project?.assets.find(
    (a) => a.id === candidate?.artifact_id,
  );
  const exportSource = project?.assets.find(a => a.id === exportId) || project?.assets.slice().reverse().find(a => a.stem_role === "mix") || comparisonB || source;
  const mixStateKey = JSON.stringify([project?.id, project?.tracks.filter(t => t.role !== "mix"), project?.selection, mixMonitor]);
  const currentMixPreview =
    mixPreview?.key === mixStateKey
      ? (mixPreview.artifact as Artifact)
      : undefined;
  const trackMeter = (id: string) => {
    const meter = currentMixPreview?.track_meters?.[id];
    const index = Math.floor((playhead - seconds(currentMixPreview?.timeline_offset_ticks || 0)) / (meter?.hop_seconds || .05));
    return playing && step === 4 ? Math.max(-120, ...(meter?.peaks_dbfs?.[index] || [-120])) : -120;
  };
  const listen =
    step === 4
      ? currentMixPreview
      : step === 5
        ? exportSource
        : project?.assets.find((a) => a.id === listenId) ||
          (step === 3
            ? ab === "B"
              ? comparisonB
              : comparisonA
            : step === 0
              ? source
              : vocal || source);
  const artifactOffset = (a?: Artifact) =>
    a
      ? seconds(
          project?.tracks.find((t) => t.artifact_id === a.id)?.offset_ticks ??
            a.timeline_offset_ticks ??
            0,
        )
      : 0;
  const listenOffset = artifactOffset(listen);
  const maxDuration = Math.max(
    0,
    seconds(project?.parameters.timeline_duration_ticks || 0),
    ...(project?.notes.map((n) => seconds(n.end)) || []),
    ...(project?.lyrics.map((n) => seconds(n.end)) || []),
    ...(project?.assets
      .filter(isAudio)
      .map((a) => duration(a) + artifactOffset(a)) || []),
  );
  const selection = project?.selection || { start: 0, end: ticks(maxDuration) };
  const currentModel = models.find((m) => m.id === editorParameters.model_id);
  const pitchAnalysis = workflow?.project_id === project?.id &&
    (workflow?.voice_part || "vocal") === activePart ? workflow?.pitch_analysis : undefined;
  const lastPitch = project?.assets.find((a) => a.id === pitchAnalysis?.artifact_id) || project?.assets
    .slice()
    .reverse()
    .find(
      (a) =>
        (a.stem_role === "pitch" || String(a.media_type).includes("pitch")) &&
        a.input_artifact_ids?.includes(vocal?.id || ""),
    );
  const outputPitch = project?.assets
    .slice()
    .reverse()
    .find(
      (a) =>
        a.stem_role === "pitch" &&
        a.input_artifact_ids?.includes(candidate?.artifact_id || ""),
    );
  const pitchData =
    loadedPitchData?.artifact_id === lastPitch?.id ? loadedPitchData : null;
  const outputPitchData =
    loadedOutputPitchData?.artifact_id === outputPitch?.id
      ? loadedOutputPitchData
      : null;
  const activeCount = jobs.filter(activeJob).length;
  const mode = project?.mode || "quick";
  const dirty =
    editorEditGroups.length + editorNotes.length +
    (rvcCatalog?.parameters || []).filter(p=>p.group==='advanced' && p.supported && !p.read_only &&
      JSON.stringify(editorParameters[p.id] ?? p.current_value ?? p.default)!==JSON.stringify(p.default)).length;
  const svsEngine = engines.find((e) => e.id === "svs");
  const svsModelId =
    project?.parameters.svs_model_id ||
    (typeof svsEngine?.default_model === "string"
      ? svsEngine.default_model
      : svsEngine?.default_model?.id);
  const engineReady = (name: string) =>
    engines.some(
      (e) =>
        (e.id.includes(name) || e.name?.toLowerCase().includes(name)) &&
        ["ready", "available", "installed"].includes(e.status),
    );
  useEffect(() => {
    if (hasVoiceParts && pitchTab === "lyrics") setPitchTab("pitch");
  }, [hasVoiceParts, pitchTab]);
  useEffect(() => {
    if (!lastPitch) {
      setPitchData(null);
      return;
    }
    let live = true;
    request<PitchData>(`/artifacts/${lastPitch.id}/data`)
      .then((d) => {
        if (live) setPitchData({ ...d, artifact_id: lastPitch.id });
      })
      .catch((e) => {
        if (live) setError(errorText(e));
      });
    return () => {
      live = false;
    };
  }, [lastPitch?.id]);
  useEffect(() => {
    let live = true;
    setOutputPitchData(null);
    if (outputPitch)
      request<PitchData>(`/artifacts/${outputPitch.id}/data`)
        .then((d) => {
          if (live) setOutputPitchData({ ...d, artifact_id: outputPitch.id });
        })
        .catch((e) => {
          if (live) setError(errorText(e));
        });
    return () => {
      live = false;
    };
  }, [outputPitch?.id]);
  useEffect(() => {
    const a = audio.current;
    if (!a) return;
    if (!listen) {
      a.pause();
      a.removeAttribute("src");
      return;
    }
    let live = true;
    const wasPlaying = !a.paused;
    const at = playRef.current;
    const cache = mediaCache.current;
    let cached = cache.get(listen.id);
    if (!cached) {
      cached = media(`/artifacts/${listen.id}/audio`).then(blob => URL.createObjectURL(blob)).catch(e => { cache.delete(listen.id); throw e; });
      cache.set(listen.id, cached);
      if (cache.size > 6) {
        const oldest = cache.keys().next().value!;
        if (oldest !== listen.id) { void cache.get(oldest)!.then(url => URL.revokeObjectURL(url)); cache.delete(oldest); }
      }
    }
    cached.then((url) => {
        if (!live || pRef.current?.id !== project?.id) return;
        a.pause();
        a.dataset.projectId = project?.id || "";
        a.src = url;
        a.onloadedmetadata = () => {
          if (!live || pRef.current?.id !== project?.id) return;
          a.currentTime = Math.max(
            0,
            Math.min(
              at - listenOffset,
              Number.isFinite(a.duration)
                ? a.duration
                : Math.max(0, at - listenOffset),
            ),
          );
          setPlayhead(a.currentTime + listenOffset);
          playRef.current = a.currentTime + listenOffset;
          if (wasPlaying) void a.play().catch((e) => setError(errorText(e)));
        };
        a.load();
      })
      .catch((e) => {
        if (live) setError(errorText(e));
      });
    return () => {
      live = false;
    };
  }, [listen?.id, listenOffset]);
  const aRms = abMeasurements?.a.rms_dbfs,
    bRms = abMeasurements?.b.rms_dbfs;
  const matchAvailable =
    step === 3 && Number.isFinite(aRms) && Number.isFinite(bRms);
  const matchGain =
    matched && matchAvailable && !listenId
      ? 10 ** ((Math.min(aRms, bRms) - (ab === "A" ? aRms : bRms)) / 20)
      : 1;
  useEffect(() => {
    const value = Math.min(1, Math.max(0, volume * matchGain));
    if (audioGraph.current) {
      const { context, gain } = audioGraph.current;
      gain.gain.cancelScheduledValues(context.currentTime);
      gain.gain.setTargetAtTime(value, context.currentTime, .008);
      if (audio.current) audio.current.volume = 1;
    } else if (audio.current) audio.current.volume = value;
  }, [volume, matchGain]);
  const prepareAudioGraph = async () => {
    if (!audio.current) return;
    if (!audioGraph.current) {
      const context = new AudioContext(), gain = context.createGain(), analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      context.createMediaElementSource(audio.current).connect(gain).connect(analyser).connect(context.destination);
      gain.gain.value = volume * matchGain; audio.current.volume = 1;
      audioGraph.current = { context, gain, analyser };
    }
    await audioGraph.current.context.resume();
  };
  const loopStart = Math.max(seconds(selection.start), listenOffset);
  const loopEnd = Math.min(
    seconds(selection.end),
    listenOffset + duration(listen),
  );
  const loopValid = !!listen && loopEnd - loopStart >= 0.05;
  const resetLoopSeek = () => {
    if (loopPending.current) clearTimeout(loopPending.current);
    loopPending.current = null;
    if (audio.current) audio.current.onseeked = null;
    const graph = audioGraph.current;
    if (graph && graph.context.state !== "closed") {
      graph.gain.gain.cancelScheduledValues(graph.context.currentTime);
      graph.gain.gain.setTargetAtTime(volume * matchGain, graph.context.currentTime, .005);
    }
  };
  const wrapLoop = () => {
    const a = audio.current;
    if (!a || !loop || !loopValid || loopPending.current) return false;
    const graph = audioGraph.current;
    if (graph) {
      graph.gain.gain.cancelScheduledValues(graph.context.currentTime);
      graph.gain.gain.setValueAtTime(graph.gain.gain.value, graph.context.currentTime);
      graph.gain.gain.linearRampToValueAtTime(0, graph.context.currentTime + .005);
    }
    loopPending.current = setTimeout(() => {
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true; resetLoopSeek();
        void a.play().catch(() => setPlaying(false));
      };
      a.onseeked = finish;
      loopPending.current = setTimeout(finish, 300);
      a.currentTime = Math.max(0, loopStart - listenOffset);
    }, 6);
    return true;
  };
  const transportFrame = useRef<() => void>(() => {});
  transportFrame.current = () => {
    const a = audio.current;
    if (!a || a.paused || a.dataset.projectId !== pRef.current?.id) return;
    if (loop && loopValid && a.currentTime + listenOffset >= loopEnd - .008) wrapLoop();
    playRef.current = a.currentTime + listenOffset;
    setPlayhead(playRef.current);
    if (audioGraph.current) {
      const data = new Float32Array(audioGraph.current.analyser.fftSize);
      audioGraph.current.analyser.getFloatTimeDomainData(data);
      setMeterDb(20 * Math.log10(Math.max(1e-6, ...data.map(Math.abs))));
    }
  };
  useEffect(() => {
    if (!playing) { setMeterDb(-120); return; }
    let frame = 0, last = 0;
    const tick = (time: number) => { if (time - last > 32) { transportFrame.current(); last = time; } frame = requestAnimationFrame(tick); };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);
  useEffect(() => {
    if (!loop || !loopValid) {
      resetLoopSeek();
      if (loop && !loopValid) setLoop(false);
    }
    return resetLoopSeek;
  }, [loop, loopValid, listen?.id]);
  const seek = (t: number) => {
    resetLoopSeek();
    const value = listen
      ? Math.min(listenOffset + duration(listen), Math.max(listenOffset, t))
      : Math.min(maxDuration, Math.max(0, t));
    setPlayhead(value);
    playRef.current = value;
    if (pRef.current) update({ playhead: ticks(value) });
    if (audio.current && audio.current.readyState)
      audio.current.currentTime = Math.min(
        Math.max(0, value - listenOffset),
        audio.current.duration || Math.max(0, value - listenOffset),
      );
  };
  const measurement = (artifact: Artifact, region?: number[]) => {
    const key = JSON.stringify([artifact.id, region]);
    let result = measurementCache.current.get(key);
    if (!result) {
      result = post<Bag>(`/artifacts/${artifact.id}/measure`, region ? { range_ticks: region } : {}).catch(e => { measurementCache.current.delete(key); throw e; });
      measurementCache.current.set(key, result);
    }
    return result;
  };
  useEffect(() => {
    if (step !== 4 || !project) return;
    const run = ++previewRun.current;
    let disposed = false,
      jobId = "";
    setMixPreviewState("当前设置已改变，正在准备选区试听…");
    audio.current?.pause();
    const timer = setTimeout(async () => {
      try {
        await flush();
        const p = pRef.current;
        if (!p || disposed || p.id !== project.id) return;
        const region = p.selection.end > p.selection.start ? [p.selection.start, p.selection.end] : [0, ticks(Math.min(maxDuration, 15))];
        const body = { revision: p.revision, idempotency_key: uid(), operation: "mix_preview", parameters: { range_ticks: region, monitor_track_id: mixMonitor || null } };
        let result: Job;
        try { result = await post<Job>(`/projects/${p.id}/jobs`, body); }
        catch (e) {
          if (!(e instanceof ApiError) || e.status !== 409 || disposed) throw e;
          const latest = await request<Project>(`/projects/${p.id}`);
          result = await post<Job>(`/projects/${p.id}/jobs`, { ...body, revision: latest.revision });
        }
        jobId = result.id;
        if (disposed) {
          void post(`/jobs/${jobId}/cancel`).catch(() => {});
          return;
        }
        while (activeJob(result) && !disposed) {
          setMixPreviewState(
            result.state === "queued"
              ? "混音试听已排队…"
              : "正在用当前推子与效果渲染选区…",
          );
          await new Promise((resolve) => setTimeout(resolve, 350));
          result = await request<Job>(`/jobs/${jobId}`);
        }
        if (disposed || run !== previewRun.current) return;
        if (result.state !== "succeeded")
          throw new Error(result.error?.message || "选区试听未完成");
        const artifact = await request<Artifact>(
          `/artifacts/${result.artifact_ids[0]}`,
        );
        if (disposed || run !== previewRun.current) return;
        setMixPreview({ key: mixStateKey, artifact });
        setMixPreviewState(
          mixMonitor
            ? "已更新：当前轨道推子后 / 效果后试听"
            : "已更新：当前推子、声像、静音/独奏、偏移和效果",
        );
      } catch (e) {
        if (!disposed) setMixPreviewState(`试听未更新：${errorText(e)}`);
      }
    }, 650);
    return () => { disposed = true; clearTimeout(timer); if (jobId) void post(`/jobs/${jobId}/cancel`).catch(() => {}); };
  }, [step, mixStateKey, flush]);
  useEffect(() => {
    let live = true; setAbMeasurements(null);
    if (step !== 3 || !comparisonA || !comparisonB) return;
    const start = Math.max(artifactOffset(comparisonA), artifactOffset(comparisonB), seconds(selection.start));
    const end = Math.min(artifactOffset(comparisonA) + duration(comparisonA), artifactOffset(comparisonB) + duration(comparisonB), seconds(selection.end));
    if (end <= start) return;
    const regionFor = (artifact: Artifact) => [start, end].map(time => ticks(time - artifactOffset(artifact)) + (artifact.timeline_offset_ticks || 0));
    const timer = setTimeout(() => { void Promise.all([measurement(comparisonA, regionFor(comparisonA)), measurement(comparisonB, regionFor(comparisonB))]).then(([a,b]) => { if (live) setAbMeasurements({ a, b }); }).catch(() => {}); }, 350);
    return () => { live = false; clearTimeout(timer); };
  }, [step, comparisonA?.id, comparisonB?.id, artifactOffset(comparisonA), artifactOffset(comparisonB), selection.start, selection.end]);
  useEffect(() => {
    let live = true; setExportMeasurements(null);
    if (step !== 5 || !exportSource) return;
    void measurement(exportSource, exportSelection ? [selection.start, selection.end] : undefined).then(value => { if (live) setExportMeasurements(value); }).catch(e => { if (live) setError(errorText(e)); });
    return () => { live = false; };
  }, [step, exportSource?.id, exportSelection, selection.start, selection.end]);
  const togglePlay = () => {
    const a = audio.current;
    if (!listen || !a?.src) {
      tell("先导入一条音频，或选择已有产物。");
      return;
    }
    if (a.paused) {
      if (
        loop &&
        (a.currentTime + listenOffset < seconds(selection.start) ||
          a.currentTime + listenOffset >= seconds(selection.end))
      )
        seek(seconds(selection.start));
      void prepareAudioGraph().then(() => a.play()).catch((e) => setError(errorText(e)));
    } else {
      resetLoopSeek(); a.pause();
    }
  };
  const selectAB = (which: "A" | "B") => {
    if (step !== 3) return;
    if (which === "A" && !comparisonA) {
      tell("候选的源音频缺失，请先重新链接。");
      return;
    }
    if (which === "B" && !comparisonB) {
      tell("生成并选择候选后即可对齐 A/B 试听。");
      return;
    }
    setListenId("");
    if (comparisonA && comparisonB) {
      const start = Math.max(
        artifactOffset(comparisonA),
        artifactOffset(comparisonB),
      );
      const end = Math.min(
        artifactOffset(comparisonA) + duration(comparisonA),
        artifactOffset(comparisonB) + duration(comparisonB),
      );
      if (end > start) {
        const at = Math.min(end - 0.001, Math.max(start, playRef.current));
        playRef.current = at;
        setPlayhead(at);
      }
    }
    setAB(which);
  };
  const ensureProject = async () => {
    if (pRef.current) return pRef.current;
    const p = await post<Project>("/projects", {
      name: newName,
      task: "cover",
    });
    applyProject(p);
    return p;
  };
  const setupReady = !!project?.assets.length || !!(project?.parameters.task_confirmed && project?.parameters.import_role);
  const importRole = project?.parameters.import_role || (project?.assets.length ? "original" : "");
  const chooseImport = (role = importRole || "original") => {
    if (step === 0 && !setupReady) { tell("先确认制作任务和本次素材角色。"); return; }
    roleRef.current = role;
    upload.current?.click();
  };
  const importFile = async (file: File, role = roleRef.current) => {
    await flush();
    const p = await ensureProject();
    const data = new FormData();
    data.append("file", file);
    data.append("role", role);
    const result = await request<Bag>(`/projects/${p.id}/import`, {
      method: "POST",
      body: data,
    });
    applyProject(result.project);
    setListenId(result.artifact.id);
    setAB("A");
    setError("");
    tell(`${file.name} 已导入；原始文件保持不变。`);
  };
  const handleFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) void guard(() => importFile(file), "正在导入与解码");
  };
  const job = async (
    operation: string,
    parameters: Bag = {},
    inputId?: string,
  ) => {
    const focused = pRef.current?.parameters.active_voice_part;
    if (
      ["pitch", "convert"].includes(operation) &&
      ["lead_vocal", "backing_vocal"].includes(focused) &&
      !parameters.voice_part
    )
      parameters = { ...parameters, voice_part: focused };
    await flush();
    const p = await ensureProject();
    const body = {
      revision: pRef.current!.revision,
      idempotency_key: uid(),
      operation,
      input_artifact_id: inputId,
      parameters,
    };
    let result: Job;
    try {
      result = await post<Job>(`/projects/${p.id}/jobs`, body);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 409) throw e;
      const latest = await request<Project>(`/projects/${p.id}`);
      if (pRef.current?.id !== p.id)
        throw new Error("工程已切换，请在当前工程重新创建任务。");
      applyProject(mergeEditorPatch(latest, pending.current) as Project);
      await flush();
      result = await post<Job>(`/projects/${p.id}/jobs`, {
        ...body,
        revision: pRef.current!.revision,
      });
    }
    setJobs((js) => [result, ...js]);
    tell("任务已加入本机队列。");
    return result;
  };
  const renderCandidate = (short: boolean, chain = false) =>
    guard(async () => {
      if (short && step !== 2) {
        setStep(2);
        return;
      }
      if (!project || !editorParameters.model_id)
        throw new Error("请先选择兼容模型。");
      if (activePart !== "vocal" && partBinding?.mode !== "convert")
        throw new Error(
          "当前声部选择了保留原声或静音。请先将处理方式切换为转换。",
        );
      if (
        activePart === "vocal" &&
        project!.task === "rewrite" &&
        !project.assets.some((a) =>
          ["guide", "synthesized"].includes(a.stem_role || ""),
        )
      )
        throw new Error("改词转换需要唱着新歌词的导唱。请先导入或录制导唱。");
      const renderValues = editorContext(
        pRef.current,
        activePart,
        !short,
      ).parameters;
      const parameters: Bag = {
        ...renderValues,
        pitch_artifact_id: renderValues.pitch_artifact_id ?? null,
        pitch_edits: editorPitchEdits,
        ...(activePart !== "vocal" ? { voice_part: activePart } : {}),
      };
      delete parameters.range_ticks;
      if (short) {
        parameters.range_ticks = [selection.start, selection.end];
        parameters.preview_purpose = "pitch_edit";
      } else {
        parameters.full_source = true;
        parameters.ignore_clip_bindings = true;
        delete parameters.clip_id;
        delete parameters.clip_range_ticks;
      }
      if (chain && candidate) parameters.chain_candidate_id = candidate.id;
      await job("convert", parameters, vocal?.id);
    }, "提交转换任务");
  const historyAction = (direction: "undo" | "redo") =>
    guard(async () => {
      await flush();
      let p = pRef.current;
      if (!p) return;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const result = await post<Project>(`/projects/${p.id}/history`, { direction, revision: p.revision });
          applyProject(mergeEditorPatch(result, pending.current) as Project);
          setError(""); break;
        } catch (e) {
          if (!(e instanceof ApiError) || e.status !== 409 || attempt === 2) throw e;
          p = await request<Project>(`/projects/${p.id}`);
          if (pRef.current?.id !== p.id) return;
          applyProject(mergeEditorPatch(p, pending.current) as Project);
          await flush(); p = pRef.current!;
        }
      }
      tell(direction === "undo" ? "已撤销上一步编辑。" : "已重做。");
    });
  const openModelManager = (scope: VoicePartKey | "vocal" = activePart) => {
    setManagerScope(scope);
    setModal("models");
  };
  const usedModelIds = [
    ...new Set(
      [
        ...(project?.candidates.map((c) => c.model_id) || []),
        project?.parameters.model_id,
        ...Object.values(project?.voice_parts || {}).map(
          (b) => b?.parameters.model_id,
        ),
      ].filter(Boolean),
    ),
  ] as string[];
  const bindVersion = async (
    model: Bag,
    indexId: string | null | undefined,
    scope?: "project" | "track" | "clip",
    speakerId?: number,
  ) => {
    await flush();
    const p = pRef.current;
    if (!p) throw new Error("先创建或导入工程。");
    const ctx = editorContext(p, "vocal");
    const chosenScope =
      scope ||
      (ctx.scope === "clip"
        ? "clip"
        : ctx.scope === "track"
          ? "track"
          : "project");
    const body: Bag = {
      revision: p.revision,
      idempotency_key: uid(),
      scope: chosenScope,
      model_id: model.id,
      index_id: indexId,
      speaker_id: speakerId ?? ctx.parameters.speaker_id ?? 0,
      preset_id: ctx.parameters.preset_id,
      policy:
        ctx.parameters.switch_policy === "reset_defaults"
          ? "reset_defaults"
          : "preserve_overrides",
    };
    if (chosenScope !== "project") {
      if (!ctx.track) throw new Error("先选择有效的人声输入轨。");
      body.track_id = ctx.track.id;
    }
    if (chosenScope === "clip") {
      body.range_ticks = scope
        ? [p.selection.start, p.selection.end]
        : ctx.clip?.range_ticks || [p.selection.start, p.selection.end];
      if (!scope && ctx.targetId) body.clip_id = ctx.targetId;
    }
    const result = await post<Bag>(`/projects/${p.id}/model-selection`, body);
    if (pRef.current?.id !== p.id)
      throw new Error("工程已切换，原工程的选择已保存。");
    applyProject(result.project);
    if (
      chosenScope === "clip" &&
      result.project.parameters.ignore_clip_bindings
    ) {
      globalParam({ ignore_clip_bindings: false });
      await flush();
    }
    setModels((ms) => [
      ...ms.filter((x) => x.id !== model.id),
      { ...model, engine_id: model.engine_id || "rvc" } as Model,
    ]);
    setCatalogRevision((n) => n + 1);
    return result;
  };
  const selectScopedModel = async (
    scope: VoicePartKey | "vocal",
    model: Bag,
    indexInfo: Bag,
    options: Bag = {},
  ) => {
    const p = pRef.current;
    if (!p) throw new Error("先创建或导入工程。");
    const current = editorContext(p, scope).parameters;
    const matching = indexInfo.indices || [];
    const targetIndex =
      options.index_id !== undefined
        ? matching.find((i: Bag) => i.id === options.index_id)
        : current.index_id === null
          ? null
          : matching.find(
              (i: Bag) =>
                i.id === current.index_id &&
                i.compatibility?.status === "verified",
            ) ||
            matching.find(
              (i: Bag) =>
                i.id === indexInfo.selected_index_id &&
                i.compatibility?.status === "verified",
            );
    const indexId = options.index_id === null ? null : targetIndex?.id || null;
    if (Number(current.index_rate || 0) > 0 && !targetIndex)
      throw new Error(
        "该版本没有已验证匹配的索引。请先明确将检索率设为0，再切换版本。",
      );
    const speakerIds = model.speaker_ids_trained || model.speaker_ids || [0];
    const speaker =
      options.speaker_id ??
      (speakerIds.includes(current.speaker_id)
        ? current.speaker_id
        : (speakerIds[0] ?? 0));
    if (scope === "vocal")
      await bindVersion(model, indexId, options.binding_scope, speaker);
    else {
      let defaults: Bag = {};
      if (current.switch_policy === "reset_defaults") {
        const s = await post<RvcCatalog>("/rvc/parameters", {
          model_id: model.id,
          task: p.task,
          values: {},
          has_external_f0: true,
        });
        defaults = Object.fromEntries(
          s.parameters
            .filter((f) => f.supported && !f.read_only)
            .map((f) => [f.id, f.default]),
        );
      }
      const changes = {
        ...defaults,
        model_id: model.id,
        model_artifact_id: model.id,
        model_hash: model.model_hash,
        voice_id: model.voice_id,
        run_id: model.run_id,
        index_id: indexId,
        index_hash: targetIndex?.index_hash || targetIndex?.sha256 || null,
        speaker_id: speaker,
      };
      if (pRef.current?.id !== p.id)
        throw new Error("工程已切换，请重新选择版本。");
      patchPart(scope, {
        parameters: {
          ...changes,
          parameter_sources: {
            ...current.parameter_sources,
            ...Object.fromEntries(
              Object.keys(changes).map((k) => [k, "user_override"]),
            ),
          },
        },
      });
      await flush();
      setModels((ms) => [
        ...ms.filter((x) => x.id !== model.id),
        { ...model, engine_id: model.engine_id || "rvc" } as Model,
      ]);
    }
    tell(`已固定 ${versionLabel(model)}。人工音高与原有候选保留。`);
  };
  const changeIndex = (scope: VoicePartKey | "vocal", id: string | null) =>
    void guard(async () => {
      const ctx = editorContext(pRef.current, scope);
      if (!ctx.parameters.model_id) throw new Error("先选择模型版本。");
      if (!id) {
        if (scope === "vocal" && ctx.scope !== "project")
          update({
            model_parameter_updates: {
              [ctx.scope === "clip" ? "clips" : "tracks"]: {
                [ctx.targetId!]: {
                  index_rate: 0,
                  parameter_sources: { index_rate: "user_override" },
                },
              },
            },
          });
        else if (scope === "vocal") globalParam({ index_rate: 0 });
        else patchPart(scope, { parameters: { index_rate: 0 } });
        await flush();
      }
      const model = await request<Bag>(
        `/catalog/models/${encodeURIComponent(ctx.parameters.model_id)}`,
      );
      const indices = await request<Bag>(
        `/catalog/models/${encodeURIComponent(model.id)}/indices`,
      );
      await selectScopedModel(scope, model, indices, { index_id: id });
    }, "更新索引绑定");
  const applyRvcChanges = (changes: Bag, source: string, presetId?: string) =>
    void guard(async () => {
      const { speaker_id, ...rest } = changes;
      if (
        speaker_id !== undefined &&
        speaker_id !== editorParameters.speaker_id &&
        activePart === "vocal" &&
        currentContext.scope !== "project"
      ) {
        const m = await request<Bag>(
            `/catalog/models/${encodeURIComponent(editorParameters.model_id)}`,
          ),
          ix = await request<Bag>(
            `/catalog/models/${encodeURIComponent(editorParameters.model_id)}/indices`,
          );
        await selectScopedModel(activePart, m, ix, { speaker_id });
      } else if (speaker_id !== undefined) rest.speaker_id = speaker_id;
      if (Object.keys(rest).length) param(rest, source, presetId);
    });
  const compareVersions = async (selected: Bag[]) => {
    if (!vocal) throw new Error("先选择已准备的人声输入。");

    await job(
      "compare_versions",
      {
        ...editorContext(pRef.current, activePart, true).parameters,
        model_ids: selected.map((m) => m.id),
        full_source: true,
        ignore_clip_bindings: true,
        range_ticks: null,
        ...(activePart !== "vocal" ? { voice_part: activePart } : {}),
        pitch_artifact_id:
          editorContext(pRef.current, activePart).parameters
            .pitch_artifact_id ?? null,
      },
      vocal.id,
    );
  };
  const selectorFor = (
    scope: VoicePartKey | "vocal",
    compact = false,
    label = "目标声音",
  ) => {
    const context = editorContext(project, scope),
      values = context.parameters;
    return (
      <ModelSelector
        label={label}
        compact={compact}
        preferences={project?.view_preferences?.model_selector?.[scope] || {}}
        onPreferences={(changes)=>updateView({model_selector:{...pRef.current?.view_preferences?.model_selector,[scope]:{...pRef.current?.view_preferences?.model_selector?.[scope],...changes}}})}
        value={values}
        fallback={models.find((m) => m.id === values.model_id)}
        usedModelIds={usedModelIds}
        revision={catalogRevision}
        disabled={!!busy}
        onSelect={(m, ix) => selectScopedModel(scope, m, ix)}
        onIndex={(id) => changeIndex(scope, id)}
        onNoRetrieval={() => changeIndex(scope, null)}
        onCompare={!compact ? compareVersions : undefined}
        onManage={() => openModelManager(scope)}
        onChanged={() => setCatalogRevision((v) => v + 1)}
        onError={setError}
        switchPolicy={
          values.switch_policy === "reset_defaults"
            ? "reset_defaults"
            : "keep_compatible"
        }
        onSwitchPolicy={(policy) => {
          if (scope === "vocal") param({ switch_policy: policy });
          else patchPart(scope, { parameters: { switch_policy: policy } });
        }}
      />
    );
  };
  const adoptSettings = async (c: Bag) => {
    const intended = c.parameters || c.effective_parameters || {};
    if (intended.index_rate === 0) {
      param({ index_rate: 0 }, "user_override");
      await flush();
    }
    const m = await post<Bag>(
        `/catalog/models/${encodeURIComponent(c.model_id)}/validate`,
      ),
      ix = await request<Bag>(
        `/catalog/models/${encodeURIComponent(c.model_id)}/indices`,
      );
    await selectScopedModel(activePart, m, ix, {
      index_id: c.parameters?.index_id ?? null,
    });
    const schema = await post<RvcCatalog>("/rvc/parameters", {
      model_id: m.id,
      task: project?.task,
      values: c.parameters || c.effective_parameters,
      has_external_f0: true,
    });
    const from = c.parameters || c.effective_parameters || {};
    const settings = Object.fromEntries(
      schema.parameters
        .filter((p) => p.supported && !p.read_only)
        .map((p) => [p.id, from[p.id] ?? p.current_value ?? p.default]),
    );
    param({ ...settings, accepted_preview_id: c.id }, "user_override");
    await flush();
    tell("已采用这次试听的版本与参数；整段音频尚未替换。");
  };
  const fullBaseFor = (c: Bag) =>
    adoptedCandidate &&
    !adoptedCandidate.preview_only &&
    adoptedCandidate.source_artifact_id === c.source_artifact_id
      ? adoptedCandidate
      : undefined;
  const clearPitchBinding = async () => {
    param({ pitch_artifact_id: null });
    await flush();
    setError("");
    tell("已清除固定分析绑定；下次试听按当前参数匹配或重新检测，手工编辑与锁定区保留。");
  };
  const detectCurrentPitch = () => guard(async () => {
    await clearPitchBinding();
    await job("pitch", { ...editorContext(pRef.current, activePart).parameters, pitch_artifact_id: null }, vocal?.id);
  }, "按当前参数检测音高");
  const refreshPitchPreview = () => guard(async () => {
    await clearPitchBinding();
    await renderCandidate(true);
  }, "更新分析与试听");
  const archiveCandidate = (c: Bag, archived: boolean) => guard(async () => {
    update({ candidates: [{ id: c.id, archived }] });
    await flush();
    if (archived && (candidate?.id === c.id || listenId === c.artifact_id)) {
      audio.current?.pause();
      setAuditionCandidateIds((previous) => ({ ...previous, [auditionScope]: "" }));
      setListenId("");
      setAB("A");
    }
    tell(archived ? "已归档，音频与配方保留；可在归档版本中恢复或撤销。" : "片段已恢复。");
  }, archived ? "移除片段" : "恢复片段");
  const pitchRecovery = (
    <div
      className={
        pitchAnalysis?.state === "stale" ? "warning-inline" : "info-inline"
      }
      role="status"
    >
      <strong>
        {pitchAnalysis?.state === "stale"
          ? "旧分析 · 需要更新"
          : editorParameters.pitch_artifact_id
            ? "已固定分析曲线"
            : "自动匹配当前分析"}
      </strong>
      <p>
        {pitchAnalysis?.message ||
          "试听按当前检测参数选择曲线；没有匹配结果时先重新检测。"}
      </p>
      <div className="row wrap">
        <button
          className="button small"
          disabled={!vocal || !!busy}
          onClick={() => void detectCurrentPitch()}
        >
          {lastPitch ? "按当前参数重新检测" : "检测当前声部音高"}
        </button>

        <button
          className="text-button"
          disabled={!editorParameters.pitch_artifact_id || !!busy}
          onClick={() => void guard(clearPitchBinding, "清除分析绑定")}
        >
          清除固定分析绑定
        </button>
      </div>
      <small>
        重新检测和清除绑定均保留手工音高编辑、锁定区及旧音频；旧试听可在候选卡片中移除。
      </small>
    </div>
  );
  const localAdopt = (c: Bag) =>
    guard(async () => {
      const base = fullBaseFor(c);
      if (!base)
        throw new Error("先生成并采用一个同源完整版本，再把该试听区间拼入。");
      await job("adopt_preview", {
        candidate_id: c.id,
        base_candidate_id: base.id,
        fade_ms: 20,
        ...(activePart !== "vocal" ? { voice_part: activePart } : {}),
      });
    }, "采用当前试听区间");
  const currentStage =
    workflow && project && workflow.project_id === project.id
      ? workflow.stages?.find((s: Bag) => s.id === step)
      : undefined;
  const candidateState = (c: Bag) =>
    workflow?.stages
      ?.find((s: Bag) => s.id === 3)
      ?.outputs?.find((o: Bag) => o.id === c.id || o.candidate_id === c.id);
  const candidateDiff = (c: Bag) =>
    (rvcCatalog?.parameters || [])
      .filter(
        (p) =>
          p.supported &&
          !p.read_only &&
          (c.effective_parameters || c.parameters)?.[p.id] !== undefined &&
          JSON.stringify((c.effective_parameters || c.parameters)[p.id]) !==
            JSON.stringify(editorParameters[p.id] ?? p.default),
      )
      .map((p) => p.label)
      .slice(0, 4);
  const focusControl = (id: string) =>
    setTimeout(() => {
      const element = document.getElementById(id);
      element?.scrollIntoView({ block: "center", behavior: "smooth" });
      element?.querySelector<HTMLElement>("input,select,button")?.focus();
    }, 0);
  const workflowAction = (action: Bag) => {
    const id = action.id;
    if (id === "setup") { setStep(0); focusControl("task-setup"); return; }
    if (action.kind === "navigate" && action.target_stage != null && !["choose_model", "fix_index", "models", "manage_models", "audition", "import"].includes(id)) {
      setStep(action.target_stage); return;
    }
    if (id === "refresh_pitch_preview") {
      void refreshPitchPreview();
      return;
    }
    if (["import", "import_audio"].includes(id)) {
      chooseImport();
      return;
    }
    if (["choose_model", "fix_index"].includes(id)) {
      setStep(3);
      focusControl("conversion-model");
      return;
    }
    if (["models", "manage_models"].includes(id)) {
      openModelManager();
      return;
    }
    if (id === "validate_model") {
      void guard(async () => {
        const m = await post<Bag>(
            `/catalog/models/${encodeURIComponent(action.model_id || editorParameters.model_id)}/validate`,
          ),
          ix = await request<Bag>(
            `/catalog/models/${encodeURIComponent(m.id)}/indices`,
          );
        await selectScopedModel(activePart, m, ix);
      }, "校验当前版本");
      return;
    }
    if (["preview", "render_preview"].includes(id)) {
      void renderCandidate(true);
      return;
    }
    if (["render_full", "full_render"].includes(id)) {
      void renderCandidate(false);
      return;
    }
    if (id === "adopt_settings") {
      const c =
        candidates.find((c) => c.id === action.candidate_id) || candidate;
      if (c) void guard(() => adoptSettings(c), "采用试听设置");
      return;
    }
    if (id === "adopt_preview") {
      const c =
        candidates.find((c) => c.id === action.candidate_id) || candidate;
      if (c) void localAdopt(c);
      return;
    }
    if (id === "audition") {
      setStep(3);
      const c =
        candidates.find((c) => c.id === action.candidate_id) || candidate;
      if (c) {
        setAuditionCandidateIds((previous) => ({
          ...previous,
          [auditionScope]: c.id,
        }));
        setListenId("");
        setAB("B");
      }
      focusControl("candidate-list");
      return;
    }
    if (action.operation === "pitch") {
      setStep(2);
      void detectCurrentPitch();
      return;
    }
    if (action.operation === "separate") {
      setStep(1);
      void guard(() => job("separate", {}, source?.id), "提交分离");
      return;
    }
    if (action.operation === "mix") {
      setStep(4);
      void guard(() => job("mix", { sample_rate: 48000 }), "提交混音");
      return;
    }
    if (action.operation === "export") {
      setStep(5);
      const a = exportSource;
      void guard(
        () =>
          job(
            "export",
            {
              artifact_id: a?.id,
              format,
              wav_subtype: wavSubtype,
              name: exportName || project?.name,
              selection_only: exportSelection,
              model_converted: !!a?.model_hash,
            },
            a?.id,
          ),
        "提交导出",
      );
      return;
    }
    if (action.target_stage != null) setStep(action.target_stage);
  };
  const savePlayhead = () => {
    if (pRef.current) update({ playhead: ticks(playRef.current) });
  };
  const hotkeys = useRef<Bag>({});
  hotkeys.current = {
    togglePlay,
    selectAB,
    historyAction,
    selection,
    maxDuration,
    update,
    seek,
    step,
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const target = e.target as HTMLElement;
      if (
        target.closest('input,textarea,select,[contenteditable="true"],dialog')
      )
        return;
      const h = hotkeys.current;
      if (e.code === "Space") {
        e.preventDefault();
        h.togglePlay();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        h.historyAction(e.shiftKey ? "redo" : "undo");
      } else if (e.key.toLowerCase() === "i")
        h.update({
          selection: {
            ...h.selection,
            start: Math.min(ticks(playRef.current), h.selection.end),
          },
        });
      else if (e.key.toLowerCase() === "o")
        h.update({
          selection: {
            ...h.selection,
            end: Math.max(ticks(playRef.current), h.selection.start),
          },
        });
      else if (e.key.toLowerCase() === "a") h.selectAB("A");
      else if (e.key.toLowerCase() === "b") h.selectAB("B");
      else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        h.seek(
          playRef.current +
            (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 1 : 0.1),
        );
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(
    () => () => {
      for (const value of mediaCache.current.values()) void value.then(url => URL.revokeObjectURL(url)).catch(() => {});
      if (loopPending.current) clearTimeout(loopPending.current);
      void audioGraph.current?.context.close();
      recorder.current?.stream.getTracks().forEach((t) => t.stop());
    },
    [],
  );
  const selectionControls = (
    <div className="selection-controls">
      <Field label="选区起点（秒）">
        <input
          type="number"
          min={0}
          max={seconds(selection.end)}
          step="0.01"
          value={+seconds(selection.start).toFixed(3)}
          onChange={(e) =>
            update({
              selection: {
                ...selection,
                start: Math.min(
                  selection.end,
                  Math.max(0, ticks(+e.target.value)),
                ),
              },
            })
          }
        />
      </Field>
      <Field label="选区终点（秒）">
        <input
          type="number"
          min={seconds(selection.start)}
          max={maxDuration}
          step="0.01"
          value={+seconds(selection.end).toFixed(3)}
          onChange={(e) =>
            update({
              selection: {
                ...selection,
                end: Math.max(
                  selection.start,
                  Math.min(ticks(maxDuration), ticks(+e.target.value)),
                ),
              },
            })
          }
        />
      </Field>
      <button
        className="quiet-button"
        onClick={() => {
          setLoop(true);
          seek(seconds(selection.start));
          void prepareAudioGraph().then(() => audio.current?.play()).catch((e) => setError(errorText(e)));
        }}
        disabled={!listen}
      >
        <Icon name="repeat" />
        循环试听
      </button>
      <button
        className="text-button"
        onClick={() =>
          update({ selection: { start: 0, end: ticks(maxDuration) } })
        }
      >
        选择整段
      </button>
    </div>
  );
  const focusPart = (part: VoicePartKey | "vocal") => {
    globalParam({ active_voice_part: part });
    setListenId("");
    setAB("A");
    if (part !== "vocal" && pitchTab === "lyrics") setPitchTab("pitch");
  };
  const partSelector = (
    <div className="voice-part-selector">
      <div>
        <strong>当前编辑声部</strong>
        <small>模型、音高、音符和候选分别保存</small>
      </div>
      <div className="segmented" role="group" aria-label="当前编辑声部">
        {(["vocal", "lead_vocal", "backing_vocal"] as const).map((part) => (
          <button
            key={part}
            className={activePart === part ? "active" : ""}
            disabled={part !== "vocal" && !project?.voice_parts?.[part]}
            onClick={() => focusPart(part)}
          >
            {part === "vocal" ? "完整人声" : roleNames[part]}
          </button>
        ))}
      </div>
      {activePart !== "vocal" && (
        <Badge
          tone={
            partBinding?.mode === "mute"
              ? "neutral"
              : partBinding?.mode === "original"
                ? "blue"
                : partBinding?.selected_candidate_id
                  ? ""
                  : "orange"
          }
        >
          {partBinding?.mode === "mute"
            ? "此声部静音"
            : partBinding?.mode === "original"
              ? "保留原声"
              : partBinding?.selected_candidate_id
                ? "已有转换候选"
                : "待转换 · 当前保留原声"}
        </Badge>
      )}
    </div>
  );
  const separationEngine = engines.find((e) => e.id === "separation");
  const harmonyModels: Bag[] =
    separationEngine?.harmony_models ||
    separationEngine?.capabilities?.harmony_models ||
    [];
  const harmonyReady =
    (!!separationEngine?.capabilities?.lead_backing_separation ||
      separationEngine?.lead_backing_separation === true) &&
    harmonyModels.some((m) => m.status === "ready");
  const combinedSources =
    project?.assets.filter(
      (a) => ["vocal", "vocals"].includes(a.stem_role || "") && isAudio(a),
    ) || [];
  const combinedSource =
    combinedSources.find(
      (a) =>
        a.id ===
        (harmonySourceId || project?.parameters.harmony_source_artifact_id),
    ) || combinedSources.at(-1);
  const activeHarmonyModel =
    harmonyModels.find((m) => m.id === (project?.parameters.harmony_model_id || harmonyModelId)) ||
    harmonyModels.find((m) => m.status === "ready") ||
    harmonyModels[0];
  const parts = project?.voice_parts || {};
  const bothPartsReady =
    !!parts.lead_vocal?.source_artifact_id &&
    !!parts.backing_vocal?.source_artifact_id;
  const partModeControls =
    activePart !== "vocal" && partBinding ? (
      <Field
        label={`${roleNames[activePart]}在混音中的处理`}
        hint="只改变当前声部；另一个声部与伴奏保持各自选择。"
      >
        <select
          value={partBinding.mode}
          onChange={(e) => patchPart(activePart, { mode: e.target.value })}
        >
          <option value="convert">使用转换候选</option>
          <option value="original">保留原声</option>
          <option value="mute">静音此声部</option>
        </select>
      </Field>
    ) : null;
  const harmonyPanel = (
    <Card
      title="第二步 · 拆分主唱与和声"
      icon="layers"
      aside={
        <Badge tone={harmonyReady ? "" : "orange"}>
          {harmonyReady ? "专用执行器可用" : "专用模型需要准备"}
        </Badge>
      }
    >
      <div className="card-pad">
        <p className="info-inline">
          输入是一条已去伴奏的混合人声。分离结果代表主唱组与和声组，不等于识别了每一个演唱者；串音与残留需要独听确认。
        </p>
        <div className="field-grid">
          <Field label="包含主唱与和声的干人声">
            <select
              value={combinedSource?.id || ""}
              onChange={(e) => {
                setHarmonySourceId(e.target.value);
                globalParam({ harmony_source_artifact_id: e.target.value });
              }}
            >
              <option value="">先分离原曲或导入混合干人声</option>
              {combinedSources.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="主唱 / 和声分离模型">
            <select
              value={activeHarmonyModel?.id || ""}
              onChange={(e) => {
                setHarmonyModelId(e.target.value);
                globalParam({ harmony_model_id: e.target.value });
              }}
            >
              <option value="">尚无可用专用模型</option>
              {harmonyModels.map((m) => (
                <option key={m.id} value={m.id} disabled={m.status !== "ready"}>
                  {m.name} · {stateLabel[m.status] || m.status}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <SeparationPanel
          harmony
          parameters={project?.parameters || {}}
          onChange={globalParam}
        />
        {activeHarmonyModel && (
          <p className="info-inline">
            {activeHarmonyModel.semantic_warning ||
              activeHarmonyModel.warning ||
              "按模型清单映射声部，不根据 Karaoke 名称猜测输出。"}
            {activeHarmonyModel.reason ? " " + activeHarmonyModel.reason : ""}
          </p>
        )}
        <div className="row wrap">
          <button
            className="button primary"
            disabled={
              !combinedSource ||
              !harmonyReady ||
              activeHarmonyModel?.status !== "ready" ||
              !!busy
            }
            onClick={() =>
              void guard(
                () =>
                  job(
                    "separate_harmony",
                    { harmony_model_id: activeHarmonyModel?.id },
                    combinedSource?.id,
                  ),
                "提交主唱与和声分离",
              )
            }
          >
            <Icon name="layers" />
            分离主唱与和声
          </button>
          <button
            className="button ghost"
            onClick={() => chooseImport("lead_vocal")}
          >
            导入主唱分轨
          </button>
          <button
            className="button ghost"
            onClick={() => chooseImport("backing_vocal")}
          >
            导入和声分轨
          </button>
          {combinedSource && (
            <button
              className="text-button"
              onClick={() => {
                setListenId(combinedSource.id);
                tell("正在监听拆分前的混合干人声。");
              }}
            >
              <Icon name="headphones" />
              混合人声对照
            </button>
          )}
        </div>
        <div className="voice-part-cards">
          {(["lead_vocal", "backing_vocal"] as const).map((part) => {
            const binding = parts[part],
              a = project?.assets.find(
                (x) => x.id === binding?.source_artifact_id,
              );
            return (
              <div className={`voice-part-card ${part}`} key={part}>
                <div className="flex-between">
                  <strong>{roleNames[part]}</strong>
                  <Badge tone={a ? "blue" : "neutral"}>
                    {a ? kindNames[a.processing_kind] : "尚未产生"}
                  </Badge>
                </div>
                <p>{a ? a.name : "完成真实分离，或显式导入此声部。"}</p>
                {a && (
                  <>
                    <small>
                      {clock(duration(a))} · {a.sample_rate / 1000} kHz ·
                      输入血缘已保留
                    </small>
                    <div className="row wrap space-top">
                      <button
                        className="button small"
                        onClick={() => {
                          setListenId(a.id);
                          tell(`正在独听原始${roleNames[part]}分轨。`);
                        }}
                      >
                        <Icon name="headphones" />
                        独听原声
                      </button>
                      <button
                        className="button small ghost"
                        onClick={() => {
                          focusPart(part);
                          setStep(2);
                        }}
                      >
                        编辑音高
                      </button>
                      <button
                        className="text-button"
                        onClick={() => {
                          focusPart(part);
                          setStep(3);
                        }}
                      >
                        选择独立声音
                      </button>
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </Card>
  );
  const partVoicesPanel = hasVoiceParts ? (
    <div className="part-summary" aria-label="项目声部状态">
      {(["lead_vocal", "backing_vocal"] as const).map((part) => {
        const binding = parts[part];
        const selected = project?.candidates.find(
          (c) => c.id === binding?.selected_candidate_id,
        );
        return (
          <div key={part}>
            <strong>{roleNames[part]}</strong>
            <span>
              {binding?.mode === "mute"
                ? "静音"
                : binding?.mode === "original"
                  ? "保留原声"
                  : selected?.name || "待生成完整版本"}
            </span>
            <small>
              {models.find((m) => m.id === binding?.parameters.model_id)
                ?.name || "尚未选择模型"}
            </small>
          </div>
        );
      })}
    </div>
  ) : null;
  const waveformCard = (tracks?: Track[]) => {
    const rows = (
      tracks ||
      project?.tracks.filter((t) => !["pitch"].includes(t.role)) ||
      []).filter((t) => !t.mute || !project?.candidates.some(c => c.archived && c.artifact_id === t.artifact_id) || project?.candidates.some(c => !c.archived && c.artifact_id === t.artifact_id));
    return (
      <Card
        title="同一条时间轴"
        icon="wave"
        aside={
          <div className="row">
            <span className="mini-note">拖动选择区间 · 单击定位</span>
            <button
              className="icon-button"
              aria-label="缩小时间轴"
              onClick={() => setZoom((z) => Math.max(1, z / 2))}
            >
              −
            </button>
            <output>{zoom}×</output>
            <button
              className="icon-button"
              aria-label="放大时间轴"
              onClick={() => setZoom((z) => Math.min(8, z * 2))}
            >
              +
            </button>
          </div>
        }
      >
        {!rows.length ? (
          <Empty title="这里还没有音频">
            <p>导入音频后，真实波形会显示在这里。</p>
          </Empty>
        ) : (
          <div className="wave-editor">
            {rows.map((track, i) => {
              const a = project?.assets.find((x) => x.id === track.artifact_id);
              if (!a || !isAudio(a)) return null;
              return (
                <div
                  className={`wave-row ${selectedTrack === track.id ? "selected" : ""}`}
                  key={track.id}
                >
                  <button
                    className="track-label"
                    onClick={() => {
                      setSelectedTrack(track.id);
                      if (step === 4) setMixMonitor(track.id);
                      else setListenId(a.id);
                    }}
                    onDoubleClick={() => {
                      if (step === 4) setMixMonitor(track.id);
                      else { setListenId(a.id); void prepareAudioGraph().then(() => audio.current?.play()).catch(() => {}); }
                    }}
                  >
                    <strong>
                      {track.name || roleNames[track.role] || track.role}
                    </strong>
                    <small>{kindNames[a.processing_kind]}</small>
                    <span className="track-role">
                      {roleNames[track.role] || track.role}
                    </span>
                  </button>
                  <Waveform
                    artifact={a}
                    selection={selection}
                    playhead={playhead}
                    timelineDuration={maxDuration}
                    offset={seconds(
                      track.offset_ticks ?? a.timeline_offset_ticks ?? 0,
                    )}
                    onSelect={(s) => update({ selection: s })}
                    onSeek={seek}
                    zoom={zoom}
                    color={["#c5eb85", "#8bb9d3", "#b9a6db", "#e6ae7f"][i % 4]}
                  />
                </div>
              );
            })}
          </div>
        )}
        <div className="segment-band">
          选区{" "}
          <strong>
            {clock(seconds(selection.start))} — {clock(seconds(selection.end))}
          </strong>
          <span>
            · {((selection.end - selection.start) / 48000).toFixed(2)} 秒
          </span>
        </div>
      </Card>
    );
  };
  const modelField = (
    <div id="conversion-model">
      {selectorFor(activePart, false, "目标声音与版本")}
    </div>
  );
  const inputChoices =
    project?.assets.filter(
      (a) =>
        isAudio(a) &&
        (["vocal", "vocals", "guide", "synthesized"].includes(
          a.stem_role || "",
        ) ||
          (a.stem_role === "original" &&
            (project.task === "speech" || project.parameters.skip_separation))),
    ) || [];
  const sourceSelector = (
    <Card title="1 · 选择输入与音高" icon="wave">
      <div className="card-pad">
        <Field
          label="本次转换输入"
          hint={
            activePart === "vocal"
              ? "从不可变原始/整理后素材开始；旧候选不会自动成为下一轮输入。"
              : "此声部沿用已经绑定的分轨；另一声部不受影响。"
          }
        >
          {activePart === "vocal" ? (
            <select
              id="conversion-source"
              value={project?.parameters.source_artifact_id || vocal?.id || ""}
              onChange={(e) =>
                globalParam({
                  source_artifact_id: e.target.value,
                  track_id: null,
                  clip_id: null,
                  ignore_clip_bindings: false,
                })
              }
            >
              <option value="">先导入干人声、分离产物或新导唱</option>
              {project?.parameters.source_artifact_id &&
                !inputChoices.some(
                  (a) => a.id === project.parameters.source_artifact_id,
                ) && (
                  <option value={project.parameters.source_artifact_id}>
                    所选输入缺失或当前任务不适用
                  </option>
                )}
              {inputChoices.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {roleNames[a.stem_role || ""] || "音频"} ·{" "}
                  {clock(duration(a))}
                </option>
              ))}
            </select>
          ) : (
            <output className="bound-input">
              {vocal?.name || "当前声部还没有有效分轨"}
            </output>
          )}
        </Field>
        <div className="input-facts">
          <span>完整输入 {clock(duration(vocal))}</span>
          <span>
            {lastPitch
              ? `${pitchData?.estimator || "已检测"} · ${editorPitchEdits.filter((e) => e.locked).length} 个锁定区`
              : "尚无检测曲线；生成试听会先执行独立音高分析"}
          </span>
        </div>

        {activePart === "vocal" && !vocal && (
          <div className="row wrap">
            <button
              className="button"
              onClick={() =>
                chooseImport(project?.task === "rewrite" ? "guide" : "vocal")
              }
            >
              导入{project?.task === "rewrite" ? "新导唱" : "干人声"}
            </button>
            <button className="text-button" onClick={() => setStep(1)}>
              先整理素材
            </button>
          </div>
        )}
        {editorPitchEdits.length > 0 && (
          <p className="info-inline">
            更换输入会保留手工编辑。请先核对时间位置与锁定曲线，旧候选继续保留。
          </p>
        )}
      </div>
    </Card>
  );
  const scopeSummary =
    activePart === "vocal"
      ? currentContext.scope === "clip"
        ? "选区覆盖"
        : currentContext.scope === "track"
          ? "轨道覆盖"
          : "工程默认"
      : `${roleNames[activePart]}独立绑定`;
  const scopeControls = (
    <div className="binding-scope">
      <div className="parameter-row">
        <span>当前生效范围</span>
        <strong>{scopeSummary}</strong>
      </div>
      {activePart === "vocal" && (
        <details open={!!project?.view_preferences?.scope_details}>
          <summary
            onClick={(e) => {
              e.preventDefault();
              updateView({
                scope_details: !project?.view_preferences?.scope_details,
              });
            }}
          >
            工程与当前音轨的模型配置
          </summary>
          <p className="info-inline">
            覆盖只影响对应输入与范围。恢复继承不会删除原始模型或旧候选。
          </p>
          <div className="row wrap">
            {(["project", "track"] as const).map((scope) => (
              <button
                key={scope}
                className="quiet-button"
                disabled={
                  !editorParameters.model_id ||
                  (scope !== "project" && !currentContext.track) ||
                  !!busy
                }
                onClick={() =>
                  void guard(async () => {
                    const m = await request<Bag>(
                      `/catalog/models/${encodeURIComponent(editorParameters.model_id)}`,
                    );
                    await bindVersion(m, editorParameters.index_id, scope);
                    tell("模型版本与适用范围已固定。");
                  }, "保存模型覆盖")
                }
              >
                {scope === "project" ? "设为工程默认" : "覆盖当前轨道"}
              </button>
            ))}
          </div>
          {currentContext.scope !== "project" && (
            <button
              className="text-button space-top"
              onClick={() =>
                void guard(async () => {
                  await flush();
                  const p = pRef.current!;
                  const ctx = editorContext(p, "vocal");
                  const r = await post<Bag>(
                    `/projects/${p.id}/model-selection/clear`,
                    {
                      revision: p.revision,
                      idempotency_key: uid(),
                      scope: ctx.scope,
                      ...(ctx.scope === "clip"
                        ? { clip_id: ctx.targetId }
                        : { track_id: ctx.targetId }),
                    },
                  );
                  applyProject(r.project);
                  tell("已清除这一层覆盖，恢复上层版本。");
                })
              }
            >
              清除此{currentContext.scope === "clip" ? "选区" : "轨道"}
              覆盖，恢复继承
            </button>
          )}
        </details>
      )}
    </div>
  );
  const addPitchEdit = (operation: string, extra: Bag = {}) => {
    if (selection.end <= selection.start) {
      tell("先选择一段非零长度的区间。");
      return;
    }
    if (editorPitchEdits.some(e => e.locked && e.start_tick < selection.end && e.end_tick > selection.start)) {
      setError("选区包含已锁定区域。请先在目标编辑记录中解除锁定，再应用修改。"); return;
    }
    update({
      pitch_edits: [
        ...(editorPitchEdits || []),
        {
          id: uid(),
          start_tick: selection.start,
          end_tick: selection.end,
          operation,
          locked: lock,
          ...extra,
        },
      ],
    });
    tell("目标曲线已保存；重新生成候选后才能听到修改。");
  };
  const patchTrack = (id: string, changes: Bag) =>
    update({ track_updates: { [id]: changes } });
  const record = () =>
    guard(async () => {
      if (recording) {
        recorder.current?.stop();
        setRecording(false);
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error(
          "当前 WebView 不支持录音。可用系统录音后导入新词导唱。",
        );
      await ensureProject();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      const r = new MediaRecorder(stream);
      recorder.current = r;
      chunks.current = [];
      r.ondataavailable = (e) => {
        if (e.data.size) chunks.current.push(e.data);
      };
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const file = new File(
          chunks.current,
          `导唱-${new Date().toISOString().replace(/[:.]/g, "-")}.webm`,
          { type: r.mimeType || "audio/webm" },
        );
        void guard(() => importFile(file, "guide"), "正在保存导唱");
      };
      r.start();
      setRecording(true);
      tell("正在录制新词导唱，完成后点击停止。");
    });

  const renderSvs = () =>
    guard(async () => {
      if (!project || !editorNotes.length)
        throw new Error("先在“人工音符”页添加音符、时间与日语音节。");
      if (!svsModelId) throw new Error("兼容 SVS 模型尚未就绪。");
      const notes = editorNotes.map((n) => {
        const unit = project.lyrics.find(
          (l) => l.start <= n.start && l.end > n.start,
        );
        const lyric = n.lyric || unit?.text || "";
        if (!lyric && !n.phonemes && !unit?.pronunciation)
          throw new Error(
            "每个演唱音符需要日语假名或显式音素；请检查空白音节。",
          );
        return {
          start_tick: n.start,
          end_tick: n.end,
          pitch_midi_float: n.pitch_midi_float,
          lyric,
          ...(n.phonemes || unit?.pronunciation
            ? {
                phonemes: String(n.phonemes || unit?.pronunciation)
                  .split(/\s+/)
                  .filter(Boolean),
              }
            : {}),
        };
      });
      await job("svs", {
        svs_model_id: svsModelId,
        language: "ja",
        notes,
        duration_ticks: Math.max(
          ticks(maxDuration),
          ...notes.map((n) => n.end_tick),
        ),
        steps: 10,
      });
    }, "提交歌声合成");

  const artifactLabel = (artifact: Artifact) => {
    const version = project?.candidates.find(
      (candidate) => candidate.artifact_id === artifact.id,
    );
    if (version)
      return `${version.name} · ${version.preview_only ? "音高试听" : "完整声部"}${version.archived ? " · 已归档" : ""} · ${clock(duration(artifact))}`;
    const generation = project?.separation_runs?.find((run: Bag) =>
      run.artifact_ids.includes(artifact.id),
    );
    if (generation)
      return `${generation.name} · ${roleNames[artifact.stem_role || ""] || artifact.name}`;
    if (artifact.stem_role === "mix")
      return `混音版本 ${(project?.assets.filter((a) => a.stem_role === "mix").findIndex((a) => a.id === artifact.id) ?? 0) + 1} · ${clock(duration(artifact))} · ${new Date(artifact.created_at).toLocaleTimeString("zh-CN")}`;
    return `${artifact.name} · ${kindNames[artifact.processing_kind]}`;
  };
  let content: ReactNode;
  if (step === 0)
    content = (
      <div className="layout-split">
        <div className="stack">
          <Card className="card-pad">
            <div className="section-caption" id="task-setup">
              你想完成什么？<span>先确认任务，再选择素材角色</span>
            </div>
            <div className="task-grid">
              {[
                ["cover", "note", "歌曲翻唱", "原词 · 换一种音色"],
                ["speech", "mic", "语音变声", "对话 · 口播 · 配音"],
                ["rewrite", "spark", "改词翻唱", "新歌词 · 新演唱来源"],
              ].map(([id, icon, title, sub]) => (
                <button
                  className={`task-card ${project?.task === id ? "active" : ""}`}
                  key={id}
                  disabled={connection !== "ready" || !!busy}
                  onClick={() =>
                    void guard(async () => {
                      await ensureProject();
                      update({ task: id, parameters: { ...pRef.current!.parameters, task_confirmed: true, import_role: pRef.current!.parameters.import_role || "" } });
                    })
                  }
                >
                  <Icon name={icon} />
                  <strong>{title}</strong>
                  <small>{sub}</small>
                </button>
              ))}
            </div>
            <Field
              label="本次导入角色"
              hint="原曲含伴奏时选原曲；独立录音选干人声；改词演唱选新导唱。"
            >
              <select
                value={importRole}
                disabled={
                  !project?.parameters.task_confirmed && !project?.assets.length
                }
                onChange={(e) => globalParam({ import_role: e.target.value })}
              >
                <option value="">请选择本次素材角色</option>
                <option value="original">原曲 · 含伴奏</option>
                <option value="vocal">干人声 · 无伴奏</option>
                <option value="guide">新导唱 · 唱着新歌词</option>
                <option value="instrumental">已有伴奏</option>
                <option value="lead_vocal">独立主唱</option>
                <option value="backing_vocal">独立和声</option>
              </select>
            </Field>
            <div
              className={`drop-zone ${busy ? "working" : ""}`}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const f = e.dataTransfer.files[0];
                if (f && !setupReady)
                  tell("请先确认任务和素材角色，再拖入音频。");
                if (f && setupReady)
                  void guard(() => importFile(f, importRole), "正在导入与解码");
              }}
            >
              <span className="drop-icon">
                <Icon name="import" />
              </span>
              <div>
                <h2>{source?.name || "把你的音频，放到这里。"}</h2>
                <p>WAV / MP3 / FLAC / M4A 等 · 在本机解码与处理</p>
                <div className="row wrap">
                  <button
                    className="button primary"
                    disabled={!setupReady || !!busy || connection !== "ready"}
                    onClick={() => chooseImport()}
                  >
                    <Icon name="import" />
                    选择音频
                  </button>
                  <button
                    className="text-button"
                    disabled={!setupReady || !!busy || connection !== "ready"}
                    onClick={() => {
                      roleRef.current = importRole;
                      setModal("import-path");
                    }}
                  >
                    从本机路径导入
                  </button>
                  <button
                    className="text-button"
                    disabled={!setupReady || !!busy || connection !== "ready"}
                    onClick={() =>
                      void guard(async () => {
                        const p = await ensureProject();
                        const r = await post<Bag>(`/projects/${p.id}/demo`);
                        applyProject(r.project);
                        setListenId(r.artifact.id);
                        tell(
                          "已载入合成技术样本；它用于验证信号链，不代表真实歌声音质。",
                        );
                      }, "载入技术样本")
                    }
                  >
                    体验技术样本 <Icon name="arrow" />
                  </button>
                </div>
              </div>
            </div>
          </Card>
          <Card
            title="素材速览"
            icon="info"
            aside={<Badge tone="neutral">实际文件测量</Badge>}
          >
            {source ? (
              <>
                <div className="mini-stats">
                  <div className="stat">
                    <span>音频长度</span>
                    <strong>{clock(duration(source))}</strong>
                  </div>
                  <div className="stat">
                    <span>采样率 / 声道</span>
                    <strong>
                      {source.sample_rate / 1000}
                      <small> kHz / {source.channels}</small>
                    </strong>
                  </div>
                  <div className="stat">
                    <span>采样峰值</span>
                    <strong>
                      {source.diagnostics?.sample_peak_dbfs != null
                        ? `${Number(source.diagnostics.sample_peak_dbfs).toFixed(1)} dBFS`
                        : "尚未测量"}
                    </strong>
                  </div>
                </div>
                <div className="issue-list">
                  <div className="issue-item">
                    <span className="issue-index">✓</span>
                    <div className="issue-text">
                      <strong>已解码，可试听与非破坏裁剪</strong>
                      <small>
                        {source.sample_count.toLocaleString()} 个采样 ·
                        原文件哈希 {source.sha256?.slice(0, 14)}…
                      </small>
                    </div>
                  </div>
                  {source.processing_kind === "demo" && (
                    <div className="issue-item">
                      <span className="issue-index">i</span>
                      <div className="issue-text">
                        <strong>当前素材为合成技术样本</strong>
                        <small>
                          可以验证采样、时序与模型调用，不能替代真实歌声听感验收。
                        </small>
                      </div>
                    </div>
                  )}
                  {(source.diagnostics?.silence_regions || [])
                    .slice(0, 5)
                    .map((r: Bag, i: number) => (
                      <div className="issue-item" key={i}>
                        <span className="issue-index">{i + 1}</span>
                        <div className="issue-text">
                          <strong>检测到低电平区间</strong>
                          <small>
                            {clock(seconds(r.start_tick))} —{" "}
                            {clock(seconds(r.end_tick))} · 确定性测量
                          </small>
                        </div>
                        <button
                          className="text-button"
                          onClick={() => {
                            update({
                              selection: {
                                start: r.start_tick,
                                end: r.end_tick,
                              },
                            });
                            seek(seconds(r.start_tick));
                            setLoop(true);
                          }}
                        >
                          定位试听 <Icon name="play" />
                        </button>
                      </div>
                    ))}
                  {source.diagnostics?.clipping_samples > 0 && (
                    <div className="risk-card">
                      <span>!</span>
                      <div>
                        <strong>检测到接近满幅的采样</strong>
                        <p>
                          {source.diagnostics.clipping_samples}{" "}
                          个采样，建议试听确认削波。
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <Empty title="等待你的第一条音频">
                <p>媒体属性与诊断会在导入后出现。</p>
              </Empty>
            )}
          </Card>
          {waveformCard()}
        </div>
        <aside className="inspector stack">
          <Card title="当前项目" icon="folder">
            <div className="card-pad">
              <strong>{project?.name || "新项目"}</strong>
              <div className="parameter-row">
                <span>素材数量</span>
                <b>{project?.assets.filter(isAudio).length || 0}</b>
              </div>
              <div className="parameter-row">
                <span>音轨数量</span>
                <b>{project?.tracks.length || 0}</b>
              </div>
              <div className="parameter-row">
                <span>完整生成版本</span>
                <b>
                  {project?.candidates.filter((c) => !c.preview_only).length ||
                    0}
                </b>
              </div>
              <p className="info-inline">
                确认素材后，按六阶段完成分离、音高、转换、混音和导出。
              </p>
            </div>
          </Card>
        </aside>
      </div>
    );
  else if (step === 1)
    content = (
      <div className="layout-split">
        <div className="stack">
          {waveformCard(
            project?.tracks.filter(
              (t) =>
                t.artifact_id === source?.id ||
                t.artifact_id === project.parameters.source_artifact_id ||
                (t.role === "instrumental" && !t.mute) ||
                Object.values(parts).some(
                  (binding) => binding?.source_artifact_id === t.artifact_id,
                ),
            ),
          )}
          <Card className="card-pad">
            <SeparationPanel
              parameters={project?.parameters || {}}
              onChange={globalParam}
              models={separationEngine?.models || []}
            />

            <div className="row wrap space-top">
              <button
                className="button primary"
                disabled={!source || !!busy || !engineReady("separation")}
                onClick={() =>
                  void guard(
                    () => job("separate", {}, source?.id),
                    "提交分离任务",
                  )
                }
              >
                <Icon name="layers" />
                分离人声与伴奏
              </button>
              <button
                className="button ghost"
                onClick={() => chooseImport("vocal")}
              >
                导入干人声
              </button>
              <button
                className="button ghost"
                onClick={() => chooseImport("instrumental")}
              >
                导入伴奏
              </button>
              <button
                className="text-button"
                disabled={!source}
                onClick={() => {
                  globalParam({
                    skip_separation: true,
                    source_artifact_id: (vocal || source)?.id,
                  });
                  setStep(2);
                }}
              >
                确认输入是干人声，跳过分离 <Icon name="arrow" />
              </button>
            </div>
          </Card>
          {project && (
            <SeparationVersions
              project={project}
              operation="separate"
              busy={!!busy}
              flush={flush}
              onProject={(p) => {
                if (pRef.current?.id === p.id) {
                  applyProject(p);
                  setListenId("");
                  setHarmonySourceId("");
                }
              }}
              onListen={(id) => setListenId(id)}
              onError={setError}
            />
          )}
          {harmonyPanel}
          {project && (
            <SeparationVersions
              project={project}
              operation="separate_harmony"
              busy={!!busy}
              flush={flush}
              onProject={(p) => {
                if (pRef.current?.id === p.id) {
                  applyProject(p);
                  setListenId("");
                  setHarmonySourceId("");
                }
              }}
              onListen={(id) => setListenId(id)}
              onError={setError}
            />
          )}
          <div className="risk-card">
            <span className="risk-icon">i</span>
            <div>
              <h3>主唱与和声分离检查</h3>
              <p>
                人声 /
                伴奏分离不会自动分出主唱与和声。出现音高争夺时，先独听人声、确认声部，再比较算法或修曲线。
              </p>
            </div>
          </div>
        </div>
        <aside className="inspector stack">
          <Card className="card-pad">
            <div className="inspector-heading">
              <strong>处理策略</strong>
              <span>SEPARATION</span>
            </div>
            <p className="info-inline">
              当前分离执行器：audio-separator / MelBand
              RoFormer。实际权重与输出语义记录在产物中。
            </p>
            <Badge tone={engineReady("separation") ? "" : "orange"}>
              {engineReady("separation")
                ? "分离执行器已准备"
                : "分离执行器需要准备"}
            </Badge>
            <div className="line" />
            <button
              className="button full ghost"
              onClick={() => {
                setListenId(source?.id || "");
                tell("正在监听原始素材。");
              }}
              disabled={!source}
            >
              旁路处理，监听原音
            </button>
            <button
              className="button full ghost space-top"
              onClick={() => {
                setListenId(vocal?.id || "");
                tell("正在监听当前人声素材。");
              }}
              disabled={!vocal}
            >
              监听当前人声
            </button>
          </Card>
        </aside>
      </div>
    );
  else if (step === 2)
    content = (
      <>
        {partSelector}
        <div className="step-tabs">
          {[
            ["pitch", "音高曲线"],
            ["notes", "人工音符"],
            ["lyrics", "歌词与新导唱"],
          ]
            .filter(
              ([id]) =>
                (!hasVoiceParts && activePart === "vocal") || id !== "lyrics",
            )
            .map(([id, title]) => (
              <button
                key={id}
                className={pitchTab === id ? "active" : ""}
                onClick={() => setPitchTab(id)}
              >
                {title}
              </button>
            ))}
        </div>
        {pitchTab === "pitch" ? (
          <div className="layout-split">
            <div className="stack">
              <Card
                title="音高编辑器"
                icon="note"
                aside={
                  <Badge tone="blue">
                    {pitchData
                      ? `${pitchData.estimator} · 实际检测`
                      : "等待分析"}
                  </Badge>
                }
              >
                <div className="editor-toolbar">
                  <div className="editor-tools">
                    <button
                      className={`tool-button ${pitchTool === "select" ? "active" : ""}`}
                      onClick={() => setPitchTool("select")}
                    >
                      选择区间
                    </button>
                    <button
                      className={`tool-button ${pitchTool === "point" ? "active" : ""}`}
                      disabled={!pitchData}
                      onClick={() => setPitchTool("point")}
                    >
                      连续绘制目标
                    </button>
                  </div>
                  <span className="mini-note">目标编辑在重新渲染后生效</span>
                </div>
                {pitchData && lastPitch && vocal ? (
                  <PitchCanvas
                    key={`${project!.id}:${activePart}:${lastPitch.id}`}
                    data={pitchData}
                    outputData={outputPitchData}
                    projectId={project!.id}
                    pitchId={lastPitch!.id}
                    sourceId={vocal!.id}
                    parameters={editorParameters}
                    stale={pitchAnalysis?.state === "stale"}
                    onTargetHash={setPreviewTargetHash}
                    onSeek={seek}
                    view={
                      project?.view_preferences?.pitch_views?.[activePart] || {}
                    }
                    onView={(v) =>
                      update({
                        view_preferences: {
                          ...pRef.current?.view_preferences,
                          pitch_views: {
                            ...pRef.current?.view_preferences?.pitch_views,
                            [activePart]: v,
                          },
                        },
                      })
                    }
                    timelineDuration={maxDuration}
                    selection={selection}
                    edits={editorPitchEdits || []}
                    notes={editorNotes || []}
                    playhead={playhead}
                    onSelect={(s) => update({ selection: s })}
                    tool={pitchTool}
                    onStroke={(stroke) => {
                      if (stroke.some(e => editorPitchEdits.some(old => old.locked && old.start_tick < e.end_tick && old.end_tick > e.start_tick))) {
                        setError("绘制范围包含已锁定区域。请先在目标编辑记录中解除该区域锁定，再绘制。"); return;
                      }
                      const strokeId = uid();
                      update({ pitch_edits: [...editorPitchEdits,
                      ...stroke.map(e => ({ ...e, id: uid(), stroke_id: strokeId, locked: lock }))] });
                    }}
                  />
                ) : (
                  <Empty title="先检测真实的人声音高" icon="note">
                    <p>不会生成示例曲线来代替你的素材分析。</p>
                  </Empty>
                )}
                <div className="card-pad">
                  {selectionControls}
                  <div className="pitch-preview-flow">
                    <strong>编辑 → 生成选区试听 → 对比实际输出</strong>
                    <p>
                      绿色曲线包含移调、滤波、自动音准、人工音符和锁定编辑；旧音频不会随编辑自动改变。
                    </p>
                    <Field label="音高编辑对照候选">
                      <select
                        value={candidate?.id || ""}
                        onChange={(e) => {
                          setAuditionCandidateIds((previous) => ({
                            ...previous,
                            [auditionScope]: e.target.value,
                          }));
                          setListenId("");
                        }}
                      >
                        <option value="">尚未生成试听</option>
                        {[...candidates].reverse().map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                            {c.preview_only ? " · 片段" : " · 完整"}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <div className="row wrap">
                      <button
                        className="button primary"
                        disabled={
                          !currentModel ||
                          !pitchData ||
                          !!busy ||
                          selection.end <= selection.start ||
                          (activePart !== "vocal" &&
                            partBinding?.mode !== "convert")
                        }
                        onClick={() => void refreshPitchPreview()}
                      >
                        生成编辑后的选区试听
                      </button>
                      <button
                        className="button small"
                        disabled={!vocal}
                        onClick={() => {
                          setListenId(vocal!.id);
                          setLoop(true);
                          seek(seconds(selection.start));
                        }}
                      >
                        A · 原始人声
                      </button>
                      <button
                        className="button small"
                        disabled={!comparisonB}
                        onClick={() => {
                          setListenId(comparisonB!.id);
                          setLoop(true);
                          seek(seconds(selection.start));
                        }}
                      >
                        B · 转换结果
                      </button>
                      <button
                        className="button small"
                        disabled={!comparisonB || !!busy}
                        onClick={() =>
                          void guard(
                            () =>
                              job(
                                "pitch",
                                {
                                  pitch_algorithm:
                                    editorParameters.pitch_algorithm || "rmvpe",
                                },
                                comparisonB!.id,
                              ),
                            "复测输出音高",
                          )
                        }
                      >
                        复测输出音高
                      </button>
                    </div>
                    <p role="status">
                      {candidate
                        ? `对照：${candidate.name}。${candidateDiff(candidate).length || (previewTargetHash && candidate.requested_f0_hash !== previewTargetHash) ? "目标或设置已有变化，请重新生成试听；当前 B 是历史音频。" : "请结合输出复测曲线与实际试听判断效果。"}`
                        : currentModel
                          ? "当前选区尚无试听结果，可生成编辑后的选区试听。"
                          : "请先在声音转换阶段选择模型。"}
                    </p>
                  </div>
                </div>
              </Card>
              <Card
                title="目标编辑记录"
                aside={
                  <span className="mini-note">独立于检测缓存 · 可撤销</span>
                }
              >
                <div className="edit-list">
                  {!editorPitchEdits.length ? (
                    <p className="info-inline">
                      尚无人工修改。检测曲线保留为原始参考。
                    </p>
                  ) : (
                    editorEditGroups.map((e, i) => (
                      <div className="edit-row" key={e.id}>
                        <span>
                          {i + 1}. {clock(seconds(e.start_tick))}–
                          {clock(seconds(e.end_tick))}
                        </span>
                        <strong>
                          {e.stroke_id
                            ? `连续绘制 · ${e.member_ids.length} 帧`
                            : e.operation === "transpose"
                              ? `${e.semitones > 0 ? "+" : ""}${e.semitones} 半音`
                              : e.operation === "set_hz"
                                ? `${Number(e.hz).toFixed(1)} Hz`
                                : "设为清音"}
                        </strong>
                        <button
                          className={`chip ${e.locked ? "active" : ""}`}
                          onClick={() =>
                            update({
                              pitch_edits: editorPitchEdits.map((x) =>
                                e.member_ids.includes(x.id)
                                  ? {
                                      ...x,
                                      locked: !x.locked,
                                      snapshot: undefined,
                                    }
                                  : x,
                              ),
                            })
                          }
                        >
                          {e.locked ? "已锁定" : "未锁定"}
                        </button>
                        <button
                          className="text-button"
                          onClick={() =>
                            update({
                              pitch_edits: editorPitchEdits.filter(
                                (x) => !e.member_ids.includes(x.id),
                              ),
                            })
                          }
                        >
                          恢复此处
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </Card>
            </div>
            <aside className="inspector stack">
              <Card className="card-pad">
                <div className="inspector-heading">
                  <strong>音高检查器</strong>
                  <span>TARGET F0</span>
                </div>
                <RvcParameterPanel
                  section="pitch"
                  modelId={editorParameters.model_id}
                  task={project?.task || "speech"}
                  values={{
                    ...editorParameters,
                    range_ticks: [selection.start, selection.end],
                  }}
                  mode={mode}
                  preferences={
                    project?.view_preferences?.expanded_panels?.[activePart] ||
                    {}
                  }
                  onPreferences={(changes) => {
                    const p = pRef.current;
                    updateView({
                    expanded_panels: {
                      ...p?.view_preferences?.expanded_panels,
                      [activePart]: {
                        ...p?.view_preferences?.expanded_panels?.[activePart],
                        ...changes,
                      },
                    },
                  });
                  }}
                  onChange={applyRvcChanges}
                  onError={setError}
                  busy={!!busy}
                  pitchInfo={{
                    artifact_id: lastPitch?.id,
                    estimator: pitchData?.estimator,
                    locked_count: editorPitchEdits.filter((e) => e.locked)
                      .length,
                    edits_count: editorPitchEdits.length,
                    requested_f0_hash: comparisonB?.requested_f0_hash,
                    consumed_f0_hash: comparisonB?.consumed_f0_hash,
                  }}
                />
                {pitchRecovery}
                <div className="line" />
                <Slider
                  label="选区移调"
                  value={semitones}
                  min={-24}
                  max={24}
                  step={0.01}
                  suffix="st"
                  onChange={setSemitones}
                />
                <button
                  className="button full"
                  disabled={!pitchData}
                  onClick={() => addPitchEdit("transpose", { semitones })}
                >
                  应用到目标曲线
                </button>
                <div className="row space-top">
                  <button
                    className="button small"
                    disabled={!pitchData}
                    onClick={() =>
                      addPitchEdit("transpose", { semitones: -12 })
                    }
                  >
                    降低八度
                  </button>
                  <button
                    className="button small"
                    disabled={!pitchData}
                    onClick={() => addPitchEdit("transpose", { semitones: 12 })}
                  >
                    升高八度
                  </button>
                </div>
                <Field label="目标音高（Hz）">
                  <input
                    type="number"
                    min={40}
                    max={2000}
                    step={0.1}
                    value={targetHz}
                    onChange={(e) => setTargetHz(+e.target.value)}
                  />
                </Field>
                <div className="row">
                  <button
                    className="button small"
                    disabled={!pitchData || targetHz < 40 || targetHz > 2000}
                    onClick={() => addPitchEdit("set_hz", { hz: targetHz })}
                  >
                    设定选区
                  </button>
                  <button
                    className="button small"
                    disabled={!pitchData}
                    onClick={() => addPitchEdit("unvoiced")}
                  >
                    设为清音
                  </button>
                </div>
                <label className="toggle-row">
                  <span>锁定新增人工编辑</span>
                  <input
                    type="checkbox"
                    checked={lock}
                    onChange={(e) => setLock(e.target.checked)}
                  />
                </label>
              </Card>
            </aside>
          </div>
        ) : pitchTab === "notes" ? (
          <div className="stack">
            <Card
              title="人工旋律参考"
              icon="note"
              aside={<Badge tone="purple">目标旋律 · 渲染后生效</Badge>}
            >
              <div className="card-pad">
                {activePart === "vocal" && (
                  <Field
                    label="乐谱时长（秒）"
                    hint="可以不导入音频，直接创建音符并使用兼容 SVS 声库。时长包括休止与尾部。"
                  >
                    <input
                      type="number"
                      min={0.1}
                      max={3600}
                      step={0.1}
                      value={seconds(
                        project?.parameters.timeline_duration_ticks ||
                          ticks(maxDuration),
                      )}
                      onChange={(e) => {
                        const end = ticks(
                          Math.max(0.1, Math.min(3600, +e.target.value)),
                        );
                        update({
                          parameters: {
                            ...project?.parameters,
                            timeline_duration_ticks: end,
                          },
                          ...(!selection.end
                            ? { selection: { start: 0, end } }
                            : {}),
                        });
                      }}
                    />
                  </Field>
                )}
                {selectionControls}
                <div className="row wrap space-top">
                  <button
                    className="button primary"
                    disabled={!project || selection.end <= selection.start}
                    onClick={() =>
                      update({
                        notes: [
                          ...(editorNotes || []),
                          {
                            id: uid(),
                            start: selection.start,
                            end: selection.end,
                            pitch_midi_float: 69,
                            reference_source: "manual",
                            locked: true,
                          },
                        ],
                      })
                    }
                  >
                    在选区添加音符
                  </button>
                  {activePart === "vocal" && (
                    <button
                      className="button ghost"
                      onClick={() => midiInput.current?.click()}
                    >
                      导入 MIDI
                    </button>
                  )}
                  {activePart === "vocal" && (
                    <button
                      className="button ghost"
                      disabled={!editorNotes.length}
                      onClick={() =>
                        void guard(async () => {
                          await flush();
                          await download(
                            `/projects/${project!.id}/midi`,
                            `${project!.name}.mid`,
                          );
                        })
                      }
                    >
                      导出 MIDI
                    </button>
                  )}
                  <p className="info-inline">
                    精确时间和音高均可用数值编辑。人工音符在转换时作为目标 F0
                    条件，保留原清音与休止；仍需试听确认表现。
                  </p>
                </div>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>起点 / 终点（秒）</th>
                      <th>MIDI 音高</th>
                      {!hasVoiceParts && <th>演唱音节 / 音素（SVS）</th>}
                      <th>来源</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {editorNotes.map((n) => (
                      <tr key={n.id}>
                        <td>
                          <div className="row">
                            <input
                              aria-label="音符起点"
                              type="number"
                              min={0}
                              step={0.01}
                              value={seconds(n.start)}
                              onChange={(e) =>
                                update({
                                  notes: editorNotes.map((x) =>
                                    x.id === n.id
                                      ? {
                                          ...x,
                                          start: Math.min(
                                            x.end,
                                            ticks(+e.target.value),
                                          ),
                                        }
                                      : x,
                                  ),
                                })
                              }
                            />
                            <input
                              aria-label="音符终点"
                              type="number"
                              min={seconds(n.start)}
                              step={0.01}
                              value={seconds(n.end)}
                              onChange={(e) =>
                                update({
                                  notes: editorNotes.map((x) =>
                                    x.id === n.id
                                      ? {
                                          ...x,
                                          end: Math.max(
                                            x.start,
                                            ticks(+e.target.value),
                                          ),
                                        }
                                      : x,
                                  ),
                                })
                              }
                            />
                          </div>
                        </td>
                        <td>
                          <input
                            aria-label="MIDI 音高"
                            type="number"
                            min={24}
                            max={108}
                            step={0.01}
                            value={n.pitch_midi_float}
                            onChange={(e) =>
                              update({
                                notes: editorNotes.map((x) =>
                                  x.id === n.id
                                    ? {
                                        ...x,
                                        pitch_midi_float: +e.target.value,
                                      }
                                    : x,
                                ),
                              })
                            }
                          />
                        </td>
                        {!hasVoiceParts && (
                          <td>
                            <div className="stack compact">
                              <input
                                aria-label="音符演唱音节"
                                placeholder="例：あ"
                                value={n.lyric || ""}
                                onChange={(e) =>
                                  update({
                                    notes: editorNotes.map((x) =>
                                      x.id === n.id
                                        ? { ...x, lyric: e.target.value }
                                        : x,
                                    ),
                                  })
                                }
                              />
                              <input
                                aria-label="音符显式音素"
                                placeholder="可选：a"
                                value={n.phonemes || ""}
                                onChange={(e) =>
                                  update({
                                    notes: editorNotes.map((x) =>
                                      x.id === n.id
                                        ? { ...x, phonemes: e.target.value }
                                        : x,
                                    ),
                                  })
                                }
                              />
                            </div>
                          </td>
                        )}
                        <td>人工参考</td>
                        <td>
                          <div className="row">
                            <button
                              className="text-button"
                              onClick={() =>
                                update({
                                  pitch_edits: [
                                    ...editorPitchEdits,
                                    {
                                      id: uid(),
                                      start_tick: n.start,
                                      end_tick: n.end,
                                      operation: "set_hz",
                                      hz:
                                        440 *
                                        2 ** ((n.pitch_midi_float - 69) / 12),
                                      locked: true,
                                    },
                                  ],
                                })
                              }
                            >
                              应用到目标 F0
                            </button>
                            <button
                              className="text-button"
                              onClick={() =>
                                update({
                                  notes: editorNotes.filter(
                                    (x) => x.id !== n.id,
                                  ),
                                })
                              }
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!editorNotes.length && (
                <Empty title="尚无人工音符">
                  <p>选择区间，然后添加一个参考音符。</p>
                </Empty>
              )}
            </Card>
          </div>
        ) : (
          <div className="layout-split">
            <div className="stack">
              <div className="highlight-strip">
                新歌词需要新的演唱。导入或录制新词导唱，或使用兼容声库从音符与音节合成。
              </div>
              <Card
                title="新词与原曲窗口"
                icon="note"
                aside={<Badge>锁定窗口 · 保留后续位置</Badge>}
              >
                <div className="card-pad">
                  {selectionControls}
                  <div className="row wrap space-top">
                    <button
                      className="button"
                      disabled={!project || selection.end <= selection.start}
                      onClick={() =>
                        update({
                          lyrics: [
                            ...(project?.lyrics || []),
                            {
                              id: uid(),
                              start: selection.start,
                              end: selection.end,
                              original_text: "",
                              text: "",
                              language: "zh",
                              pronunciation: "",
                              timing_policy: "locked_window",
                              minimum_duration: 0.08,
                              note_ids: [],
                              is_breath: false,
                              is_slur: false,
                            },
                          ],
                        })
                      }
                    >
                      选区添加歌词句
                    </button>
                    <button
                      className="button ghost"
                      onClick={() => lyricInput.current?.click()}
                    >
                      导入 LRC / TXT
                    </button>
                  </div>
                </div>
                <div className="lyrics-list">
                  {project?.lyrics.map((l, i) => (
                    <div className="lyric-card" key={l.id}>
                      <div className="row wrap">
                        <strong className="lyric-number">
                          {String(i + 1).padStart(2, "0")}
                        </strong>
                        <input
                          aria-label={`第${i + 1}句起点`}
                          type="number"
                          min={0}
                          step={0.01}
                          value={seconds(l.start)}
                          onChange={(e) =>
                            update({
                              lyrics: project.lyrics.map((x) =>
                                x.id === l.id
                                  ? {
                                      ...x,
                                      start: Math.min(
                                        x.end,
                                        ticks(+e.target.value),
                                      ),
                                    }
                                  : x,
                              ),
                            })
                          }
                        />
                        <span>—</span>
                        <input
                          aria-label={`第${i + 1}句终点`}
                          type="number"
                          min={seconds(l.start)}
                          step={0.01}
                          value={seconds(l.end)}
                          onChange={(e) =>
                            update({
                              lyrics: project.lyrics.map((x) =>
                                x.id === l.id
                                  ? {
                                      ...x,
                                      end: Math.max(
                                        x.start,
                                        ticks(+e.target.value),
                                      ),
                                    }
                                  : x,
                              ),
                            })
                          }
                        />
                        <button
                          className="text-button"
                          onClick={() => {
                            update({
                              selection: { start: l.start, end: l.end },
                            });
                            seek(seconds(l.start));
                            setLoop(true);
                          }}
                        >
                          定位
                        </button>
                        <button
                          className="text-button"
                          onClick={() =>
                            update({
                              lyrics: project.lyrics.filter(
                                (x) => x.id !== l.id,
                              ),
                            })
                          }
                        >
                          删除
                        </button>
                      </div>
                      <Field label="原词参考">
                        <input
                          value={l.original_text || ""}
                          maxLength={1000}
                          onChange={(e) =>
                            update({
                              lyrics: project.lyrics.map((x) =>
                                x.id === l.id
                                  ? { ...x, original_text: e.target.value }
                                  : x,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field label="新的演唱歌词">
                        <input
                          value={l.text || ""}
                          maxLength={1000}
                          onChange={(e) =>
                            update({
                              lyrics: project.lyrics.map((x) =>
                                x.id === l.id
                                  ? { ...x, text: e.target.value }
                                  : x,
                              ),
                            })
                          }
                        />
                      </Field>
                      <div className="field-grid">
                        <Field label="语言">
                          <select
                            value={l.language || "zh"}
                            onChange={(e) =>
                              update({
                                lyrics: project.lyrics.map((x) =>
                                  x.id === l.id
                                    ? { ...x, language: e.target.value }
                                    : x,
                                ),
                              })
                            }
                          >
                            <option value="zh">中文</option>
                            <option value="en">English</option>
                            <option value="mixed">混合语言</option>
                            <option value="ja">日本語</option>
                          </select>
                        </Field>
                        <Field label="发音 / 音素覆盖">
                          <input
                            value={l.pronunciation || ""}
                            placeholder="多音字、重音或音素备注"
                            onChange={(e) =>
                              update({
                                lyrics: project.lyrics.map((x) =>
                                  x.id === l.id
                                    ? { ...x, pronunciation: e.target.value }
                                    : x,
                                ),
                              })
                            }
                          />
                        </Field>
                      </div>
                      <label className="toggle-row">
                        <span>一字多音 / 连音</span>
                        <input
                          type="checkbox"
                          checked={!!l.is_slur}
                          onChange={(e) =>
                            update({
                              lyrics: project.lyrics.map((x) =>
                                x.id === l.id
                                  ? { ...x, is_slur: e.target.checked }
                                  : x,
                              ),
                            })
                          }
                        />
                      </label>
                      {l.end - l.start <
                        ticks(
                          (l.text || "").replace(/\s/g, "").length * 0.08,
                        ) && (
                        <div className="warning-inline">
                          此句窗口偏短：这里只按字符作初步提示，请核对音节、辅音与实际演唱。
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                {!project?.lyrics.length && (
                  <Empty title="先标出需要改词的句子">
                    <p>添加歌词窗口，保留原曲起止位置。</p>
                  </Empty>
                )}
              </Card>
            </div>
            <aside className="inspector stack">
              <Card className="card-pad">
                <div className="inspector-heading">
                  <strong>新演唱来源</strong>
                  <span>GUIDE TAKE</span>
                </div>
                <div className="route-card active">
                  <strong>录制 / 导入新词导唱</strong>
                  <p>已有 VC 模型即可转换这段新演唱。</p>
                </div>
                <button
                  className="button primary full space-top"
                  onClick={() => chooseImport("guide")}
                >
                  <Icon name="import" />
                  导入新词干声
                </button>
                <button
                  className={`button full space-top ${recording ? "recording" : ""}`}
                  onClick={() => void record()}
                >
                  <Icon name="mic" />
                  {recording ? "停止并保存导唱" : "录制一条导唱"}
                </button>
                <div className="line" />
                {project?.assets
                  .filter((a) =>
                    ["guide", "synthesized"].includes(a.stem_role || ""),
                  )
                  .map((a) => {
                    const track = project.tracks.find(
                      (t) => t.artifact_id === a.id,
                    );
                    return (
                      <div key={a.id}>
                        <button
                          className="take-row"
                          onClick={() => setListenId(a.id)}
                        >
                          <Icon name="play" />
                          <span>
                            {a.name}
                            <small>{clock(duration(a))}</small>
                          </span>
                        </button>
                        {track && (
                          <Field label="导唱在原曲中的起点（秒）">
                            <input
                              type="number"
                              step={0.001}
                              min={0}
                              value={seconds(track.offset_ticks || 0)}
                              onChange={(e) =>
                                patchTrack(track.id, {
                                  offset_ticks: ticks(
                                    Math.max(0, +e.target.value),
                                  ),
                                })
                              }
                            />
                          </Field>
                        )}
                      </div>
                    );
                  })}
                <div
                  className={`route-card ${engineReady("svs") ? "active" : "muted-route"}`}
                >
                  <strong>DiffSinger · 日语歌声合成</strong>
                  <p>
                    {svsEngine?.reason ||
                      "尚未配置兼容 SVS 声库、词典与声码器。当前可以使用导唱路线。"}
                  </p>
                </div>
                <button
                  className="button full space-top"
                  disabled={
                    !engineReady("svs") ||
                    !svsModelId ||
                    !editorNotes.length ||
                    !!busy
                  }
                  onClick={() => void renderSvs()}
                >
                  <Icon name="note" />
                  从音符与日语音节合成
                </button>
                <button
                  className="text-button space-top"
                  onClick={() => setPitchTab("notes")}
                >
                  编辑逐音符音节与时值 <Icon name="arrow" />
                </button>
                <p className="info-inline">
                  当前声库只验证日语假名或声库支持的显式音素。每个音符填写一个音节；空白会尝试读取对应歌词窗口。RVC
                  模型与 SVS 声库分开管理。
                </p>
              </Card>
            </aside>
          </div>
        )}
      </>
    );
  else if (step === 3)
    content = (
      <>
        {partSelector}
        {partVoicesPanel}
        <div className="layout-split">
          <div className="stack">
            {sourceSelector}
            <Card title="目标声音与模型版本" icon="box">
              <div className="card-pad">
                {modelField}
                {scopeControls}
              </div>
            </Card>
            <div className="render-actions">
              <button
                className="button primary"
                disabled={
                  !vocal ||
                  !currentModel ||
                  !!busy ||
                  (activePart !== "vocal" && partBinding?.mode !== "convert")
                }
                onClick={() => void renderCandidate(false)}
              >
                生成当前声部完整版本
              </button>
              {bothPartsReady && (
                <button
                  className="button ghost"
                  disabled={!!busy}
                  onClick={() =>
                    void guard(
                      () =>
                        job("convert_parts", {
                          parts: ["lead_vocal", "backing_vocal"],
                          mix_after: false,
                        }),
                      "生成双声部完整版本",
                    )
                  }
                >
                  按各自设置生成两声部
                </button>
              )}
              <small>使用完整输入，音高编辑随当前声部进入生成。</small>
            </div>
            {project && (
              <CandidateVersions
                project={project}
                blindLabels={blindLabels}
                onToggleBlind={() => {
                  if (blindLabels) {
                    setBlindLabels(null);
                    return;
                  }
                  const ids = scopedCandidates.map((c) => c.id);
                  for (let i = ids.length - 1; i > 0; i--) {
                    const j =
                      crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
                    [ids[i], ids[j]] = [ids[j], ids[i]];
                  }
                  setBlindLabels(
                    Object.fromEntries(
                      ids.map((id, i) => [
                        id,
                        `盲听版本 ${String.fromCharCode(65 + i)}`,
                      ]),
                    ),
                  );
                }}
                part={activePart}
                selectedId={selectedCandidateId}
                auditionId={candidate?.id}
                busy={!!busy}
                isCompatible={candidateCompatible}
                stateOf={candidateState}
                onAdopt={(c) => {
                  update({ selected_candidate_id: c.id });
                  setAuditionCandidateIds((v) => ({
                    ...v,
                    [auditionScope]: c.id,
                  }));
                  setListenId("");
                  setAB("B");
                }}
                onListen={(c) => {
                  setAuditionCandidateIds((v) => ({
                    ...v,
                    [auditionScope]: c.id,
                  }));
                  setListenId("");
                  setAB("B");
                }}
                onArchive={(c, value) => void archiveCandidate(c, value)}
                onUpdate={(c, changes) =>
                  update({ candidates: [{ id: c.id, ...changes }] })
                }
              />
            )}
            {mode === "expert" && activePart === "vocal" && !hasVoiceParts && (
              <details className="archive-policy">
                <summary>完整版本显式串联</summary>
                <p>以上一完整候选为输入生成下一代，保留各代音频与来源。</p>
                <button
                  className="button"
                  disabled={
                    !candidate ||
                    candidate.preview_only ||
                    !currentModel ||
                    !!busy
                  }
                  onClick={() => void renderCandidate(false, true)}
                >
                  以上一完整版本再生成一轮
                </button>
              </details>
            )}
            <details className="archive-policy">
              <summary>版本自动归档规则</summary>
              <label className="toggle-row">
                <span>成功生成后，同来源同声部保留最近两个普通版本</span>
                <input
                  type="checkbox"
                  checked={
                    project?.view_preferences?.auto_archive_candidates !== false
                  }
                  onChange={(e) =>
                    updateView({ auto_archive_candidates: e.target.checked })
                  }
                />
              </label>
              <p>收藏、恢复保留、正在采用、混音引用和比较批次受到保护。</p>
            </details>
            <Field label="A 通道对照">
              <select
                value={referenceCandidate?.id || ""}
                onChange={(e) => {
                  updateView({ ab_reference_candidate_id: e.target.value });
                  setListenId("");
                }}
              >
                <option value="">当前候选的原始输入</option>
                {candidates
                  .filter(
                    (c) =>
                      !c.preview_only &&
                      c.id !== candidate?.id &&
                      c.source_artifact_id === candidate?.source_artifact_id,
                  )
                  .map((c) => (
                    <option value={c.id} key={c.id}>
                      {blindLabels ? blindLabels[c.id] || "新版本" : c.name}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
          <aside className="inspector stack">
            <Card className="card-pad">
              {partModeControls}
              <RvcParameterPanel
                section="conversion"
                modelId={editorParameters.model_id}
                task={project?.task || "speech"}
                values={{
                  ...editorParameters,
                  range_ticks: [selection.start, selection.end],
                }}
                mode={mode}
                preferences={
                  project?.view_preferences?.expanded_panels?.[activePart] || {}
                }
                onPreferences={(changes) => {
                  const p = pRef.current;
                  updateView({
                    expanded_panels: {
                      ...p?.view_preferences?.expanded_panels,
                      [activePart]: {
                        ...p?.view_preferences?.expanded_panels?.[activePart],
                        ...changes,
                      },
                    },
                  });
                }}
                onChange={applyRvcChanges}
                onError={setError}
                busy={!!busy}
                onSchema={setRvcCatalog}
                pitchInfo={{
                  artifact_id: lastPitch?.id,
                  estimator: pitchData?.estimator,
                  locked_count: editorPitchEdits.filter((e) => e.locked).length,
                  edits_count: editorPitchEdits.length,
                  requested_f0_hash: comparisonB?.requested_f0_hash,
                  consumed_f0_hash: comparisonB?.consumed_f0_hash,
                }}
              />
            </Card>
          </aside>
        </div>
      </>
    );
  else if (step === 4) {
    let mixTracks =
      project?.tracks.filter(
        (t) =>
          !["pitch", "original", "mix"].includes(t.role) &&
          !t.preview_only &&
          !project?.candidates.some(
            (c) => c.preview_only && c.artifact_id === t.artifact_id,
          ) &&
          (Object.keys(parts).length === 0 ||
            (!["vocal", "vocals", "guide", "synthesized"].includes(t.role) &&
              !(t.role === "converted" && !t.voice_part))),
      ) || [];
    const historicalIds = new Set<string>(
      project?.candidates
        .filter((c) => !c.selected)
        .map((c) => c.artifact_id) || [],
    );
    for (const run of project?.separation_runs || [])
      if (project?.selected_separations?.[run.operation] !== run.id)
        for (const id of run.artifact_ids) historicalIds.add(id);
    for (const binding of Object.values(parts))
      if (binding?.selected_candidate_id && binding.mode === "convert")
        historicalIds.add(binding.source_artifact_id);
    if (!showHistoricalTracks)
      mixTracks = mixTracks.filter(
        (t) => !historicalIds.has(t.artifact_id) || !t.mute,
      );
    if (!mixTracks.length) mixTracks = project?.tracks.filter(t => t.role === "original") || [];
    content = (
      <div className="layout-split">
        <div className="stack">
          <Card
            title="混音台"
            icon="sliders"
            aside={<Badge tone="neutral">不叠加原始整曲</Badge>}
          >
            <label className="inline-check card-pad">
              <input
                type="checkbox"
                checked={showHistoricalTracks}
                onChange={(e) => setShowHistoricalTracks(e.target.checked)}
              />
              显示历史音轨
            </label>
            {mixTracks.length ? (
              <div className="mixer">
                {mixTracks.map((t, i) => (
                  <div
                    className={`channel ${selectedTrack === t.id ? "selected" : ""}`}
                    key={t.id}
                  >
                    <button
                      className="channel-name"
                      onClick={() => setSelectedTrack(t.id)}
                    >
                      <i
                        style={{
                          background: [
                            "#c5eb85",
                            "#8bb9d3",
                            "#b9a6db",
                            "#e6ae7f",
                          ][i % 4],
                        }}
                      />
                      {t.name || roleNames[t.role]}
                    </button>
                    <div className="fader-wrap">
                      <input
                        className="fader"
                        aria-label={`${t.name}音量`}
                        type="range"
                        min={-60}
                        max={12}
                        step={0.1}
                        value={t.gain_db || 0}
                        onChange={(e) =>
                          patchTrack(t.id, { gain_db: +e.target.value })
                        }
                      />
                      <div className="fader-scale">
                        {[12, 0, -24, -60].map((db) => (
                          <span
                            key={db}
                            style={{ top: `${((12 - db) / 72) * 100}%` }}
                          >
                            {db > 0 ? "+" : ""}
                            {db}
                          </span>
                        ))}
                      </div>
                      <div
                        className={`channel-meter ${trackMeter(t.id) >= 0 ? "clipping" : ""}`}
                        role="meter"
                        aria-label={`${t.name}效果后电平`}
                        aria-valuemin={-60}
                        aria-valuemax={0}
                        aria-valuenow={Math.max(
                          -60,
                          Math.min(0, trackMeter(t.id)),
                        )}
                      >
                        <i
                          style={{
                            height: `${Math.max(0, Math.min(100, ((trackMeter(t.id) + 60) / 60) * 100))}%`,
                          }}
                        />
                      </div>
                    </div>
                    <small className="meter-readout">
                      {trackMeter(t.id) > -120
                        ? `${trackMeter(t.id).toFixed(1)} dBFS`
                        : "效果后电平 · 停止"}
                    </small>
                    <div className="channel-level">
                      <input
                        aria-label={`${t.name}增益数值`}
                        type="number"
                        min={-60}
                        max={12}
                        step={0.1}
                        value={t.gain_db || 0}
                        onChange={(e) =>
                          patchTrack(t.id, { gain_db: +e.target.value })
                        }
                      />{" "}
                      dB
                    </div>
                    <div className="channel-buttons">
                      <button
                        className={t.mute ? "active" : ""}
                        aria-pressed={t.mute}
                        onClick={() => patchTrack(t.id, { mute: !t.mute })}
                      >
                        静音
                      </button>
                      <button
                        className={t.solo ? "active" : ""}
                        aria-pressed={t.solo}
                        onClick={() => patchTrack(t.id, { solo: !t.solo })}
                      >
                        独奏
                      </button>
                    </div>
                    <Field label="声像">
                      <input
                        aria-label={`${t.name}声像`}
                        type="range"
                        min={-1}
                        max={1}
                        step={0.05}
                        value={t.pan || 0}
                        onChange={(e) =>
                          patchTrack(t.id, { pan: +e.target.value })
                        }
                      />
                      <small>
                        {t.pan
                          ? `${t.pan < 0 ? "左" : "右"} ${Math.round(Math.abs(t.pan) * 100)}%`
                          : "居中"}
                      </small>
                    </Field>
                    <button
                      className="text-button"
                      onClick={() => {
                        setMixMonitor(t.id);
                        tell("正在生成该轨的推子后、效果后选区试听。");
                      }}
                    >
                      <Icon name="headphones" />
                      试听此轨 · 含推子与效果
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <Empty title="准备要组合的人声与伴奏">
                <p>导入分轨或完成声音转换后，轨道会出现在这里。</p>
              </Empty>
            )}
          </Card>
          <div className="highlight-strip">
            <strong role="status">
              {mixPreviewState || "进入混音后，按当前选区自动生成试听。"}
            </strong>
            <p>
              设置变化时暂停旧试听，更新完成后可播放新版本。完整成品请点击“渲染混音”。
            </p>
            {currentMixPreview &&
              currentMixPreview.measurements?.true_peak_dbfs > 0 && (
                <p className="warning-inline">
                  当前混音真峰值{" "}
                  {currentMixPreview.measurements.true_peak_dbfs.toFixed(1)}{" "}
                  dBTP，超过满幅；请降低推子后导出整数格式。
                </p>
              )}
            {selectionControls}
            {mixMonitor && (
              <button
                className="button small"
                onClick={() => setMixMonitor("")}
              >
                返回全部轨道混音试听
              </button>
            )}
            <button
              className="button small"
              disabled={!currentMixPreview}
              onClick={togglePlay}
            >
              {playing ? "暂停混音试听" : "播放当前混音选区"}
            </button>
          </div>
          <div className="row">
            <button
              className="button primary"
              disabled={!mixTracks.length || !!busy}
              onClick={() =>
                void guard(
                  () => job("mix", { sample_rate: 48000 }),
                  "提交混音任务",
                )
              }
            >
              <Icon name="sliders" />
              渲染混音
            </button>
            <button
              className="button ghost"
              onClick={() => chooseImport("instrumental")}
            >
              添加伴奏
            </button>
          </div>
          {waveformCard(mixTracks)}
        </div>
        <aside className="inspector stack">
          <Card className="card-pad">
            <div className="inspector-heading">
              <strong>轨道处理链</strong>
              <span>POST FX</span>
            </div>
            {(() => {
              const t =
                mixTracks.find((x) => x.id === selectedTrack) || mixTracks[0];
              if (!t)
                return <p className="info-inline">选择一个轨道后编辑效果。</p>;
              const defs = [
                { type: "highpass", name: "高通滤波", frequency: 80 },
                { type: "lowpass", name: "低通滤波", frequency: 16000 },
                {
                  type: "compressor",
                  name: "压缩器",
                  threshold_db: -18,
                  ratio: 3,
                },
                {
                  type: "reverb",
                  name: "房间混响",
                  wet: 0.15,
                  room_seconds: 0.65,
                  predelay_ms: 18,
                },
                { type: "delay", name: "延迟", delay_ms: 120, decay: 0.2 },
                { type: "limiter", name: "限制器", limit_db: -1 },
              ];
              return (
                <>
                  <strong className="track-heading">{t.name}</strong>
                  <Field label="对齐偏移（秒）">
                    <input
                      type="number"
                      step={0.001}
                      value={seconds(t.offset_ticks || 0)}
                      onChange={(e) =>
                        patchTrack(t.id, {
                          offset_ticks: ticks(+e.target.value),
                        })
                      }
                    />
                  </Field>
                  {defs.map((d) => {
                    const fx = t.effects?.find((f) => f.type === d.type);
                    const effect = { ...d, ...fx };
                    return (
                      <div className="effect-row" key={d.type}>
                        <label className="toggle-row">
                          <span>{d.name}</span>
                          <input
                            type="checkbox"
                            checked={!!fx?.enabled}
                            onChange={(e) =>
                              patchTrack(t.id, {
                                effects: [
                                  ...(t.effects || []).filter(
                                    (f) => f.type !== d.type,
                                  ),
                                  { ...effect, enabled: e.target.checked },
                                ],
                              })
                            }
                          />
                        </label>
                        {fx?.enabled &&
                          Object.entries(d)
                            .filter(([k]) => !["type", "name"].includes(k))
                            .map(([k, v]) => (
                              <Field
                                label={
                                  (
                                    {
                                      frequency: "截止频率 Hz",
                                      wet: "混响湿声比例",
                                      room_seconds: "混响长度（秒）",
                                      predelay_ms: "预延迟 ms",
                                      threshold_db: "阈值 dB",
                                      ratio: "压缩比",
                                      delay_ms: "延迟 ms",
                                      decay: "反馈比例",
                                      limit_db: "限制 dB",
                                    } as Bag
                                  )[k]
                                }
                                key={k}
                              >
                                <input
                                  type="number"
                                  step={
                                    ["decay", "wet", "room_seconds"].includes(k)
                                      ? 0.01
                                      : 1
                                  }
                                  value={effect[k as keyof typeof effect] ?? v}
                                  onChange={(e) =>
                                    patchTrack(t.id, {
                                      effects: t.effects.map((f) =>
                                        f.type === d.type
                                          ? { ...f, [k]: +e.target.value }
                                          : f,
                                      ),
                                    })
                                  }
                                />
                              </Field>
                            ))}
                      </div>
                    );
                  })}
                  <p className="info-inline">
                    处理顺序以启用顺序保存。修改后会自动更新选区试听；底部标明当前是全部混音还是单轨效果后监听。
                  </p>
                </>
              );
            })()}
          </Card>
        </aside>
      </div>
    );
  } else
    content = (
      <div className="layout-split">
        <div className="stack">
          <Card
            title="成品导出"
            icon="export"
            aside={<Badge>原子写入 · 文件冲突自动编号</Badge>}
          >
            <div className="card-pad">
              <Field label="输出产物">
                <select
                  value={exportId}
                  onChange={(e) => setExportId(e.target.value)}
                >
                  <option value="">当前最终候选 / 最新混音</option>
                  {project?.assets.filter(isAudio).map((a) => (
                    <option value={a.id} key={a.id}>
                      {artifactLabel(a)}
                    </option>
                  ))}
                </select>
              </Field>
              <div className="export-grid">
                {[
                  ["wav", "WAV", "无损 PCM · 编辑与母带"],
                  ["flac", "FLAC", "无损压缩 · 保存与共享"],
                  ["mp3", "MP3", "压缩音频 · 方便试听"],
                ].map(([id, title, sub]) => (
                  <button
                    className={`export-card ${format === id ? "active" : ""}`}
                    key={id}
                    onClick={() => setFormat(id)}
                  >
                    <Icon name="export" />
                    <strong>{title}</strong>
                    <small>{sub}</small>
                  </button>
                ))}
              </div>
              {format === "wav" && (
                <Field
                  label="WAV 位深与采样格式"
                  hint="16/24位整数需要峰值留量；32位浮点保留超满幅数据，播放仍需降低监听音量。"
                >
                  <select
                    value={wavSubtype}
                    onChange={(e) => setWavSubtype(e.target.value)}
                  >
                    <option value="pcm_s16le">16-bit PCM</option>
                    <option value="pcm_s24le">24-bit PCM</option>
                    <option value="pcm_f32le">32-bit Float</option>
                  </select>
                </Field>
              )}
              <Field label="文件名（不含扩展名）">
                <input
                  value={exportName}
                  onChange={(e) => setExportName(e.target.value)}
                  placeholder={project?.name || "声序作品"}
                  maxLength={120}
                />
              </Field>
              <label className="toggle-row">
                <span>仅导出当前选区</span>
                <input
                  type="checkbox"
                  checked={exportSelection}
                  onChange={(e) => setExportSelection(e.target.checked)}
                />
              </label>
              {exportSelection && selectionControls}
              <div className="row wrap space-top">
                <button
                  className="button primary"
                  disabled={!source || !!busy}
                  onClick={() =>
                    void guard(async () => {
                      const a = exportSource;
                      await job(
                        "export",
                        {
                          artifact_id: a?.id,
                          format,
                          wav_subtype: wavSubtype,
                          name: exportName || project?.name,
                          selection_only: exportSelection,
                          model_converted:
                            a?.processing_kind === "model_inference" ||
                            !!a?.model_inference_lineage,
                        },
                        a?.id,
                      );
                    }, "提交导出任务")
                  }
                >
                  <Icon name="export" />
                  导出所选音频
                </button>
                <button
                  className="button ghost"
                  disabled={!project}
                  onClick={() =>
                    void guard(async () => {
                      await flush();
                      const r = await request(
                        `/projects/${project!.id}/recipe`,
                      );
                      jsonDownload(r, `${project!.name}.sonora.json`);
                      tell("配方已导出，不包含模型权重。");
                    })
                  }
                >
                  <Icon name="save" />
                  保存工程配方
                </button>
              </div>
              <p className="info-inline">
                未经过模型推理的文件只会作为原音频处理版导出。产物血缘与实际处理类型保存在导出清单中。
              </p>
            </div>
          </Card>
          <Card
            title="逐轨 stems 导出"
            icon="layers"
            aside={<Badge tone="neutral">共同起点 · 相同长度</Badge>}
          >
            <div className="card-pad">
              <Field label="分轨处理语义">
                <select
                  value={stemSemantics}
                  onChange={(e) => setStemSemantics(e.target.value)}
                >
                  <option value="raw">原始分轨 · 推子与效果前</option>
                  <option value="post_fader_fx">
                    处理后分轨 · 推子与轨道效果后
                  </option>
                </select>
              </Field>
              <p className="info-inline">
                导出有效分轨，统一起点、长度与采样率。最终母线经过非线性处理时，分轨相加不保证逐采样等同最终混音。
              </p>
              <button
                className="button"
                disabled={!project?.tracks.length || !!busy}
                onClick={() =>
                  void guard(
                    () =>
                      job("export_stems", {
                        semantics: stemSemantics,
                        wav_subtype: wavSubtype,
                        format: format === "mp3" ? "wav" : format,
                      }),
                    "提交分轨导出",
                  )
                }
              >
                <Icon name="export" />
                导出 {format === "mp3" ? "WAV" : format.toUpperCase()} 分轨
              </button>
            </div>
          </Card>
          <Card title="已导出的版本" icon="save">
            <div className="export-list">
              {project?.exports.map((e) => (
                <div className="export-row" key={e.id}>
                  <div>
                    <strong>
                      {e.name ||
                        e.filename ||
                        e.path?.split(/[\\/]/).pop() ||
                        e.id}
                    </strong>
                    <small>
                      {e.format?.toUpperCase()} · {e.codec} ·{" "}
                      {e.processing_kind
                        ? kindNames[e.processing_kind]
                        : e.created_at}{" "}
                      · {e.sample_count ? `${e.sample_count} samples` : ""}
                    </small>
                  </div>
                  <button
                    className="button small"
                    onClick={() =>
                      void guard(() =>
                        download(
                          `/exports/${e.id}/download`,
                          e.name || e.filename || `SONORA.${e.format || "wav"}`,
                        ),
                      )
                    }
                  >
                    下载
                  </button>
                </div>
              ))}
              {!project?.exports.length && (
                <Empty title="还没有导出文件">
                  <p>导出成功后，版本与下载入口会保留在这里。</p>
                </Empty>
              )}
            </div>
          </Card>
        </div>
        <aside className="inspector stack">
          <Card className="card-pad">
            <div className="inspector-heading">
              <strong>技术检查</strong>
              <span>MEASURED</span>
            </div>
            {(() => {
              const a = exportSource;
              const d = a?.diagnostics;
              const measuredExport = project?.exports
                .slice()
                .reverse()
                .find(
                  (e) =>
                    e.artifact_id === a?.id &&
                    e.measurements?.measurement_succeeded,
                );
              const measured =
                exportMeasurements || measuredExport?.measurements;
              return (
                <>
                  <div className="parameter-row">
                    <span>处理类型</span>
                    <strong>
                      {a ? kindNames[a.processing_kind] : "待选择"}
                    </strong>
                  </div>
                  <div className="parameter-row">
                    <span>时长</span>
                    <strong>{clock(duration(a))}</strong>
                  </div>
                  <div className="parameter-row">
                    <span>采样率</span>
                    <strong>{a?.sample_rate || "—"} Hz</strong>
                  </div>
                  <div className="parameter-row">
                    <span>采样峰值</span>
                    <strong>
                      {d?.sample_peak_dbfs != null
                        ? `${Number(d.sample_peak_dbfs).toFixed(2)} dBFS`
                        : "未测量"}
                    </strong>
                  </div>
                  <div className="parameter-row">
                    <span>
                      {exportSelection ? "选区综合响度" : "当前源综合响度"}
                    </span>
                    <strong>
                      {measured?.integrated_lufs != null
                        ? `${Number(measured.integrated_lufs).toFixed(1)} LUFS`
                        : "未测量"}
                    </strong>
                  </div>
                  <div className="parameter-row">
                    <span>
                      {exportSelection ? "选区真峰值" : "当前源真峰值"}
                    </span>
                    <strong>
                      {measured?.true_peak_dbfs != null
                        ? `${Number(measured.true_peak_dbfs).toFixed(2)} dBTP`
                        : "未测量"}
                    </strong>
                  </div>
                  {exportMeasurements && (
                    <p className="info-inline">
                      已测量 {exportMeasurements.duration.toFixed(2)} 秒 ·{" "}
                      {exportMeasurements.measurement_engine}
                    </p>
                  )}
                  {!exportMeasurements && (
                    <p className="info-inline">
                      正在测量所选音频的真实峰值与响度…
                    </p>
                  )}
                  {measuredExport && (
                    <div className="helper-box">
                      <strong>最近导出测量</strong>
                      <p>
                        {measuredExport.name}
                        <br />
                        {clock(measuredExport.duration || 0)} ·{" "}
                        {measured.measurement_engine}
                      </p>
                    </div>
                  )}
                  <div className="line" />
                  <p className="info-inline">
                    采样峰值与真峰值是不同测量。没有测量结果的项目不表示已经通过。
                  </p>
                </>
              );
            })()}
          </Card>
        </aside>
      </div>
    );

  return (
    <div className={`app-shell density-${mode} ui-${uiTheme}`}>
      <aside className="sidebar">
        <button className="brand" onClick={() => setStep(0)}>
          <span className="brand-mark">
            <Icon name="wave" />
          </span>
          <span>
            SONORA<small>声 序 · 音 频 工 作 台</small>
          </span>
        </button>
        <button className="project-picker" onClick={() => void guard(async()=>{await flush(); const result=await request<{projects:Project[]}>("/projects");setProjects(result.projects);setModal("projects");})}>
          <span className="project-dot" />
          <span>
            <strong>{project?.name || "创建你的第一个工程"}</strong>
            <small>本地工程 · {saveState}</small>
          </span>
          <span className="chevron">⌄</span>
        </button>
        <div className="nav-label">
          制作流程 <span>06</span>
        </div>
        <nav aria-label="制作步骤">
          {steps.map(([icon, title], i) => {
            const stage = workflow?.project_id === project?.id ? workflow?.stages?.find((item: Bag) => item.id === i) : undefined;
            const stale = ["stale", "preview_stale"].includes(stage?.state);
            const done = !!stage?.completed;
            return (
              <button
                className={`nav-step ${step === i ? "active" : ""} ${done ? "done" : ""}`}
                onClick={() => setStep(i)}
                key={title}
                disabled={!project || connection !== "ready"}
                aria-current={step === i ? "step" : undefined}
              >
                <Icon name={icon} />
                <span>{title}</span>
                <span
                  className="step-number"
                  title={
                    stale
                      ? "已失效：新版本待渲染"
                      : done
                        ? "已有已完成产物"
                        : "可编辑"
                  }
                >
                  {stale ? "!" : done ? "✓" : String(i + 1).padStart(2, "0")}
                </span>
              </button>
            );
          })}
        </nav>
        <div className="nav-divider" />
        <button
          className="nav-secondary"
          onClick={() => openModelManager(activePart)}
        >
          <Icon name="box" />
          声音与模型<span className="side-count">{models.length}</span>
        </button>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className={connection === "ready" ? "" : "offline"} />
            {connection === "ready" ? "文件在本机处理" : "本地服务未连接"}
          </div>

          <button className="text-button" onClick={() => setModal("settings")}>
            <Icon name="settings" />
            引擎与设置
          </button>
        </div>
      </aside>
      <div className="workspace has-workflow">
        <header className="topbar">
          <div className="breadcrumb">
            <span>我的工作室</span>
            <span>/</span>
            <strong>{steps[step][1]}</strong>
          </div>
          <div className="top-actions">
            <div className="segmented ui-switch" aria-label="界面风格">
              <button
                className={uiTheme === "classic" ? "active" : ""}
                onClick={() => setUiTheme("classic")}
              >
                经典 UI
              </button>
              <button
                className={uiTheme === "soft" ? "active" : ""}
                onClick={() => setUiTheme("soft")}
              >
                柔和 UI
              </button>
            </div>
            <span
              className={`save-indicator ${saveState === "保存失败" ? "error-text" : ""}`}
              title={`工程修订 ${project?.revision || 0}`}
            >
              {saveState === "已保存" ? "✓ " : ""}
              {saveState}
            </span>
            <button
              className="icon-button"
              disabled={!project}
              title="撤销 Ctrl+Z"
              aria-label="撤销"
              onClick={() => void historyAction("undo")}
            >
              <Icon name="undo" />
            </button>
            <button
              className="icon-button"
              disabled={!project}
              title="重做 Ctrl+Shift+Z"
              aria-label="重做"
              onClick={() => void historyAction("redo")}
            >
              <Icon name="redo" />
            </button>
            <button className="quiet-button" onClick={() => setModal("queue")}>
              <Icon name="clock" />
              <span>队列 {activeCount || ""}</span>
            </button>
            <div
              className="segmented mode-switch"
              role="group"
              aria-label="编辑信息密度"
            >
              {[
                ["quick", "快速完成"],
                ["expert", "专业模式"],
              ].map(([id, title]) => (
                <button
                  className={mode === id ? "active" : ""}
                  key={id}
                  disabled={!project}
                  onClick={() => update({ mode: id })}
                >
                  {title}
                </button>
              ))}
            </div>
          </div>
        </header>
        <main>
          {connection !== "ready" && (
            <div className="connection-banner">
              <div>
                <strong>
                  {connection === "connecting"
                    ? "正在连接本机服务…"
                    : connection === "unauthorized"
                      ? "需要本地会话凭据"
                      : "未连接本地编排服务"}
                </strong>
                <p>使用项目启动器启动桌面程序，或在设置中填写本次会话令牌。</p>
              </div>
              <button
                className="button small"
                onClick={() => setModal("settings")}
              >
                连接设置
              </button>
              <button
                className="text-button"
                onClick={() => void guard(refresh)}
              >
                重新连接
              </button>
            </div>
          )}
          {error && (
            <div className="error-banner" role="alert">
              <Icon name="info" />
              <span>{error}</span>
              <button
                className="icon-button"
                aria-label="关闭错误提示"
                onClick={() => setError("")}
              >
                <Icon name="close" />
              </button>
            </div>
          )}
          {busy && (
            <div className="busy-banner" role="status">
              <span className="spinner" />
              {busy}…
            </div>
          )}
          {(project?.warnings || [])
            .filter((w) => ["missing_media", "missing_model"].includes(w.code))
            .map((w, i) => (
              <div className="connection-banner" key={i}>
                <div>
                  <strong>{w.message}</strong>
                  {w.hash && <p>素材哈希 {w.hash}</p>}
                </div>
                <button
                  className="button small"
                  onClick={() => {
                    if (w.code === "missing_media") {
                      setRelinkHash(w.hash);
                      setPathDraft("");
                      setModal("relink");
                    } else openModelManager(activePart);
                  }}
                >
                  {w.code === "missing_media" ? "重新链接素材" : "导入匹配模型"}
                </button>
              </div>
            ))}
          <div className="page-head">
            <div>
              <div className="eyebrow">
                STEP {String(step + 1).padStart(2, "0")} /{" "}
                {
                  [
                    "IMPORT & DIAGNOSE",
                    "PREPARE YOUR VOICE",
                    "PITCH & LYRICS",
                    "VOICE CONVERSION",
                    "MIX & FINISH",
                    "EXPORT YOUR WORK",
                  ][step]
                }
              </div>
              <h1>{steps[step][1]}</h1>
            </div>
            <div className="page-head-right">
              <Badge tone={connection === "ready" ? "" : "orange"}>
                {connection === "ready" ? "本地服务已连接" : "等待本地连接"}
              </Badge>
              {mode === "quick" && dirty > 0 && (
                <button
                  className="quiet-button"
                  onClick={() => update({ mode: "expert" })}
                >
                  配方含 {dirty} 项高级调整
                </button>
              )}
            </div>
          </div>
          <WorkflowStrip
            projectId={project?.id}
            revision={project?.revision || 0}
            stage={step}
            voicePart={activePart}
            catalogRevision={catalogRevision}
            activityKey={jobs
              .map((j) => `${j.id}:${j.state}:${j.stage}`)
              .join("|")}
            disabled={!!busy || saveState !== "已保存"}
            onAction={workflowAction}
            onLoaded={setWorkflow}
          />
          {currentStage?.input?.stale && (
            <div className="pending-strip">
              当前输入已受上游修改影响，请按推荐动作更新。
            </div>
          )}
          {step >= 2 &&
            ["stale", "preview_stale"].includes(currentStage?.state) &&
            project?.warnings.some((w) => w.code === "pending_render") && (
              <div className="pending-strip">
                新版本待试听 · 已完成版本继续保留，请渲染受影响的节点。
              </div>
            )}
          {content}
          <div className="page-bottom">
            <span className="bottom-note">
              <Icon name="save" />
              编辑自动保存 · 原素材保持不变
            </span>
            <div className="bottom-actions">
              {step > 0 && (
                <button
                  className="button ghost"
                  onClick={() => setStep(step - 1)}
                >
                  上一步
                </button>
              )}
              {step < 5 && (
                <button
                  className="button primary"
                  disabled={
                    !currentStage?.continuation ||
                    currentStage.continuation.disabled ||
                    !!busy ||
                    saveState !== "已保存"
                  }
                  title={
                    saveState !== "已保存"
                      ? saveState
                      : currentStage?.continuation?.reason ||
                        "只切换页面，不执行任务"
                  }
                  onClick={() => workflowAction(currentStage!.continuation)}
                >
                  {currentStage?.continuation?.label || "继续"}
                  <Icon name="arrow" />
                </button>
              )}
              {currentStage?.continuation?.disabled && (
                <small role="status">{currentStage.continuation.reason}</small>
              )}
            </div>
          </div>
        </main>
        <footer className="transport" aria-label="音频播放控制">
          <div className="transport-source">
            <span className="mini-cover">
              <Icon name="wave" />
            </span>
            <div>
              <strong>
                {listen?.name ||
                  (step === 4
                    ? "当前混音试听正在更新"
                    : "暂无当前阶段的试听来源")}
              </strong>
              <small>
                {listen
                  ? `${steps[step][1]} · ${step === 4 ? (mixMonitor ? "单轨 · 推子/效果后" : "当前混音 · 推子/效果后") : step === 5 ? "当前导出源" : step === 3 ? `${ab} 通道 · ` + kindNames[listen.processing_kind] : kindNames[listen.processing_kind]}`
                  : step === 4
                    ? "等待当前设置渲染完成"
                    : `${steps[step][1]} · 请准备本阶段输入`}
                {listenId && step !== 4 && step !== 5 ? " · 单轨原始产物" : ""}
              </small>
            </div>
          </div>
          <div className="transport-center">
            <div className="player-buttons">
              <button
                className={`icon-button ${loop ? "on" : ""}`}
                aria-label="循环选区"
                aria-pressed={loop}
                disabled={!loopValid}
                title={
                  loopValid
                    ? "循环当前选区与试听源的交集"
                    : "选区与试听音频需要至少50毫秒交集"
                }
                onClick={() => setLoop(!loop)}
              >
                <Icon name="repeat" />
              </button>
              <button
                className="play-button"
                aria-label={playing ? "暂停音频" : "播放音频"}
                disabled={!listen}
                onClick={togglePlay}
              >
                <Icon name={playing ? "pause" : "play"} />
              </button>
              <button
                className="icon-button"
                aria-label="回到选区起点"
                onClick={() => seek(seconds(selection.start))}
              >
                ↤
              </button>
              <span className="timecode">
                {clock(playhead)} <em>/ {clock(maxDuration)}</em>
              </span>
            </div>
            <input
              type="range"
              min={0}
              max={maxDuration || 1}
              step={0.01}
              value={Math.min(playhead, maxDuration || 1)}
              aria-label="播放位置"
              onChange={(e) => seek(+e.target.value)}
              onPointerUp={savePlayhead}
              onKeyUp={savePlayhead}
            />
          </div>
          <div className="transport-right">
            <div className="segmented ab-switch">
              <button
                className={ab === "A" && !listenId ? "active" : ""}
                disabled={step !== 3 || !comparisonA}
                onClick={() => selectAB("A")}
              >
                {referenceCandidate ? "候选 A" : "原始 A"}
              </button>
              <button
                className={ab === "B" && !listenId ? "active" : ""}
                disabled={step !== 3 || !comparisonB}
                onClick={() => selectAB("B")}
              >
                候选 B
              </button>
            </div>
            <label
              className="inline-check match"
              title={
                matchAvailable
                  ? `按 RMS 匹配：${(20 * Math.log10(matchGain)).toFixed(1)} dB`
                  : "声音转换页对同一选区测量RMS后启用；测量期间不匹配响度"
              }
            >
              <input
                type="checkbox"
                checked={matched && matchAvailable}
                disabled={!matchAvailable}
                onChange={(e) => setMatched(e.target.checked)}
              />
              等响
            </label>
            <label className="volume">
              <Icon name="headphones" />
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={volume}
                aria-label="监听音量"
                onChange={(e) => setVolume(+e.target.value)}
              />
            </label>
            <span
              className="monitor-meter"
              title="实际监听输出峰值，受监听音量影响"
            >
              {playing ? `${meterDb.toFixed(0)} dBFS` : "监听停止"}
            </span>
          </div>
        </footer>
        <audio
          ref={audio}
          onPlay={() => {
            if (audio.current?.dataset.projectId === pRef.current?.id)
              setPlaying(true);
          }}
          onPause={() => {
            setPlaying(false);
            if (audio.current?.dataset.projectId === pRef.current?.id)
              savePlayhead();
          }}
          onEnded={() => {
            if (audio.current?.dataset.projectId !== pRef.current?.id) return;
            if (!wrapLoop()) setPlaying(false);
          }}
          onTimeUpdate={() => {
            const a = audio.current!;
            if (a.dataset.projectId !== pRef.current?.id) return;
            if (loop && loopValid && a.currentTime + listenOffset >= loopEnd)
              wrapLoop();
            setPlayhead(a.currentTime + listenOffset);
            playRef.current = a.currentTime + listenOffset;
          }}
        />
      </div>
      <input
        hidden
        type="file"
        ref={upload}
        accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.webm"
        onChange={handleFile}
      />
      <input
        hidden
        type="file"
        ref={recipeInput}
        accept="application/json,.json"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f)
            void guard(async () => {
              await flush();
              const body = new FormData();
              body.append("file", f);
              const p = await request<Project>("/projects/restore", {
                method: "POST",
                body,
              });
              applyProject(p);
              setModal("");
              tell("工程配方已恢复，请检查缺失素材或模型提示。");
            }, "正在恢复工程");
        }}
      />
      <input
        hidden
        type="file"
        ref={lyricInput}
        accept=".lrc,.txt"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f)
            void guard(async () => {
              const text = await f.text();
              if (text.length > 200000) throw new Error("歌词文件过大。");
              const lines = text.split(/\r?\n/).filter((l) => l.trim());
              const parsed = lines.map((line, i) => {
                const match = line.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)$/);
                return {
                  id: uid(),
                  start: match
                    ? ticks(+match[1] * 60 + +match[2])
                    : Math.round((i / lines.length) * ticks(maxDuration)),
                  text: match ? match[3] : line,
                  original_text: "",
                  language: "zh",
                  pronunciation: "",
                  timing_policy: "locked_window",
                  minimum_duration: 0.08,
                };
              });
              update({
                lyrics: parsed.map((l, i) => ({
                  ...l,
                  end: parsed[i + 1]?.start || ticks(maxDuration),
                })),
              });
              tell("歌词已导入；请人工检查句界与发音。");
            });
        }}
      />
      <input
        hidden
        type="file"
        ref={midiInput}
        accept=".mid,.midi"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f)
            void guard(async () => {
              await flush();
              const p = await ensureProject();
              const body = new FormData();
              body.append("file", f);
              body.append("revision", String(pRef.current!.revision));
              const result = await request<Project>(`/projects/${p.id}/midi`, {
                method: "POST",
                body,
              });
              applyProject(result);
              tell("MIDI 音符已导入，请检查音节、连音与时值。");
            }, "导入 MIDI");
        }}
      />
      {!!notices.length && (
        <div
          className="toast show"
          role="status"
          key={notices[0].id}
          onMouseEnter={() => setToastPaused(true)}
          onMouseLeave={() => setToastPaused(false)}
        >
          <span>{notices[0].text}</span>
          {notices.length > 1 && <small>另有 {notices.length - 1} 条</small>}
          <button
            className="icon-button"
            aria-label="关闭当前提示"
            onClick={() => {
              setNotices((items) => items.slice(1));
              setToastPaused(false);
            }}
          >
            ×
          </button>
        </div>
      )}
      {modal && (
        <Modal
          title={
            (
              {
                projects: "项目库",
                models: "声音与模型",
                queue: "本机任务队列",
                settings: "引擎与设置",
                "import-path": "从本机路径导入",
                relink: "重新链接缺失素材",
              } as Bag
            )[modal]
          }
          onClose={() => setModal("")}
        >
          {modal === "projects" ? (
            <>
              <div className="field-grid">
                <Field label="新工程名称">
                  <input
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    maxLength={120}
                  />
                </Field>
                <button
                  className="button primary align-end"
                  disabled={connection !== "ready"}
                  onClick={() =>
                    void guard(async () => {
                      await flush();
                      const p = await post<Project>("/projects", {
                        name: newName || "新的声音作品",
                        task: "cover",
                      });
                      applyProject(p);
                      setListenId("");
                      seek(0);
                      setModal("");
                      setStep(0);
                    })
                  }
                >
                  创建工程
                </button>
              </div>
              {project && (
                <Field label="当前项目名称">
                  <input
                    value={project.name}
                    maxLength={120}
                    onChange={(e) => update({ name: e.target.value })}
                  />
                </Field>
              )}
              <input
                className="project-search"
                aria-label="搜索项目"
                value={projectSearch}
                onChange={(e) => setProjectSearch(e.target.value)}
                placeholder="搜索项目名称"
              />
              <div className="project-list">
                {projects
                  .filter((p) =>
                    p.name.toLowerCase().includes(projectSearch.toLowerCase()),
                  )
                  .map((p) => (
                    <button
                      key={p.id}
                      className={`project-row ${p.id === project?.id ? "current" : ""}`}
                      onClick={() =>
                        void guard(async () => {
                          await flush();
                          audio.current?.pause();
                          applyProject(
                          await request<Project>(`/projects/${p.id}`),
                        );
                          setListenId("");
                          setModal("");
                        })
                      }
                    >
                      <Icon name="wave" />
                      <span>
                        <strong>{p.name}</strong>
                        <small>
                          {p.tracks.length} 条音轨 ·{" "}
                          {p.candidates.filter((c) => !c.preview_only).length}{" "}
                          个完整版本 · {p.exports.length} 次导出 ·{" "}
                          {steps[p.view_preferences?.stage || 0][1]}
                        </small>
                      </span>
                      <Icon name="arrow" />
                    </button>
                  ))}
              </div>
              <button
                className="button ghost full"
                onClick={() => recipeInput.current?.click()}
              >
                打开工程配方 JSON
              </button>
            </>
          ) : modal === "models" ? (
            <CatalogManager
              onChanged={() => {
                setCatalogRevision((n) => n + 1);
                void refresh().catch(() => {});
              }}
              onError={setError}
              onSelect={async (m) => {
                const ix = await request<Bag>(
                  `/catalog/models/${encodeURIComponent(m.id)}/indices`,
                );
                await selectScopedModel(managerScope, m, ix);
                setModal("");
              }}
            />
          ) : modal === "queue" ? (
            <>
              <p className="info-inline">
                重计算任务在独立进程中运行。取消保留已提交成果，恢复会重新检查输入与缓存。
              </p>
              <div className="job-list">
                {jobs.map((j) => (
                  <div className="job" key={j.id}>
                    <span
                      className={`job-status ${activeJob(j) ? "running" : ""}`}
                    >
                      {j.state === "succeeded"
                        ? "✓"
                        : j.state === "failed"
                          ? "!"
                          : "·"}
                    </span>
                    <div className="job-text">
                      <strong>
                        {j.parameters?.voice_part && (
                          <span>
                            {roleNames[j.parameters.voice_part] ||
                              j.parameters.voice_part}{" "}
                            ·{" "}
                          </span>
                        )}
                        {(
                          {
                            separate: "人声与伴奏分离",
                            separate_harmony: "主唱与和声分离",
                            convert_parts: "两声部独立转换与混音",
                            pitch: "音高分析",
                            convert: "声音转换",
                            compare_versions: "多 Epoch 同源对比",
                            adopt_preview: "局部采用试听版本",
                            mix: "混音渲染",
                            export: "音频导出",
                            pipeline: "完整制作流程",
                            comp: "候选片段拼选",
                            export_stems: "逐轨分轨导出",
                            svs: "日语歌声合成",
                          } as Bag
                        )[j.operation] || j.operation}
                      </strong>
                      <small>
                        {stateLabel[j.state] || j.state} ·{" "}
                        {j.stage || j.message || "等待执行器"}
                      </small>
                      {j.progress != null && (
                        <progress
                          value={j.progress > 1 ? j.progress / 100 : j.progress}
                          max={1}
                        />
                      )}{" "}
                      {j.error && (
                        <div className="job-error">
                          {j.error.message}
                          <small>
                            {stalePitchJob(j)
                              ? "点击右侧“重新检测并重试”，解除该任务的旧曲线绑定并按任务参数重新分析；保留手工编辑和已有音频。"
                              : j.error.recovery}
                          </small>
                        </div>
                      )}
                    </div>
                    {activeJob(j) ? (
                      <button
                        className="button small"
                        onClick={() =>
                          void guard(async () => {
                            await post(`/jobs/${j.id}/cancel`);
                            tell("已请求取消。");
                          })
                        }
                      >
                        取消
                      </button>
                    ) : (
                      ["failed", "blocked", "cancelled"].includes(j.state) && (
                        <button
                          className="button small"
                          onClick={() =>
                            void guard(async () => {
                              await post(
                                `/jobs/${j.id}/retry`,
                                stalePitchJob(j)
                                  ? { reset_pitch_binding: true }
                                  : {},
                              );
                              tell("已加入重试队列。");
                            })
                          }
                        >
                          {stalePitchJob(j) ? "重新检测并重试" : "重试"}
                        </button>
                      )
                    )}
                  </div>
                ))}
                {!jobs.length && (
                  <Empty title="队列安静，创作继续" icon="clock">
                    <p>创建分析、转换或导出任务后，在这里查看实际状态。</p>
                  </Empty>
                )}
              </div>
            </>
          ) : modal === "relink" ? (
            <>
              <p className="info-inline">
                只接受哈希与配方匹配的原素材或缓存产物；同名文件不会被自动替换。
              </p>
              <Field label="匹配素材的本机路径">
                <input
                  value={pathDraft}
                  onChange={(e) => setPathDraft(e.target.value)}
                />
              </Field>
              <button
                className="button primary full"
                disabled={!pathDraft || !!busy}
                onClick={() =>
                  void guard(async () => {
                    await flush();
                    const r = await post<Bag>(
                      `/projects/${project!.id}/relink`,
                      { hash: relinkHash, path: pathDraft },
                    );
                    applyProject(r.project);
                    setModal("");
                    tell("素材已按哈希重新链接。");
                  }, "校验并重新链接")
                }
              >
                校验哈希并链接
              </button>
            </>
          ) : modal === "import-path" ? (
            <>
              <Field
                label="音频文件绝对路径"
                hint="路径必须属于本机服务已授权的资源范围。"
              >
                <input
                  value={pathDraft}
                  onChange={(e) => setPathDraft(e.target.value)}
                  placeholder="E:\voice\…\song.wav"
                />
              </Field>
              <Field label="素材角色">
                <select
                  defaultValue={roleRef.current}
                  onChange={(e) => {
                    roleRef.current = e.target.value;
                  }}
                >
                  <option value="original">原始素材</option>
                  <option value="vocal">干人声</option>
                  <option value="instrumental">伴奏</option>
                  <option value="guide">新词导唱</option>
                  <option value="lead_vocal">主唱干声分轨</option>
                  <option value="backing_vocal">和声干声分轨</option>
                </select>
              </Field>
              <button
                className="button primary full"
                disabled={!pathDraft || !!busy}
                onClick={() =>
                  void guard(async () => {
                    await flush();
                    const p = await ensureProject();
                    const r = await post<Bag>(`/projects/${p.id}/import-path`, {
                      path: pathDraft,
                      role: roleRef.current,
                    });
                    applyProject(r.project);
                    setListenId(r.artifact.id);
                    setModal("");
                    setPathDraft("");
                    tell("素材已导入。");
                  }, "导入本机素材")
                }
              >
                导入与解码
              </button>
            </>
          ) : (
            <>
              <div className="setting-section">
                <h3>本机服务连接</h3>
                <p className="info-inline">
                  明确配置的本机端点 · 协议
                  v1。仅使用启动器提供的当前会话令牌，不扫描端口。
                </p>
                <Field label="本机服务端点">
                  <input
                    value={endpointDraft}
                    onChange={(e) => setEndpointDraft(e.target.value)}
                    placeholder="http://127.0.0.1:8765"
                  />
                </Field>
                <Field label="会话令牌">
                  <input
                    type="password"
                    value={tokenDraft}
                    autoComplete="off"
                    onChange={(e) => setTokenDraft(e.target.value)}
                    placeholder="粘贴当前会话令牌"
                  />
                </Field>
                <button
                  className="button primary"
                  onClick={() =>
                    void guard(async () => {
                      const changingEndpoint =
                        endpointDraft.replace(/\/$/, "") !==
                        getConnectionEndpoint();
                      if (changingEndpoint && !tokenDraft)
                        throw new Error(
                          "切换服务端点时，请同时提供该服务的当前会话令牌。",
                        );
                      if (changingEndpoint) {
                        await flush();
                        audio.current?.pause();
                        pRef.current = null;
                        setProject(null);
                        setJobs([]);
                        setListenId("");
                        eventCursor.current = 0;
                        jobSignature.current = "";
                      }
                      if (tokenDraft) setConnection(tokenDraft, endpointDraft);
                      await refresh();
                      setTokenDraft("");
                      setError("");
                      tell("已连接本机编排服务。");
                    })
                  }
                >
                  连接 / 重新探测
                </button>
              </div>
              <div className="setting-section">
                <h3>真实引擎能力</h3>
                {engines.map((e) => (
                  <div className="engine-row" key={e.id}>
                    <div>
                      <strong>{e.name || e.id}</strong>
                      <small>
                        {e.reason ||
                          Object.keys(e.capabilities || {})
                            .filter((k) => e.capabilities[k] === true)
                            .join(" · ") ||
                          "以适配器能力清单为准"}
                      </small>
                    </div>
                    <Badge
                      tone={
                        ["ready", "available", "installed"].includes(e.status)
                          ? ""
                          : "orange"
                      }
                    >
                      {stateLabel[e.status] || e.status}
                    </Badge>
                  </div>
                ))}
                <p className="info-inline">
                  {device.name || device.gpu || device.platform || ""}{" "}
                  {device.cpu || ""}
                </p>
              </div>
              <div className="setting-section">
                <h3>界面与快捷键</h3>
                <label className="toggle-row">
                  <span>放大界面文字</span>
                  <input
                    type="checkbox"
                    onChange={(e) =>
                      document.documentElement.classList.toggle(
                        "large-text",
                        e.target.checked,
                      )
                    }
                  />
                </label>
                <p className="info-inline">
                  <kbd>空格</kbd> 播放暂停 · <kbd>I</kbd> / <kbd>O</kbd>{" "}
                  选区入出点 · <kbd>A</kbd> / <kbd>B</kbd> 对照 ·{" "}
                  <kbd>Ctrl Z</kbd> 撤销 · <kbd>Ctrl Shift Z</kbd>{" "}
                  重做。文本输入时不触发编辑快捷键。
                </p>
                <div className="helper-box">
                  <strong>实时与远端会话</strong>
                  <p>
                    尚未验证流式执行器与系统虚拟麦克风，不开放实时音频路由。离线制作可继续。
                  </p>
                </div>
              </div>
            </>
          )}
        </Modal>
      )}
    </div>
  );
}

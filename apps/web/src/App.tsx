import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
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
    mode: p.mode || "quick",
  };
}
function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}
function mergeEditorPatch(base: Bag, delta: Bag): Bag {
  const merged = { ...base, ...delta };
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
  return merged;
}
function isAudio(a: Artifact) {
  return (
    a.sample_count > 0 &&
    !String(a.media_type).includes("json") &&
    a.stem_role !== "pitch"
  );
}

export default function App() {
  const [connection, setConnectionState] = useState("connecting");
  const [project, setProject] = useState<Project | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [device, setDevice] = useState<Bag>({});
  const [jobs, setJobs] = useState<Job[]>([]);
  const [step, setStep] = useState(0);
  const [modal, setModal] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
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
  const [zoom, setZoom] = useState(1);
  const [pitchTab, setPitchTab] = useState("pitch");
  const [pitchData, setPitchData] = useState<PitchData | null>(null);
  const [outputPitchData, setOutputPitchData] = useState<PitchData | null>(
    null,
  );
  const [relinkHash, setRelinkHash] = useState("");
  const [pitchTool, setPitchTool] = useState("select");
  const [targetHz, setTargetHz] = useState(220);
  const [semitones, setSemitones] = useState(0);
  const [lock, setLock] = useState(true);
  const [selectedTrack, setSelectedTrack] = useState("");
  const [blind, setBlind] = useState(false);
  const [format, setFormat] = useState("wav");
  const [stemSemantics, setStemSemantics] = useState("post_fader_fx");
  const [experimentShifts, setExperimentShifts] = useState("-2,0,2");
  const [exportSelection, setExportSelection] = useState(false);
  const [exportId, setExportId] = useState("");
  const [exportName, setExportName] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [endpointDraft, setEndpointDraft] = useState(getConnectionEndpoint());
  const [harmonySourceId, setHarmonySourceId] = useState("");
  const [harmonyModelId, setHarmonyModelId] = useState("");
  const [pathDraft, setPathDraft] = useState("");
  const [modelPath, setModelPath] = useState("");
  const [newName, setNewName] = useState("新的声音作品");
  const [search, setSearch] = useState("");
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
  const objectUrl = useRef("");
  const applyProject = useCallback((p: Project) => {
    const n = normal(p);
    pRef.current = n;
    setProject(n);
    localStorage.setItem("sonora-project", n.id);
  }, []);
  const tell = useCallback((msg: string) => {
    setNotice(msg);
    setTimeout(() => setNotice(""), 5500);
  }, []);
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
  const patchPart = (part: VoicePartKey, changes: Bag) => {
    if (pRef.current?.voice_parts?.[part])
      update({ voice_parts: { [part]: changes } });
  };
  const param = (changes: Bag) => {
    const part = pRef.current?.parameters.active_voice_part as VoicePartKey,
      binding = pRef.current?.voice_parts?.[part];
    if (binding) patchPart(part, { parameters: changes });
    else globalParam(changes);
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
        setPlayhead(seconds(p.playhead || 0));
        playRef.current = seconds(p.playhead || 0);
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
  const legacyVocal =
    project?.assets
      .slice()
      .reverse()
      .find(
        (a) =>
          ["guide", "vocal", "vocals", "synthesized"].includes(
            a.stem_role || "",
          ) && isAudio(a),
      ) || source;
  const activePart: VoicePartKey | "vocal" = [
    "lead_vocal",
    "backing_vocal",
  ].includes(project?.parameters.active_voice_part)
    ? project!.parameters.active_voice_part
    : "vocal";
  const partBinding =
    activePart === "vocal" ? undefined : project?.voice_parts?.[activePart];
  const hasVoiceParts = Object.keys(project?.voice_parts || {}).length > 0;
  const editorParameters =
    activePart === "vocal"
      ? project?.parameters || {}
      : partBinding?.parameters || {};
  const editorPitchEdits =
    activePart === "vocal"
      ? project?.pitch_edits || []
      : partBinding?.pitch_edits || [];
  const editorNotes =
    activePart === "vocal" ? project?.notes || [] : partBinding?.notes || [];
  const vocal =
    activePart === "vocal"
      ? legacyVocal
      : project?.assets.find((a) => a.id === partBinding?.source_artifact_id);
  const candidates =
    project?.candidates.filter((c) =>
      activePart === "vocal" ? !c.voice_part : c.voice_part === activePart,
    ) || [];
  const selectedCandidateId =
    activePart === "vocal"
      ? project?.selected_candidate_id
      : partBinding?.selected_candidate_id;
  const auditionScope = `${project?.id || ""}:${activePart}`;
  const adoptedCandidate = candidates.find((c) => c.id === selectedCandidateId);
  const candidate =
    candidates.find((c) => c.id === auditionCandidateIds[auditionScope]) ||
    adoptedCandidate ||
    (activePart === "vocal"
      ? candidates
          .slice()
          .reverse()
          .find((c) => c.artifact_id)
      : undefined);
  const candidateCompatible = (c: Bag) =>
    activePart === "vocal" ||
    (!!vocal?.sha256 &&
      c.voice_part === activePart &&
      project?.assets.find(
        (a) => a.id === (c.part_source_artifact_id || c.source_artifact_id),
      )?.sha256 === vocal?.sha256);
  const comparisonA =
    project?.assets.find((a) => a.id === candidate?.source_artifact_id) ||
    (activePart === "vocal" ? source : vocal);
  const comparisonB = project?.assets.find(
    (a) => a.id === candidate?.artifact_id,
  );
  const listen =
    project?.assets.find((a) => a.id === listenId) ||
    (ab === "B" ? comparisonB : comparisonA) ||
    (activePart === "vocal" ? source : vocal);
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
  const lastPitch = project?.assets
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
  const activeCount = jobs.filter(activeJob).length;
  const mode = project?.mode || "quick";
  const defaultParameters: Bag = {
    transpose: 0,
    index_rate: 0,
    protect: 0.33,
    volume_envelope: 1,
    seed: 42,
  };
  const dirty =
    editorPitchEdits.length +
    Object.keys(editorParameters).filter(
      (k) =>
        !["model_id", "pitch_algorithm", "active_voice_part"].includes(k) &&
        JSON.stringify(editorParameters[k]) !==
          JSON.stringify(defaultParameters[k]),
    ).length;
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
        if (live) setPitchData(d);
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
          if (live) setOutputPitchData(d);
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
    media(`/artifacts/${listen.id}/audio`)
      .then((blob) => {
        if (!live) return;
        const url = URL.createObjectURL(blob);
        if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
        objectUrl.current = url;
        a.src = url;
        a.onloadedmetadata = () => {
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
  const aRms = comparisonA?.diagnostics?.rms_dbfs,
    bRms = comparisonB?.diagnostics?.rms_dbfs;
  const matchAvailable =
    Number.isFinite(aRms) &&
    Number.isFinite(bRms) &&
    Math.abs(duration(comparisonA) - duration(comparisonB)) < 0.001 &&
    Math.abs(artifactOffset(comparisonA) - artifactOffset(comparisonB)) < 0.001;
  const matchGain =
    matched && matchAvailable && !listenId
      ? 10 ** ((Math.min(aRms, bRms) - (ab === "A" ? aRms : bRms)) / 20)
      : 1;
  useEffect(() => {
    if (audio.current)
      audio.current.volume = Math.min(1, Math.max(0, volume * matchGain));
  }, [volume, matchGain]);
  const seek = (t: number) => {
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
      void a.play().catch((e) => setError(errorText(e)));
    } else a.pause();
  };
  const selectAB = (which: "A" | "B") => {
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
    setProjects((ps) => [p, ...ps]);
    return p;
  };
  const chooseImport = (role = "original") => {
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
      await job(
        "convert",
        {
          ...editorParameters,
          pitch_artifact_id: lastPitch?.id,
          pitch_edits: editorPitchEdits,
          ...(short ? { range_ticks: [selection.start, selection.end] } : {}),
          ...(chain && candidate ? { chain_candidate_id: candidate.id } : {}),
        },
        vocal?.id,
      );
    }, "提交转换任务");
  const historyAction = (direction: "undo" | "redo") =>
    guard(async () => {
      await flush();
      const p = pRef.current;
      if (!p) return;
      applyProject(
        await post<Project>(`/projects/${p.id}/history`, {
          direction,
          revision: p.revision,
        }),
      );
      tell(direction === "undo" ? "已撤销上一步编辑。" : "已重做。");
    });
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
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
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
          void audio.current?.play().catch((e) => setError(errorText(e)));
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
    combinedSources.find((a) => a.id === harmonySourceId) ||
    combinedSources.at(-1);
  const activeHarmonyModel =
    harmonyModels.find((m) => m.id === harmonyModelId) ||
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
              onChange={(e) => setHarmonySourceId(e.target.value)}
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
  const partVoicesPanel =
    Object.keys(parts).length > 0 ? (
      <Card
        title="主唱与和声 · 各自的声音"
        icon="branch"
        aside={<Badge tone="blue">独立模型 · 独立音高</Badge>}
      >
        <div className="card-pad">
          <div className="voice-part-cards">
            {(["lead_vocal", "backing_vocal"] as const).map((part) => {
              const binding = parts[part],
                chosen = project?.candidates.find(
                  (c) => c.id === binding?.selected_candidate_id,
                );
              return (
                <div
                  className={`voice-part-card ${part} ${activePart === part ? "focused" : ""}`}
                  key={part}
                >
                  <div className="flex-between">
                    <strong>{roleNames[part]}</strong>
                    <button
                      className="text-button"
                      disabled={!binding}
                      onClick={() => focusPart(part)}
                    >
                      编辑此声部
                    </button>
                  </div>
                  {binding ? (
                    <>
                      <Field label={`${roleNames[part]}目标声音模型`}>
                        <select
                          value={binding.parameters.model_id || ""}
                          onChange={(e) =>
                            patchPart(part, {
                              parameters: { model_id: e.target.value },
                            })
                          }
                        >
                          <option value="">选择独立的 RVC 模型</option>
                          {models
                            .filter(
                              (m) =>
                                m.engine_id?.includes("rvc") ||
                                m.capabilities?.voice_conversion,
                            )
                            .map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.name}
                              </option>
                            ))}
                        </select>
                      </Field>
                      <Field label={`${roleNames[part]}处理方式`}>
                        <select
                          value={binding.mode}
                          onChange={(e) =>
                            patchPart(part, { mode: e.target.value })
                          }
                        >
                          <option value="convert">转换此声部</option>
                          <option value="original">保留原声</option>
                          <option value="mute">静音此声部</option>
                        </select>
                      </Field>
                      <p className="info-inline">
                        {binding.mode === "mute"
                          ? "此声部不进入混音。"
                          : binding.mode === "original"
                            ? "混音保留此声部的原始分轨。"
                            : chosen
                              ? `采用 ${chosen.name}；另一个声部保持自己的选择。`
                              : "待转换：目前保留此声部原声，不会冒充转换结果。"}
                      </p>
                      <button
                        className="button small"
                        disabled={
                          binding.mode !== "convert" ||
                          !binding.parameters.model_id ||
                          !!busy
                        }
                        onClick={() =>
                          void guard(
                            () =>
                              job(
                                "convert",
                                { voice_part: part },
                                binding.source_artifact_id,
                              ),
                            `提交${roleNames[part]}转换`,
                          )
                        }
                      >
                        仅转换{roleNames[part]}
                      </button>
                    </>
                  ) : (
                    <p className="info-inline">
                      先完成专用分离，或手动导入此声部。
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <div className="row wrap space-top">
            <button
              className="button primary"
              disabled={!bothPartsReady || !!busy}
              onClick={() =>
                void guard(
                  () =>
                    job("convert_parts", {
                      parts: ["lead_vocal", "backing_vocal"],
                      mix_after: true,
                    }),
                  "提交双声部转换与混音",
                )
              }
            >
              <Icon name="spark" />
              分别转换两声部并混音
            </button>
            <span className="info-inline">
              按各自模式顺序处理；保留另一个声部、伴奏与已提交成果。
            </span>
          </div>
        </div>
      </Card>
    ) : null;
  const waveformCard = (tracks?: Track[]) => {
    const rows =
      tracks ||
      project?.tracks.filter((t) => !["pitch"].includes(t.role)) ||
      [];
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
                      setListenId(a.id);
                    }}
                    onDoubleClick={() => {
                      setListenId(a.id);
                      void audio.current?.play().catch(() => {});
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
    <Field label="目标声音模型">
      <select
        value={editorParameters.model_id || ""}
        onChange={(e) => param({ model_id: e.target.value })}
      >
        <option value="">选择已注册的兼容模型</option>
        {models
          .filter(
            (m) =>
              m.engine_id?.includes("rvc") || m.capabilities?.voice_conversion,
          )
          .map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
      </select>
      {currentModel && (
        <span className="model-sub">
          {currentModel.sample_rate / 1000} kHz · {currentModel.engine_id} ·{" "}
          {stateLabel[currentModel.test_status] || currentModel.test_status}
        </span>
      )}
    </Field>
  );
  const algorithms = engines.find((e) => e.id === "pitch")?.capabilities
    ?.algorithms;
  const algorithmOptions = Array.isArray(algorithms)
    ? algorithms.map((a: Bag | string) =>
        typeof a === "string"
          ? { id: a, label: a, status: "ready" }
          : {
              id: a.id || a.name,
              label: a.name || a.id,
              status: a.status || "ready",
            },
      )
    : [
        { id: "rmvpe", label: "RMVPE · 常规歌声", status: "ready" },
        { id: "pyin", label: "pYIN · CPU 单声部", status: "ready" },
      ];
  const algorithmField = (
    <Field
      label="音高检测算法"
      hint="F0 是连续音高估计；无旋律参考时不评价音准。"
    >
      <select
        value={editorParameters.pitch_algorithm || "rmvpe"}
        onChange={(e) => param({ pitch_algorithm: e.target.value })}
      >
        {algorithmOptions.map((a: Bag) => (
          <option
            key={a.id}
            value={a.id}
            disabled={!["ready", "available", "installed"].includes(a.status)}
          >
            {a.label}
            {a.status !== "ready"
              ? ` · ${stateLabel[a.status] || a.status}`
              : ""}
          </option>
        ))}
      </select>
    </Field>
  );
  const addPitchEdit = (operation: string, extra: Bag = {}) => {
    if (selection.end <= selection.start) {
      tell("先选择一段非零长度的区间。");
      return;
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

  const latestStageNode = (index: number) => {
    const types: Record<number, string[]> = {
      3: ["convert", "convert_parts", "pipeline", "comp"],
      4: ["mix"],
      5: ["export", "export_stems"],
    };
    return project?.graph
      .slice()
      .reverse()
      .find(
        (n) =>
          types[index]?.includes(n.type) &&
          (index !== 3 ||
            (!n.parameters?.range_ticks &&
              (!n.voice_part || n.voice_part === activePart))),
      );
  };
  let content: ReactNode;
  if (step === 0)
    content = (
      <div className="layout-split">
        <div className="stack">
          <Card className="card-pad">
            <div
              className={`drop-zone ${busy ? "working" : ""}`}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const f = e.dataTransfer.files[0];
                if (f)
                  void guard(() => importFile(f, "original"), "正在导入与解码");
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
                    disabled={!!busy || connection !== "ready"}
                    onClick={() => chooseImport()}
                  >
                    <Icon name="import" />
                    选择音频
                  </button>
                  <button
                    className="text-button"
                    disabled={!!busy || connection !== "ready"}
                    onClick={() => {
                      roleRef.current = "original";
                      setModal("import-path");
                    }}
                  >
                    从本机路径导入
                  </button>
                  <button
                    className="text-button"
                    disabled={!!busy || connection !== "ready"}
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
            <div className="section-caption">
              你想完成什么？<span>可随时切换，保留工程</span>
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
                  disabled={!project}
                  onClick={() => update({ task: id })}
                >
                  <Icon name={icon} />
                  <strong>{title}</strong>
                  <small>{sub}</small>
                </button>
              ))}
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
          <Card className="card-pad">{selectionControls}</Card>
        </div>
        <aside className="inspector stack">
          <Card className="card-pad">
            <div className="inspector-heading">
              <strong>制作起点</strong>
              <span>PROJECT</span>
            </div>
            {modelField}
            <button
              className="button full ghost"
              onClick={() => setModal("models")}
            >
              <Icon name="box" />
              管理声音与模型
            </button>
            <div className="line" />
            <div className="helper-box">
              <strong>
                {project?.task === "speech"
                  ? "干净语音可以直接转换"
                  : project?.task === "rewrite"
                    ? "新歌词，需要新的演唱"
                    : "先整理人声，再生成候选"}
              </strong>
              <p>
                {project?.task === "speech"
                  ? "确认输入是一条干净的单声部，再提取音高并进行变声。"
                  : project?.task === "rewrite"
                    ? "导入唱着新词的导唱，或录制新演唱。文字编辑本身不会改变音频。"
                    : "含伴奏的歌曲推荐先分离；如果已有干人声，可以直接导入。"}
              </p>
            </div>
            <button
              className="button primary full space-top"
              disabled={
                Object.keys(parts).length > 0
                  ? !bothPartsReady || !!busy
                  : !source || !currentModel || !!busy
              }
              onClick={() =>
                void guard(
                  () =>
                    Object.keys(parts).length > 0
                      ? job("convert_parts", {
                          parts: ["lead_vocal", "backing_vocal"],
                          mix_after: true,
                        })
                      : job(
                          "pipeline",
                          {
                            ...project?.parameters,
                            skip_separation: project?.task === "speech",
                          },
                          project?.task === "rewrite" ? vocal?.id : source?.id,
                        ),
                  "提交完整流程",
                )
              }
            >
              <Icon name="spark" />
              {Object.keys(parts).length > 0
                ? "继续双声部转换与混音"
                : "运行真实完整流程"}
            </button>
            <p className="info-inline">
              任务在本机执行；进度与失败原因会出现在队列。
            </p>
          </Card>
          <div className="helper-box">
            <strong>先试听一个乐句。</strong>
            <p>选择 8–15 秒代表片段，确认音色、发音和接缝，再完成整首。</p>
          </div>
        </aside>
      </div>
    );
  else if (step === 1)
    content = (
      <div className="layout-split">
        <div className="stack">
          {waveformCard()}
          <Card className="card-pad">
            {selectionControls}
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
              <button className="text-button" onClick={() => setStep(2)}>
                已有干声，跳过分离 <Icon name="arrow" />
              </button>
            </div>
          </Card>
          {harmonyPanel}
          <div className="risk-card">
            <span className="risk-icon">i</span>
            <div>
              <h3>和声也是一条独立的旋律。</h3>
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
            <div className="helper-box space-top">
              <strong>处理更少，也是一种选择。</strong>
              <p>
                不要默认串联最强降噪与去混响。保留气声、齿音与句尾，逐步比较实际听感。
              </p>
            </div>
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
                      绘制目标节点
                    </button>
                  </div>
                  <span className="mini-note">目标编辑在重新渲染后生效</span>
                </div>
                {pitchData ? (
                  <PitchCanvas
                    data={pitchData}
                    outputData={outputPitchData}
                    transpose={editorParameters.transpose || 0}
                    timelineDuration={maxDuration}
                    selection={selection}
                    edits={editorPitchEdits || []}
                    notes={editorNotes || []}
                    playhead={playhead}
                    onSelect={(s) => update({ selection: s })}
                    tool={pitchTool}
                    onPoint={(t, hz) =>
                      update({
                        pitch_edits: [
                          ...(editorPitchEdits || []),
                          {
                            id: uid(),
                            start_tick: t,
                            end_tick:
                              t + ticks(pitchData.hop / pitchData.sample_rate),
                            operation: "set_hz",
                            hz: +hz.toFixed(3),
                            locked: lock,
                          },
                        ],
                      })
                    }
                  />
                ) : (
                  <Empty title="先检测真实的人声音高" icon="note">
                    <p>不会生成示例曲线来代替你的素材分析。</p>
                    <button
                      className="button primary"
                      disabled={!vocal || !!busy}
                      onClick={() =>
                        void guard(
                          () =>
                            job(
                              "pitch",
                              {
                                algorithm:
                                  editorParameters.pitch_algorithm || "rmvpe",
                              },
                              vocal?.id,
                            ),
                          "提交音高分析",
                        )
                      }
                    >
                      开始音高分析
                    </button>
                  </Empty>
                )}
                <div className="card-pad">{selectionControls}</div>
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
                    editorPitchEdits.map((e, i) => (
                      <div className="edit-row" key={e.id}>
                        <span>
                          {i + 1}. {clock(seconds(e.start_tick))}–
                          {clock(seconds(e.end_tick))}
                        </span>
                        <strong>
                          {e.operation === "transpose"
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
                                x.id === e.id ? { ...x, locked: !x.locked } : x,
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
                                (x) => x.id !== e.id,
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
                {algorithmField}
                <button
                  className="button full"
                  disabled={!vocal || !!busy}
                  onClick={() =>
                    void guard(
                      () =>
                        job(
                          "pitch",
                          {
                            algorithm:
                              editorParameters.pitch_algorithm || "rmvpe",
                          },
                          vocal?.id,
                        ),
                      "提交音高复核",
                    )
                  }
                >
                  重新检测 · 保留编辑
                </button>
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
                <button
                  className="button primary full"
                  disabled={
                    !currentModel ||
                    !pitchData ||
                    !!busy ||
                    (activePart !== "vocal" && partBinding?.mode !== "convert")
                  }
                  onClick={() => void renderCandidate(true)}
                >
                  <Icon name="play" />
                  渲染选区试听
                </button>
              </Card>
              <div className="helper-box">
                <strong>不把歌唱磨成一条直线。</strong>
                <p>
                  保留颤音与滑音。原生算法分数不会转换成“音准百分比”；只有参考旋律才能支持音准比较。
                </p>
              </div>
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
        <div className="layout-split">
          <div className="stack">
            {partVoicesPanel}
            <Card
              title="每个候选，都从干净源出发"
              icon="branch"
              aside={<Badge tone="neutral">同源候选</Badge>}
            >
              <div className="pipeline-row">
                <div className="pipeline-node">
                  <strong>
                    {activePart !== "vocal"
                      ? roleNames[activePart]
                      : project?.task === "rewrite"
                        ? "新词导唱"
                        : "当前干净人声"}
                  </strong>
                  <small>{vocal?.name || "等待导入"}</small>
                </div>
                <span>→</span>
                <div className="pipeline-node">
                  <strong>已确认的目标音高</strong>
                  <small>{editorPitchEdits.length || 0} 项人工调整</small>
                </div>
                <span>→</span>
                <div className="pipeline-node">
                  <strong>{currentModel?.name || "选择声音模型"}</strong>
                  <small>真实 RVC 执行器</small>
                </div>
              </div>
            </Card>
            <div className="flex-between">
              <h2 className="section-title">
                候选与版本 <span>{candidates.length || 0}</span>
              </h2>
              <label className="inline-check">
                <input
                  type="checkbox"
                  checked={blind}
                  onChange={(e) => setBlind(e.target.checked)}
                />
                隐藏模型名盲听
              </label>
            </div>
            <div className="version-grid">
              {candidates.map((c, i) => (
                <div
                  className={`version-card ${candidate?.id === c.id ? "selected" : ""}`}
                  key={c.id}
                >
                  <div className="version-top">
                    <h3>
                      {blind
                        ? `候选 ${String.fromCharCode(65 + i)}`
                        : c.name || `候选 ${i + 1}`}
                    </h3>
                    <span className="version-number">#{i + 1}</span>
                  </div>
                  <p>
                    {blind
                      ? "模型信息已隐藏"
                      : models.find((m) => m.id === c.model_id)?.name ||
                        c.model_id}
                    <br />
                    {c.parameters?.segments
                      ? "同源片段拼选 · 需检查接缝"
                      : c.generation_depth > 1
                        ? `显式串联 · 第 ${c.generation_depth} 代`
                        : "从干净源生成 · 第 1 代"}
                  </p>
                  <div className="chip-row">
                    {c.preview_only && (
                      <Badge tone="orange">片段试听 · 不改变混音</Badge>
                    )}
                    <span className="chip">
                      {c.parameters?.transpose || 0} st
                    </span>
                    <span className="chip">
                      {stateLabel[c.state] || c.state || "已渲染"}
                    </span>
                    <span className="chip">
                      源 …{c.source_artifact_id?.slice(-8)}
                    </span>
                  </div>
                  <div className="version-actions space-top">
                    <button
                      className={`button small ${adoptedCandidate?.id === c.id ? "primary" : ""}`}
                      disabled={
                        !c.artifact_id ||
                        !!c.preview_only ||
                        !candidateCompatible(c) ||
                        (activePart === "vocal" &&
                          Object.keys(parts).length > 0)
                      }
                      onClick={() => {
                        update({ selected_candidate_id: c.id });
                        setAuditionCandidateIds((previous) => ({
                          ...previous,
                          [auditionScope]: c.id,
                        }));
                        setListenId("");
                        setAB("B");
                      }}
                    >
                      {c.preview_only
                        ? "片段仅供试听"
                        : !candidateCompatible(c)
                          ? "旧输入仅供试听"
                          : activePart === "vocal" &&
                              Object.keys(parts).length > 0
                            ? "旧版仅供试听"
                            : adoptedCandidate?.id === c.id
                              ? "已选候选"
                              : "采用此候选"}
                    </button>
                    <button
                      className="button ghost small"
                      disabled={!c.artifact_id}
                      onClick={() => {
                        setAuditionCandidateIds((previous) => ({
                          ...previous,
                          [auditionScope]: c.id,
                        }));
                        setListenId("");
                        setAB("B");
                        tell(
                          c.preview_only
                            ? "已载入片段试听；不会改变整曲混音或另一声部。"
                            : "候选已载入试听；混音采用状态保持不变。",
                        );
                      }}
                    >
                      <Icon name="play" />
                      试听
                    </button>
                    <button
                      className={`chip ${c.favorite ? "active" : ""}`}
                      onClick={() =>
                        update({
                          candidates: project!.candidates.map((x) =>
                            x.id === c.id ? { ...x, favorite: !x.favorite } : x,
                          ),
                        })
                      }
                    >
                      {c.favorite ? "已收藏" : "收藏"}
                    </button>
                    <button
                      className="text-button"
                      disabled={!c.artifact_id || !!busy}
                      onClick={() =>
                        void guard(
                          () =>
                            job(
                              "pitch",
                              {
                                pitch_algorithm:
                                  editorParameters.pitch_algorithm || "rmvpe",
                              },
                              c.artifact_id,
                            ),
                          "提交输出音高复测",
                        )
                      }
                    >
                      复测输出音高
                    </button>
                  </div>
                  <input
                    className="candidate-note"
                    aria-label={`候选 ${i + 1} 备注`}
                    value={c.notes || ""}
                    placeholder="写下听感与下一步调整…"
                    onChange={(e) =>
                      update({
                        candidates: project!.candidates.map((x) =>
                          x.id === c.id ? { ...x, notes: e.target.value } : x,
                        ),
                      })
                    }
                  />
                </div>
              ))}
            </div>
            {!candidates.length && (
              <Card>
                <Empty title="第一版声音，值得认真试听" icon="headphones">
                  <p>选择模型，先渲染当前选区。候选保留完整来源与参数。</p>
                </Empty>
              </Card>
            )}
            <Card className="card-pad">
              {selectionControls}
              <div className="row wrap space-top">
                <button
                  className="button primary"
                  disabled={
                    !vocal ||
                    !currentModel ||
                    !!busy ||
                    (activePart !== "vocal" &&
                      partBinding?.mode !== "convert") ||
                    selection.end <= selection.start
                  }
                  onClick={() => void renderCandidate(true)}
                >
                  <Icon name="spark" />
                  生成选区试听
                </button>
                <button
                  className="button ghost"
                  disabled={
                    !vocal ||
                    !currentModel ||
                    !!busy ||
                    (activePart !== "vocal" && partBinding?.mode !== "convert")
                  }
                  onClick={() => void renderCandidate(false)}
                >
                  生成整段候选
                </button>
                <button
                  className="text-button"
                  onClick={() => setModal("queue")}
                >
                  查看任务队列
                </button>
              </div>
              <p className="info-inline">
                选区试听包含前后文，并在原时间轴上对齐。短片段结果不保证逐采样等同整段渲染；修改参数后，上一版继续保留。
              </p>
            </Card>
            {project &&
              activePart === "vocal" &&
              !hasVoiceParts &&
              mode !== "quick" &&
              !!candidates.length && (
                <Card
                  title="逐句拼选"
                  icon="branch"
                  aside={<Badge tone="purple">同源片段 · 交叉淡化</Badge>}
                >
                  <div className="card-pad">
                    <Field label="基础候选（保留整段时间轴）">
                      <select
                        value={
                          project.parameters.comp_base_candidate_id ||
                          candidate?.id ||
                          ""
                        }
                        onChange={(e) =>
                          param({ comp_base_candidate_id: e.target.value })
                        }
                      >
                        {candidates.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <p className="info-inline">
                      当前采用的候选可覆盖所选句子。接缝在覆盖区间内部淡化，保留整段长度；不同来源或重叠区间会被拒绝。
                    </p>
                    <button
                      className="button"
                      disabled={!candidate || selection.end <= selection.start}
                      onClick={() => {
                        const segments: Bag[] =
                          project.parameters.comp_segments || [];
                        if (
                          segments.some(
                            (s) =>
                              s.start_tick < selection.end &&
                              s.end_tick > selection.start,
                          )
                        ) {
                          tell("此选区与已有拼选片段重叠，请先调整边界。");
                          return;
                        }
                        param({
                          comp_base_candidate_id:
                            project.parameters.comp_base_candidate_id ||
                            candidate?.id,
                          comp_segments: [
                            ...segments,
                            {
                              candidate_id: candidate?.id,
                              start_tick: selection.start,
                              end_tick: selection.end,
                              fade_ms: 20,
                            },
                          ],
                        });
                      }}
                    >
                      将当前候选用于选区
                    </button>
                    <div className="edit-list">
                      {(project.parameters.comp_segments || []).map(
                        (s: Bag, i: number) => (
                          <div className="edit-row" key={i}>
                            <span>
                              {clock(seconds(s.start_tick))} —{" "}
                              {clock(seconds(s.end_tick))}
                            </span>
                            <strong>
                              {candidates.find((c) => c.id === s.candidate_id)
                                ?.name || "候选缺失"}
                            </strong>
                            <label className="inline-check">
                              淡化 ms{" "}
                              <input
                                className="comp-fade"
                                type="number"
                                min={1}
                                max={200}
                                value={s.fade_ms}
                                aria-label={`拼选片段 ${i + 1} 淡化毫秒`}
                                onChange={(e) =>
                                  param({
                                    comp_segments:
                                      project.parameters.comp_segments.map(
                                        (x: Bag, j: number) =>
                                          i === j
                                            ? {
                                                ...x,
                                                fade_ms: Math.max(
                                                  1,
                                                  Math.min(
                                                    200,
                                                    +e.target.value,
                                                  ),
                                                ),
                                              }
                                            : x,
                                      ),
                                  })
                                }
                              />
                            </label>
                            <button
                              className="text-button"
                              onClick={() =>
                                param({
                                  comp_segments:
                                    project.parameters.comp_segments.filter(
                                      (_x: Bag, j: number) => j !== i,
                                    ),
                                })
                              }
                            >
                              移除
                            </button>
                          </div>
                        ),
                      )}
                    </div>
                    <button
                      className="button primary"
                      disabled={
                        !project.parameters.comp_segments?.length || !!busy
                      }
                      onClick={() =>
                        void guard(
                          () =>
                            job("comp", {
                              base_candidate_id:
                                project.parameters.comp_base_candidate_id ||
                                candidate?.id,
                              segments: project.parameters.comp_segments,
                            }),
                          "提交片段拼选",
                        )
                      }
                    >
                      <Icon name="branch" />
                      渲染拼选候选
                    </button>
                  </div>
                </Card>
              )}
            {waveformCard(
              project?.tracks.filter((t) =>
                activePart === "vocal"
                  ? ["vocal", "vocals", "guide", "converted"].includes(
                      t.role,
                    ) && !t.voice_part
                  : t.role === activePart || t.voice_part === activePart,
              ),
            )}
          </div>
          <aside className="inspector stack">
            <Card className="card-pad">
              <div className="inspector-heading">
                <strong>声音转换设置</strong>
                <span>RVC ADAPTER</span>
              </div>
              {modelField}
              {partModeControls}
              {algorithmField}
              <Field label="起点预设">
                <select
                  defaultValue=""
                  onChange={(e) => {
                    const presets: Record<string, Bag> = {
                      natural: { transpose: 0, index_rate: 0, protect: 0.33 },
                      soft: { transpose: 0, index_rate: 0, protect: 0.45 },
                      retrieve: {
                        transpose: 0,
                        index_rate: 0.5,
                        protect: 0.33,
                      },
                    };
                    param({
                      ...presets[e.target.value],
                      preset_id: e.target.value,
                    });
                    tell("已应用预设参数；可继续逐项调整。");
                  }}
                >
                  <option value="" disabled>
                    选择一个起点
                  </option>
                  <option value="natural">自然对白 · 不使用检索</option>
                  <option value="soft">保留清辅音 · 较强保护</option>
                  <option value="retrieve">声音检索 · 需要匹配索引</option>
                </select>
              </Field>
              <Slider
                label={
                  activePart === "vocal"
                    ? "主唱整体移调"
                    : `${roleNames[activePart]}整体移调`
                }
                value={editorParameters.transpose || 0}
                min={-24}
                max={24}
                suffix="st"
                onChange={(v) => param({ transpose: v })}
              />
              <Slider
                label="检索特征比例"
                value={editorParameters.index_rate || 0}
                min={0}
                max={1}
                step={0.01}
                onChange={(v) => param({ index_rate: v })}
                hint="大于零时需要匹配的索引；不会静默忽略缺失索引。"
              />
              <Slider
                label="清辅音保护"
                value={editorParameters.protect ?? 0.33}
                min={0}
                max={0.5}
                step={0.01}
                onChange={(v) => param({ protect: v })}
              />
              {activePart === "vocal" &&
                !hasVoiceParts &&
                mode === "expert" && (
                  <div className="advanced-block">
                    <div className="warning-inline">
                      显式串联会增加转换世代，也可能积累失真。
                    </div>
                    <button
                      className="button full"
                      disabled={!candidate || !currentModel || !!busy}
                      onClick={() => void renderCandidate(false, true)}
                    >
                      以上一候选为输入，再转换一轮
                    </button>
                    <Field label="固定随机种子">
                      <input
                        type="number"
                        min={0}
                        step={1}
                        value={editorParameters.seed ?? 42}
                        onChange={(e) => param({ seed: +e.target.value })}
                      />
                    </Field>
                    <div className="line" />
                    <strong className="track-heading">短片段参数实验</strong>
                    <Field
                      label="对比移调（半音，用逗号分隔）"
                      hint="最多 8 组，同源输入、固定种子，按队列顺序执行；锁定人工曲线保持优先。"
                    >
                      <input
                        value={experimentShifts}
                        onChange={(e) => setExperimentShifts(e.target.value)}
                        placeholder="-2,0,2"
                      />
                    </Field>
                    <button
                      className="button full"
                      disabled={
                        !currentModel ||
                        !vocal ||
                        !!busy ||
                        selection.end <= selection.start
                      }
                      onClick={() =>
                        void guard(async () => {
                          const shifts = experimentShifts
                            .split(/[,，]/)
                            .map((s) => Number(s.trim()));
                          if (
                            !shifts.length ||
                            shifts.length > 8 ||
                            shifts.some(
                              (n) => !Number.isFinite(n) || Math.abs(n) > 24,
                            )
                          )
                            throw new Error(
                              "请输入 1–8 个 −24 至 +24 之间的移调数值。",
                            );
                          await flush();
                          const p = pRef.current!;
                          const result = await post<Bag>(
                            `/projects/${p.id}/experiments`,
                            {
                              revision: p.revision,
                              idempotency_key: uid(),
                              input_artifact_id: vocal?.id,
                              range_ticks: [selection.start, selection.end],
                              variants: shifts.map((transpose) => ({
                                transpose,
                                seed: p.parameters.seed ?? 42,
                              })),
                            },
                          );
                          setJobs((js) => [...result.jobs, ...js]);
                          tell(`已加入 ${result.jobs.length} 个同源参数实验。`);
                        }, "提交参数实验")
                      }
                    >
                      生成对比候选组
                    </button>
                  </div>
                )}
            </Card>
            <div className="helper-box">
              <strong>A/B 要在同一位置。</strong>
              <p>
                底部 A 是候选的输入，B
                是当前候选，共用播放头与循环范围。响度匹配使用可用的实际 RMS
                测量。
              </p>
            </div>
          </aside>
        </div>
      </>
    );
  else if (step === 4) {
    const mixTracks =
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
    content = (
      <div className="layout-split">
        <div className="stack">
          <Card
            title="混音台"
            icon="sliders"
            aside={<Badge tone="neutral">不叠加原始整曲</Badge>}
          >
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
                        <span>+12</span>
                        <span>0</span>
                        <span>−24</span>
                        <span>−60</span>
                      </div>
                    </div>
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
                    </Field>
                    <button
                      className="text-button"
                      onClick={() => {
                        setListenId(t.artifact_id);
                        tell(
                          "正在监听该轨的已完成版本。混音推子与效果需渲染后试听。",
                        );
                      }}
                    >
                      <Icon name="headphones" />
                      监听已完成轨
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
            推子与效果更改已存入配方。点击“渲染混音”生成新版本，再与旧版对照试听。
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
          {waveformCard()}
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
                                  step={k === "decay" ? 0.01 : 1}
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
                    处理顺序以启用顺序保存。静音 /
                    独奏、增益、声像和效果在渲染时生效；当前播放的是已提交产物。
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
                      {a.name} · {kindNames[a.processing_kind]}
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
                      const a =
                        project?.assets.find((x) => x.id === exportId) ||
                        project?.assets
                          .slice()
                          .reverse()
                          .find((x) => x.stem_role === "mix") ||
                        comparisonB ||
                        source;
                      await job(
                        "export",
                        {
                          artifact_id: a?.id,
                          format,
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
                      {e.format?.toUpperCase()} ·{" "}
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
              const a =
                project?.assets.find((x) => x.id === exportId) ||
                project?.assets
                  .slice()
                  .reverse()
                  .find((x) => x.stem_role === "mix") ||
                comparisonB ||
                source;
              const d = a?.diagnostics;
              const measuredExport = project?.exports
                .slice()
                .reverse()
                .find(
                  (e) =>
                    e.artifact_id === a?.id &&
                    e.measurements?.measurement_succeeded,
                );
              const measured = measuredExport?.measurements;
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
                    <span>导出综合响度</span>
                    <strong>
                      {measured?.integrated_lufs != null
                        ? `${Number(measured.integrated_lufs).toFixed(1)} LUFS`
                        : "未测量"}
                    </strong>
                  </div>
                  <div className="parameter-row">
                    <span>导出真峰值</span>
                    <strong>
                      {measured?.true_peak_dbfs != null
                        ? `${Number(measured.true_peak_dbfs).toFixed(2)} dBTP`
                        : "未测量"}
                    </strong>
                  </div>
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
            <div className="helper-box space-top">
              <strong>保留一个可以继续的版本。</strong>
              <p>
                配方包含编辑、候选与素材哈希。恢复时若缺少文件，重新链接相同素材；不会自动替换模型。
              </p>
            </div>
          </Card>
        </aside>
      </div>
    );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setStep(0)}>
          <span className="brand-mark">
            <Icon name="wave" />
          </span>
          <span>
            SONORA<small>声 序 · 音 频 工 作 台</small>
          </span>
        </button>
        <button className="project-picker" onClick={() => setModal("projects")}>
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
            const stale = latestStageNode(i)?.state === "stale";
            const done =
              i === 0
                ? !!source
                : i === 1
                  ? project?.assets.some((a) => a.stem_role === "instrumental")
                  : i === 2
                    ? !!lastPitch
                    : i === 3
                      ? !!candidate
                      : i === 4
                        ? project?.assets.some((a) => a.stem_role === "mix")
                        : !!project?.exports.length;
            return (
              <button
                className={`nav-step ${step === i ? "active" : ""} ${done ? "done" : ""}`}
                onClick={() => setStep(i)}
                key={title}
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
        <button className="nav-secondary" onClick={() => setModal("models")}>
          <Icon name="box" />
          声音与模型<span className="side-count">{models.length}</span>
        </button>
        <button className="nav-secondary" onClick={() => setStep(3)}>
          <Icon name="branch" />
          候选与版本
          <span className="side-count">{candidates.length || 0}</span>
        </button>
        <button className="nav-secondary" onClick={() => setModal("queue")}>
          <Icon name="clock" />
          任务队列<span className="side-count">{activeCount}</span>
        </button>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className={connection === "ready" ? "" : "offline"} />
            {connection === "ready" ? "文件在本机处理" : "本地服务未连接"}
          </div>
          <p>
            保留原始素材
            <br />
            每一版都有来源
          </p>
          <button className="text-button" onClick={() => setModal("settings")}>
            <Icon name="settings" />
            引擎与设置
          </button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <span>我的工作室</span>
            <span>/</span>
            <strong>{steps[step][1]}</strong>
          </div>
          <div className="top-actions">
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
                ["detail", "精细编辑"],
                ["expert", "专家设置"],
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
                    } else setModal("models");
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
              <h1>{steps[step][2]}</h1>
              <p className="page-subtitle">{steps[step][3]}</p>
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
          {step >= 2 &&
            [3, 4, 5].some((i) => latestStageNode(i)?.state === "stale") &&
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
                  onClick={() => setStep(step + 1)}
                >
                  {steps[step + 1][1]}
                  <Icon name="arrow" />
                </button>
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
              <strong>{listen?.name || "等待你的第一段声音"}</strong>
              <small>
                {listen
                  ? kindNames[listen.processing_kind]
                  : "导入音频，即可试听"}
                {listenId ? " · 单轨监听" : ""}
              </small>
            </div>
          </div>
          <div className="transport-center">
            <div className="player-buttons">
              <button
                className={`icon-button ${loop ? "on" : ""}`}
                aria-label="循环选区"
                aria-pressed={loop}
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
                onClick={() => selectAB("A")}
              >
                原始 A
              </button>
              <button
                className={ab === "B" && !listenId ? "active" : ""}
                disabled={!comparisonB}
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
                  : "需要同一区间的两份 RMS 测量；片段与整段不混用统计结果"
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
          </div>
        </footer>
        <audio
          ref={audio}
          onPlay={() => setPlaying(true)}
          onPause={() => {
            setPlaying(false);
            savePlayhead();
          }}
          onEnded={() => {
            if (loop && audio.current && selection.end > selection.start) {
              audio.current.currentTime = Math.max(
                0,
                seconds(selection.start) - listenOffset,
              );
              void audio.current.play().catch(() => setPlaying(false));
            } else setPlaying(false);
          }}
          onTimeUpdate={() => {
            const a = audio.current!;
            if (
              loop &&
              selection.end > selection.start &&
              a.currentTime + listenOffset >=
                Math.min(
                  seconds(selection.end),
                  listenOffset + duration(listen),
                )
            )
              a.currentTime = Math.max(
                0,
                seconds(selection.start) - listenOffset,
              );
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
      {notice && (
        <div className="toast show" role="status">
          {notice}
        </div>
      )}
      {modal && (
        <Modal
          title={
            (
              {
                projects: "我的工程",
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
                      setProjects((ps) => [p, ...ps]);
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
              <div className="project-list">
                {projects.map((p) => (
                  <button
                    key={p.id}
                    className="project-row"
                    onClick={() =>
                      void guard(async () => {
                        await flush();
                        audio.current?.pause();
                        applyProject(
                          await request<Project>(`/projects/${p.id}`),
                        );
                        setListenId("");
                        setModal("");
                        setStep(0);
                      })
                    }
                  >
                    <Icon name="wave" />
                    <span>
                      <strong>{p.name}</strong>
                      <small>
                        {p.updated_at} · 修订 {p.revision}
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
            <>
              <Field label="查找模型">
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="名称、架构…"
                />
              </Field>
              <div className="models-list">
                {models
                  .filter((m) =>
                    `${m.name} ${m.engine_id}`
                      .toLowerCase()
                      .includes(search.toLowerCase()),
                  )
                  .map((m) => (
                    <div className="model-card" key={m.id}>
                      <div className="row">
                        <span className="avatar">
                          <Icon name="wave" />
                        </span>
                        <div className="model-text">
                          <strong>{m.name}</strong>
                          <small>
                            {m.engine_id} · {m.sample_rate / 1000} kHz ·{" "}
                            {stateLabel[m.test_status] || m.test_status}
                          </small>
                        </div>
                        <button
                          className="button small"
                          disabled={!project}
                          onClick={() => {
                            param({ model_id: m.id });
                            setModal("");
                            tell(`已选择 ${m.name}`);
                          }}
                        >
                          选择
                        </button>
                      </div>
                      <div className="model-meta">
                        <span>SHA-256 {m.model_hash?.slice(0, 20)}…</span>
                        <span>
                          权重许可：
                          {typeof m.license === "string"
                            ? m.license
                            : m.weights_license || "未确认"}
                        </span>
                        <span>
                          外部 F0：
                          {m.capabilities?.external_f0
                            ? "支持"
                            : "按执行器验证状态"}
                        </span>
                      </div>
                    </div>
                  ))}
              </div>
              <div className="line" />
              <Field
                label="导入已有模型的本机路径"
                hint="仅从已授权资源目录读取；先安全校验，不执行任意模型代码。"
              >
                <input
                  value={modelPath}
                  onChange={(e) => setModelPath(e.target.value)}
                  placeholder="E:\voice\…\model.pth"
                />
              </Field>
              <div className="row">
                <button
                  className="button primary"
                  disabled={!modelPath || !!busy}
                  onClick={() =>
                    void guard(async () => {
                      await post("/models/import", { path: modelPath });
                      await refresh();
                      setModelPath("");
                      tell("模型已校验并注册。");
                    }, "校验模型")
                  }
                >
                  校验并导入
                </button>
                <button
                  className="button ghost"
                  onClick={() => void guard(refresh)}
                >
                  刷新
                </button>
              </div>
            </>
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
                          <small>{j.error.recovery}</small>
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
                              await post(`/jobs/${j.id}/retry`);
                              tell("已加入重试队列。");
                            })
                          }
                        >
                          重试
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
                  defaultValue="original"
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

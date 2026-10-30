import type {
  SonoraProject as SharedProject,
  Artifact as SharedArtifact,
  Tick,
} from "../../../packages/contracts/schema";
export type Bag = Record<string, any>;
export interface Artifact extends Bag {
  id: string;
  name: string;
  sha256: string;
  sample_rate: number;
  sample_count: number;
  channels: number;
  processing_kind: SharedArtifact["processing_kind"];
  media_type: string;
  stem_role?: string;
  input_artifact_ids: string[];
}
export interface Track extends Bag {
  id: string;
  name: string;
  role: string;
  artifact_id: string;
  gain_db: number;
  pan: number;
  mute: boolean;
  solo: boolean;
  offset_ticks: number;
  effects: Bag[];
}
export interface Candidate extends Bag {
  id: string;
  name: string;
  source_artifact_id: string;
  artifact_id: string;
  model_id: string;
  generation_depth: number;
  state: string;
  parameters: Bag;
  notes: string;
}
export interface Project extends Bag {
  id: string;
  name: string;
  revision: number;
  schema_version: SharedProject["schema_version"];
  task: SharedProject["task"];
  mode: NonNullable<SharedProject["mode"]>;
  timebase: SharedProject["timebase"];
  assets: Artifact[];
  tracks: Track[];
  candidates: Candidate[];
  parameters: Bag;
  selection: SharedProject["selection"];
  playhead: Tick;
  pitch_edits: Bag[];
  notes: Bag[];
  lyrics: Bag[];
  exports: Bag[];
  warnings: Bag[];
  graph: Bag[];
}
export interface Job extends Bag {
  id: string;
  project_id: string;
  operation: string;
  state: string;
  stage: string;
  message: string;
  progress: number | null;
  progress_basis: string;
  error?: { code: string; message: string; recovery: string };
  artifact_ids: string[];
}
export interface Model extends Bag {
  id: string;
  name: string;
  engine_id: string;
  model_hash: string;
  sample_rate: number;
  capabilities: Bag;
  license: string;
  test_status: string;
}
export interface Engine extends Bag {
  id: string;
  name: string;
  status: string;
  reason?: string;
  capabilities: Bag;
}
export interface PitchData extends Bag {
  sample_rate: number;
  hop: number;
  frame_center_offset: number;
  f0_hz: (number | null)[];
  voiced: boolean[];
  estimator: string;
  score_kind: string;
}
export const TB: SharedProject["timebase"] = 48000;
export const seconds = (ticks: number) => ticks / TB;
export const ticks = (seconds: number) => Math.round(seconds * TB);
export const clock = (s: number) =>
  `${Math.floor(Math.max(s, 0) / 60)
    .toString()
    .padStart(2, "0")}:${(Math.max(s, 0) % 60).toFixed(1).padStart(4, "0")}`;
export const uid = () => crypto.randomUUID();
export const duration = (a?: Artifact) =>
  a?.sample_rate ? a.sample_count / a.sample_rate : 0;
export const activeJob = (j: Job) =>
  ["queued", "preparing", "running"].includes(j.state);
export const stateLabel: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  running: "正在处理",
  blocked: "需要准备",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  ready: "就绪",
  available: "可用",
  unavailable: "不可用",
  missing: "缺少资源",
  untested: "未实测",
};

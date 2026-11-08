// Generated from model-catalog.schema.json; SHA-256 bda25f1464ecd2f3d5380d7f2bcf9dd76dfc1ed5a4338107a2e721088a70d96a.
export interface VoiceProfile {
  id: string;
  name: string;
  alias?: string;
  description?: string;
  tags: Array<string>;
  favorite: boolean;
  last_used_at?: string | null;
  default_model_id?: string | null;
  last_used_model_id?: string | null;
  grouping_evidence?: string;
  version_count?: number;
}

export interface TrainingRun {
  id: string;
  voice_id: string;
  name: string;
  path: string;
  embedder_model?: string | null;
  sample_rate?: number | null;
  grouping_evidence?: string;
  metadata_path?: string | null;
  config_path?: string | null;
}

export interface ModelArtifact {
  id: string;
  model_artifact_id?: string;
  canonical_id?: string;
  alias_of?: string;
  name?: string;
  filename?: string;
  voice_id: string;
  run_id: string;
  kind: "inference_export" | "training_checkpoint" | "index" | "unknown";
  status: "discovered" | "ready" | "missing" | "quarantined" | "training_only";
  epoch?: number | null;
  global_step?: number | null;
  epoch_source?: "checkpoint" | "filename_inferred" | "unknown";
  step_source?: "checkpoint" | "filename_inferred" | "unknown";
  version_label: string;
  model_hash?: string | null;
  sha256?: string | null;
  path: string;
  origin_path?: string;
  original_paths?: Array<string>;
  metadata_evidence?: string;
  reason?: string | null;
  last_used_at?: string | null;
  sample_rate?: number;
  content_dim?: number;
  speaker_ids?: Array<number>;
  speaker_ids_trained?: Array<number>;
  embedding_speaker_capacity?: number;
  speaker_metadata_status?: string;
  embedder_model?: string | null;
  embedder_verified?: boolean;
  can_validate?: boolean;
  can_render?: boolean;
  index_id?: string | null;
  selected_index_id?: string | null;
  index_path?: string | null;
  index_hash?: string | null;
  index_match?: IndexCompatibility;
  capabilities?: Record<string, unknown>;
}

export interface IndexArtifact {
  id: string;
  kind: "index";
  run_id: string;
  voice_id?: string;
  path: string;
  sha256?: string | null;
  content_dim: number;
  status: "discovered" | "ready" | "missing" | "quarantined";
  embedder_model?: string | null;
  provenance?: string;
  run_link_verified?: boolean;
  fully_validated?: boolean;
  compatibility?: IndexCompatibility;
}

export interface TrainingCheckpoint {
  id: string;
  kind: "training_checkpoint";
  run_id: string;
  path: string;
  status: "training_only" | "missing" | "quarantined";
  epoch?: number | null;
  global_step?: number | null;
  training_iteration?: number | null;
  filename_counter_hint?: number | null;
  counter_semantics?: "unknown_without_training_adapter";
  reason?: string;
}

export interface IndexCompatibility {
  status: "verified" | "unknown" | "incompatible";
  reasons: Array<string>;
  evidence: Array<Record<string, unknown>>;
  model_run_id?: string;
  index_run_id?: string;
  content_dim?: number;
  embedder_model?: string | null;
  fully_validated?: boolean;
}

export interface ModelSelection {
  id: string;
  model_artifact_id: string;
  model_hash: string;
  project_id?: string | null;
  track_id?: string | null;
  clip_id?: string | null;
  voice_id?: string;
  run_id?: string;
  speaker_id: number;
  index_id?: string | null;
  index_hash?: string | null;
  preset_id?: string | null;
}

export interface CatalogScan {
  id: string;
  state: "queued" | "running" | "succeeded" | "partial" | "cancelled" | "failed";
  scanned_files: number;
  counts: Record<string, unknown>;
  skipped?: number;
  roots?: Array<string>;
  budget_exhausted?: boolean;
  cancel_requested?: boolean;
  elapsed_seconds?: number;
  error?: string;
}

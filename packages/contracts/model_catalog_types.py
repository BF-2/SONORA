# Generated from model-catalog.schema.json; SHA-256 bda25f1464ecd2f3d5380d7f2bcf9dd76dfc1ed5a4338107a2e721088a70d96a.
from __future__ import annotations
from typing import Any, Literal, Required, TypedDict

class VoiceProfile(TypedDict, total=False):
    id: Required[str]
    name: Required[str]
    alias: str
    description: str
    tags: Required[list[str]]
    favorite: Required[bool]
    last_used_at: str | None
    default_model_id: str | None
    last_used_model_id: str | None
    grouping_evidence: str
    version_count: int

class TrainingRun(TypedDict, total=False):
    id: Required[str]
    voice_id: Required[str]
    name: Required[str]
    path: Required[str]
    embedder_model: str | None
    sample_rate: int | None
    grouping_evidence: str
    metadata_path: str | None
    config_path: str | None

class ModelArtifact(TypedDict, total=False):
    id: Required[str]
    model_artifact_id: str
    canonical_id: str
    alias_of: str
    name: str
    filename: str
    voice_id: Required[str]
    run_id: Required[str]
    kind: Required[Literal["inference_export", "training_checkpoint", "index", "unknown"]]
    status: Required[Literal["discovered", "ready", "missing", "quarantined", "training_only"]]
    epoch: int | None
    global_step: int | None
    epoch_source: Literal["checkpoint", "filename_inferred", "unknown"]
    step_source: Literal["checkpoint", "filename_inferred", "unknown"]
    version_label: Required[str]
    model_hash: str | None
    sha256: str | None
    path: Required[str]
    origin_path: str
    original_paths: list[str]
    metadata_evidence: str
    reason: str | None
    last_used_at: str | None
    sample_rate: int
    content_dim: int
    speaker_ids: list[int]
    speaker_ids_trained: list[int]
    embedding_speaker_capacity: int
    speaker_metadata_status: str
    embedder_model: str | None
    embedder_verified: bool
    can_validate: bool
    can_render: bool
    index_id: str | None
    selected_index_id: str | None
    index_path: str | None
    index_hash: str | None
    index_match: IndexCompatibility
    capabilities: dict[str, Any]

class IndexArtifact(TypedDict, total=False):
    id: Required[str]
    kind: Required[Literal["index"]]
    run_id: Required[str]
    voice_id: str
    path: Required[str]
    sha256: str | None
    content_dim: Required[int]
    status: Required[Literal["discovered", "ready", "missing", "quarantined"]]
    embedder_model: str | None
    provenance: str
    run_link_verified: bool
    fully_validated: bool
    compatibility: IndexCompatibility

class TrainingCheckpoint(TypedDict, total=False):
    id: Required[str]
    kind: Required[Literal["training_checkpoint"]]
    run_id: Required[str]
    path: Required[str]
    status: Required[Literal["training_only", "missing", "quarantined"]]
    epoch: int | None
    global_step: int | None
    training_iteration: int | None
    filename_counter_hint: int | None
    counter_semantics: Literal["unknown_without_training_adapter"]
    reason: str

class IndexCompatibility(TypedDict, total=False):
    status: Required[Literal["verified", "unknown", "incompatible"]]
    reasons: Required[list[str]]
    evidence: Required[list[dict[str, Any]]]
    model_run_id: str
    index_run_id: str
    content_dim: int
    embedder_model: str | None
    fully_validated: bool

class ModelSelection(TypedDict, total=False):
    id: Required[str]
    model_artifact_id: Required[str]
    model_hash: Required[str]
    project_id: str | None
    track_id: str | None
    clip_id: str | None
    voice_id: str
    run_id: str
    speaker_id: Required[int]
    index_id: str | None
    index_hash: str | None
    preset_id: str | None

class CatalogScan(TypedDict, total=False):
    id: Required[str]
    state: Required[Literal["queued", "running", "succeeded", "partial", "cancelled", "failed"]]
    scanned_files: Required[int]
    counts: Required[dict[str, Any]]
    skipped: int
    roots: list[str]
    budget_exhausted: bool
    cancel_requested: bool
    elapsed_seconds: float
    error: str

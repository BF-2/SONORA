import type { Artifact, Bag, Project, Track } from "./types";

const roleStage: Record<string, number> = {
  original: 0,
  vocal: 1,
  vocals: 1,
  instrumental: 1,
  lead_vocal: 1,
  backing_vocal: 1,
  guide: 2,
  synthesized: 2,
  pitch: 2,
  converted: 3,
  mix: 4,
  mix_preview: 4,
  stem_export: 5,
};

export function trackStage(
  project: Project,
  track: Track,
  artifact: Artifact,
): number {
  if (
    ["decode", "demo"].includes(artifact.processing_kind) ||
    (!artifact.processing_kind && artifact.original_hash)
  )
    return 0;
  const candidate = project.candidates.find(
    (item) =>
      item.id === track.candidate_id || item.artifact_id === artifact.id,
  );
  if (candidate) return candidate.preview_only ? 2 : 3;
  const stage = roleStage[artifact.stem_role || track.role];
  if (stage !== undefined) return stage;
  // Older imported media may lack a processing kind. Unclassified derived media
  // stays in the last stage instead of leaking into source preparation.
  return artifact.job_id ||
    artifact.input_artifact_ids?.length ||
    artifact.processing_kind
    ? 5
    : 0;
}

function versionArchived(project: Project, track: Track): boolean {
  const candidates = project.candidates.filter((candidate) =>
    track.candidate_id
      ? candidate.id === track.candidate_id
      : candidate.artifact_id === track.artifact_id,
  );
  if (candidates.length)
    return candidates.every((candidate) => candidate.archived);
  const runs = (project.separation_runs || []).filter((run: Bag) =>
    run.artifact_ids.includes(track.artifact_id),
  );
  return runs.length > 0 && runs.every((run: Bag) => run.archived);
}

const ids = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

export function timelineRows(
  project: Project | null,
  stage: number,
  defaults?: Track[],
  available?: Track[],
) {
  if (!project)
    return { visible: [] as Track[], archived: [] as Track[], duration: 0 };
  const assets = new Map(
    project.assets.map((artifact) => [artifact.id, artifact]),
  );
  const eligible = (available || project.tracks).filter((track) => {
    const artifact = assets.get(track.artifact_id);
    return (
      artifact?.media_type?.startsWith("audio") &&
      trackStage(project, track, artifact) <= stage
    );
  });
  const defaultIds = new Set((defaults || eligible).map((track) => track.id));
  const hidden = new Set(
    ids(project.view_preferences?.timeline_archived_track_ids),
  );
  const included = new Set(
    ids(project.view_preferences?.timeline_included_track_ids),
  );
  const visible = eligible.filter(
    (track) =>
      !hidden.has(track.id) &&
      (included.has(track.id) ||
        (defaultIds.has(track.id) && !versionArchived(project, track))),
  );
  const visibleIds = new Set(visible.map((track) => track.id));
  const duration = Math.max(
    0,
    ...eligible.map((track) => {
      const artifact = assets.get(track.artifact_id)!;
      return (
        artifact.sample_count / artifact.sample_rate +
        (track.offset_ticks ?? artifact.timeline_offset_ticks ?? 0) / 48000
      );
    }),
  );
  return {
    visible,
    archived: eligible.filter((track) => !visibleIds.has(track.id)),
    duration,
  };
}

export function timelineVisibility(
  preferences: Bag,
  trackId: string,
  archived: boolean,
) {
  const hidden = new Set(ids(preferences.timeline_archived_track_ids));
  const included = new Set(ids(preferences.timeline_included_track_ids));
  if (archived) {
    hidden.add(trackId);
    included.delete(trackId);
  } else {
    hidden.delete(trackId);
    included.add(trackId);
  }
  return {
    timeline_archived_track_ids: [...hidden],
    timeline_included_track_ids: [...included],
  };
}

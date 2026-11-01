import { useState } from "react";
import { Badge, Card, Icon } from "./components";
import { clock, duration } from "./types";
import type { Bag, Project } from "./types";

export function CandidateVersions({
  project,
  blindLabels,
  onToggleBlind,
  part,
  selectedId,
  auditionId,
  busy,
  onAdopt,
  onListen,
  onArchive,
  onUpdate,
  isCompatible,
  stateOf,
}: {
  project: Project;
  blindLabels: Record<string, string> | null;
  onToggleBlind: () => void;
  part: string;
  selectedId?: string | null;
  auditionId?: string;
  busy: boolean;
  onAdopt: (candidate: Bag) => void;
  onListen: (candidate: Bag) => void;
  onArchive: (candidate: Bag, archived: boolean) => void;
  onUpdate: (candidate: Bag, changes: Bag) => void;
  isCompatible: (candidate: Bag) => boolean;
  stateOf: (candidate: Bag) => Bag | undefined;
}) {
  const [tab, setTab] = useState("active"),
    [query, setQuery] = useState("");
  const candidates = project.candidates.filter((candidate) =>
    part === "vocal" ? !candidate.voice_part : candidate.voice_part === part,
  );
  const visible = candidates
    .filter((candidate) =>
      tab === "archived"
        ? candidate.archived
        : tab === "pitch"
          ? !candidate.archived && candidate.preview_only
          : !candidate.archived && !candidate.preview_only,
    )
    .filter((candidate) =>
      `${candidate.name} ${candidate.notes || ""}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    )
    .reverse();
  if (blindLabels)
    visible.sort((a, b) =>
      (blindLabels[a.id] || a.id).localeCompare(blindLabels[b.id] || b.id),
    );
  return (
    <Card
      title={`${({ vocal: "完整人声", lead_vocal: "主唱", backing_vocal: "和声" } as Bag)[part]}生成版本`}
      icon="branch"
    >
      <div className="card-pad generation-manager" id="candidate-list">
        <div className="row wrap">
          <div className="segmented">
            {[
              ["active", "完整版本"],
              ["pitch", "音高试听记录"],
              ["archived", "已归档"],
            ].map(([id, label]) => (
              <button
                key={id}
                className={tab === id ? "active" : ""}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <input
            aria-label="搜索音频版本"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索版本名称或备注"
          />
        </div>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={!!blindLabels}
            onChange={onToggleBlind}
          />
          隐藏模型名盲听
        </label>
        {!visible.length && (
          <p className="info-inline">
            {tab === "active"
              ? "生成完整人声后，选择要进入混音的版本。"
              : "此列表暂无记录。"}
          </p>
        )}
        {visible.map((candidate) => {
          const asset = project.assets.find(
            (asset) => asset.id === candidate.artifact_id,
          );
          const protectedVersion =
            candidate.id === selectedId ||
            candidate.favorite ||
            project.tracks.some(
              (track) =>
                track.artifact_id === candidate.artifact_id && !track.mute,
            );
          const fresh = stateOf(candidate)?.current;
          return (
            <article
              className={`generation-row ${candidate.id === selectedId ? "selected" : ""}`}
              key={candidate.id}
              data-candidate-id={candidate.id}
            >
              <div className="generation-header">
                <strong>
                  {blindLabels
                    ? blindLabels[candidate.id] || "新版本"
                    : candidate.name}
                </strong>
                <div className="row wrap">
                  {candidate.id === selectedId && <Badge>混音采用</Badge>}
                  {candidate.id === auditionId && (
                    <Badge tone="blue">当前试听</Badge>
                  )}
                  {fresh === false && <Badge tone="orange">旧参数</Badge>}
                  {candidate.preview_only && (
                    <Badge tone="neutral">音高选区试听</Badge>
                  )}
                </div>
              </div>
              {!blindLabels && (
                <p>
                  {candidate.model_snapshot?.voice_name ||
                    candidate.model_snapshot?.name ||
                    candidate.model_id}{" "}
                  · Epoch {candidate.model_snapshot?.epoch ?? "—"} ·{" "}
                  {clock(duration(asset))} ·{" "}
                  {new Date(
                    candidate.created_at || project.created_at,
                  ).toLocaleString("zh-CN")}
                </p>
              )}
              <div className="row wrap">
                <button
                  className="button small"
                  disabled={busy}
                  onClick={() => onListen(candidate)}
                >
                  <Icon name="play" />
                  试听版本
                </button>
                {!candidate.preview_only && !candidate.archived && (
                  <button
                    className="button small primary"
                    disabled={
                      busy ||
                      candidate.id === selectedId ||
                      !isCompatible(candidate) ||
                      (part === "vocal" &&
                        Object.keys(project.voice_parts || {}).length > 0)
                    }
                    onClick={() => onAdopt(candidate)}
                  >
                    {candidate.id === selectedId
                      ? "当前混音版本"
                      : "采用到混音"}
                  </button>
                )}
                <button
                  className={`chip ${candidate.favorite ? "active" : ""}`}
                  onClick={() =>
                    onUpdate(candidate, { favorite: !candidate.favorite })
                  }
                >
                  {candidate.favorite ? "取消收藏" : "收藏"}
                </button>
                <button
                  className="text-button"
                  disabled={busy || (!candidate.archived && protectedVersion)}
                  onClick={() => onArchive(candidate, !candidate.archived)}
                >
                  {candidate.archived ? "恢复版本" : "归档版本"}
                </button>
              </div>
              {!blindLabels && (
                <>
                  <input
                    aria-label={`${candidate.name}备注`}
                    value={candidate.notes || ""}
                    placeholder="记录这一版的听感"
                    onChange={(e) =>
                      onUpdate(candidate, { notes: e.target.value })
                    }
                  />
                  <details>
                    <summary>输入、模型与实际参数</summary>
                    <pre>
                      {JSON.stringify(
                        {
                          source: project.assets.find(
                            (a) => a.id === candidate.source_artifact_id,
                          )?.name,
                          input_hash: candidate.source_hash,
                          model_hash: candidate.model_manifest_hash,
                          parameters:
                            candidate.effective_parameters ||
                            candidate.parameters,
                          requested_f0_hash: candidate.requested_f0_hash,
                          consumed_f0_hash: candidate.consumed_f0_hash,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                </>
              )}
            </article>
          );
        })}
      </div>
    </Card>
  );
}

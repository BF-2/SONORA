import { useEffect, useState } from "react";
import { post, request } from "./api";
import { Badge, Card, Icon } from "./components";
import { clock } from "./types";
import type { Bag, Project } from "./types";

export function SeparationVersions({
  project,
  operation,
  busy,
  flush,
  onProject,
  onListen,
  onError,
}: {
  project: Project;
  operation: "separate" | "separate_harmony";
  busy: boolean;
  flush: () => Promise<void>;
  onProject: (project: Project) => void;
  onListen: (id: string) => void;
  onError: (message: string) => void;
}) {
  const [runs, setRuns] = useState<Bag[]>([]);
  const [archived, setArchived] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true;
    request<Bag>(`/projects/${project.id}/separations`)
      .then((result) => {
        if (active)
          setRuns(
            result.runs.filter((run: Bag) => run.operation === operation),
          );
      })
      .catch((error) => active && onError(error.message));
    return () => {
      active = false;
    };
  }, [project.id, project.revision, operation]);
  const change = async (run: Bag, action: string) => {
    setLoading(true);
    try {
      await flush();
      const latest = await request<Project>(`/projects/${project.id}`);
      onProject(
        await post<Project>(
          `/projects/${project.id}/separations/${run.id}/${action}`,
          { revision: latest.revision },
        ),
      );
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  };
  const visible = runs.filter((run) => !!run.archived === archived);
  return (
    <Card
      title={operation === "separate" ? "一阶段分离结果" : "二阶段声部结果"}
      icon="layers"
    >
      <div className="card-pad generation-manager">
        <div className="row wrap">
          <div className="segmented">
            <button
              className={!archived ? "active" : ""}
              onClick={() => setArchived(false)}
            >
              可用结果 ({runs.filter((run) => !run.archived).length})
            </button>
            <button
              className={archived ? "active" : ""}
              onClick={() => setArchived(true)}
            >
              归档结果 ({runs.filter((run) => run.archived).length})
            </button>
          </div>
          <small>每次分离成套采用，音轨与后续生成按来源关联</small>
        </div>
        {!visible.length && (
          <p className="info-inline">
            {archived
              ? "没有已归档的分离结果。"
              : "完成分离后，在这里独听、比较和选定结果。"}
          </p>
        )}
        {visible.map((run) => (
          <article
            className={`generation-row ${run.selected ? "selected" : ""}`}
            key={run.id}
            data-generation-id={run.id}
          >
            <div className="generation-header">
              <strong>{run.name}</strong>
              <Badge tone={run.selected ? "" : "neutral"}>
                {run.selected
                  ? "当前采用"
                  : run.archived
                    ? "已归档"
                    : "可选版本"}
              </Badge>
            </div>
            <p>
              {new Date(run.created_at).toLocaleString("zh-CN")} ·{" "}
              {run.model_id ||
                run.parameters.harmony_model_id ||
                run.parameters.separation_model_id ||
                "默认分离模型"}
            </p>
            <div className="generation-stems">
              {run.outputs.map((output: Bag) => (
                <button
                  key={output.id}
                  className="button small ghost"
                  onClick={() => onListen(output.id)}
                >
                  <Icon name="headphones" />
                  {
                    (
                      {
                        vocal: "人声",
                        instrumental: "伴奏",
                        lead_vocal: "主唱",
                        backing_vocal: "和声",
                      } as Bag
                    )[output.stem_role]
                  }{" "}
                  · {clock(output.sample_count / output.sample_rate)}
                </button>
              ))}
            </div>
            <div className="row wrap">
              <button
                className="button small primary"
                disabled={busy || loading || run.selected || run.archived}
                onClick={() => void change(run, "adopt")}
              >
                {run.selected ? "已用于后续制作" : "采用这组分离"}
              </button>
              <button
                className="text-button"
                disabled={busy || loading || (!archived && run.protected)}
                title={
                  run.protected
                    ? "当前采用或混音中的结果受保护"
                    : "保留音频，可恢复"
                }
                onClick={() =>
                  void change(run, archived ? "restore" : "archive")
                }
              >
                {archived ? "恢复分离结果" : "归档分离结果"}
              </button>
              <details>
                <summary>分离参数与来源</summary>
                <pre>
                  {JSON.stringify(
                    {
                      input: run.input_artifact_id,
                      settings:
                        run.parameters[
                          operation === "separate"
                            ? "separation_settings"
                            : "harmony_settings"
                        ],
                      outputs: run.outputs.map((a: Bag) => ({
                        role: a.stem_role,
                        hash: a.sha256,
                        rate: a.sample_rate,
                        channels: a.channels,
                      })),
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </div>
          </article>
        ))}
      </div>
    </Card>
  );
}

import { useEffect, useState } from "react";
import { request } from "./api";
import { Badge, Icon } from "./components";
import { clock, seconds } from "./types";
import type { Bag } from "./types";

export function WorkflowStrip({
  projectId,
  revision,
  stage,
  voicePart,
  disabled,
  onAction,
  onLoaded,
  catalogRevision,
}: {
  projectId?: string;
  revision: number;
  stage: number;
  voicePart: string;
  disabled: boolean;
  onAction: (action: Bag) => void;
  onLoaded: (workflow: Bag) => void;
  catalogRevision: number;
}) {
  const [workflow, setWorkflow] = useState<Bag | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    if (!projectId) {
      setWorkflow(null);
      return;
    }
    request<Bag>(
      `/projects/${projectId}/workflow?voice_part=${encodeURIComponent(voicePart)}`,
    )
      .then((w) => {
        if (live) {
          setWorkflow(w);
          onLoaded(w);
          setError("");
        }
      })
      .catch((e) => {
        if (live) setError(e.message);
      });
    return () => {
      live = false;
    };
  }, [projectId, revision, voicePart, catalogRevision]);
  const step =
    workflow?.project_id === projectId &&
    (voicePart === "vocal"
      ? !workflow?.voice_part
      : workflow?.voice_part === voicePart)
      ? workflow?.stages?.find((s: Bag) => s.id === stage)
      : undefined;
  const action = step?.recommendation;
  const range = step?.input?.range_ticks;
  const start = Array.isArray(range) ? range[0] : range?.start,
    end = Array.isArray(range) ? range[1] : range?.end;
  const states: Record<string, string> = {
    not_started: "未开始",
    editable: "可编辑",
    preview_stale: "旧参数试听",
    preview_ready: "试听已生成",
    preview_accepted: "试听设置已采纳",
    empty: "等待输入",
    blocked: "需要准备",
    ready: "可继续",
    running: "正在处理",
    pending: "等待处理",
    completed: "已完成",
    complete: "已完成",
    succeeded: "已完成",
    stale: "需要更新",
    needs_review: "需检查",
    needs_adoption: "等待采纳",
    review: "待试听确认",
  };
  if (!projectId)
    return (
      <section className="workflow-strip" aria-label="本阶段操作指引">
        <div className="workflow-main">
          <div>
            <strong>当前输入：尚未导入</strong>
            <p>选择任务 → 导入音频 → 检查素材 → 选择下一步。</p>
            <small>先选语音变声或歌曲翻唱，原始文件只读保留。</small>
          </div>
          <button
            className="button primary workflow-primary"
            onClick={() => onAction({ id: "import_audio", label: "选择音频" })}
          >
            <Icon name="import" />
            选择音频
          </button>
        </div>
      </section>
    );
  return (
    <section className="workflow-strip" aria-label="本阶段操作指引">
      <div className="workflow-main">
        <div className="workflow-input">
          <div className="row wrap">
            <strong>当前输入：{step?.input?.name || "正在确认实际输入"}</strong>
            {step && (
              <Badge
                tone={
                  step.state === "blocked" || step.input?.stale
                    ? "orange"
                    : "blue"
                }
              >
                {step.status_text || states[step.state] || step.state}
              </Badge>
            )}
          </div>
          {typeof start === "number" && typeof end === "number" && (
            <small>
              源区间 {clock(seconds(start))} — {clock(seconds(end))}
              {step.input.stale ? " · 上游结果已变化" : ""}
            </small>
          )}
        </div>
        {action && (
          <button
            className="button primary workflow-primary"
            disabled={disabled || !!action.disabled}
            onClick={() => onAction(action)}
          >
            <Icon name={action.operation ? "play" : "arrow"} />
            {action.label}
          </button>
        )}
      </div>
      {step?.sequence && (
        <ol className="workflow-sequence">
          {step.sequence.slice(0, 6).map((item: string, i: number) => (
            <li key={i}>
              <span>{i + 1}</span>
              {item}
            </li>
          ))}
        </ol>
      )}
      {!!step?.blockers?.length && (
        <div className="workflow-blockers" role="status">
          {step.blockers.map((b: Bag, i: number) => (
            <div key={i}>
              <Icon name="info" />
              <span>
                {b.message}
                {typeof b.repair === "string" && <small>{b.repair}</small>}
              </span>
              {b.repair?.label && (
                <button
                  className="text-button"
                  onClick={() => onAction(b.repair)}
                >
                  {b.repair.label}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!!step?.outputs?.length && (
        <div className="workflow-outputs">
          {step.outputs.slice(-3).map((o: Bag, i: number) => (
            <span key={o.id || o.artifact_id || i}>
              <i />
              {o.name || o.label || o.role || "已提交产物"} ·{" "}
              {o.freshness || o.freshness_label ||
                o.confirmation_label ||
                o.state_label ||
                (o.stale
                  ? "旧参数结果"
                  : o.adopted || o.selected
                    ? "已采用"
                    : o.fresh
                      ? "可试听"
                      : "保留的版本")}
            </span>
          ))}
        </div>
      )}
      {step?.next_hint && <p className="workflow-next">{step.next_hint}</p>}
      {error && (
        <p className="warning-inline">
          无法读取当前依赖：{error}。已有编辑与完成版本保留。
        </p>
      )}
    </section>
  );
}

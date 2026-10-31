import { useEffect, useState } from "react";
import { post, request } from "./api";
import { Badge, Field, Icon } from "./components";
import type { Bag } from "./types";

const sourceLabel: Record<string, string> = {
  user_override: "用户覆盖",
  user: "用户自定",
  executor_default: "执行器默认",
  model_default: "模型已知设置",
  task_preset: "任务预设",
  model_preset: "模型专属预设",
  personal_preset: "个人预设",
  conservative: "通用保守起点",
  measured: "实测建议",
  model_known: "模型已知设置",
  model_metadata: "模型已知设置",
};
const text = (v: unknown) =>
  v == null ? "未提供" : typeof v === "object" ? JSON.stringify(v) : String(v);
export type RvcCatalog = {
  schema_version: number;
  parameter_schema_version: string;
  parameters: Bag[];
  f0_order?: string[];
  presets?: Bag[];
  warnings?: Bag[];
  model?: Bag;
};

function NumericParameter({
  parameter,
  value,
  onChange,
}: {
  parameter: Bag;
  value: unknown;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(text(value));
  const [error, setError] = useState("");
  useEffect(() => {
    setDraft(text(value));
    setError("");
  }, [value]);
  const commit = () => {
    const n = Number(draft);
    if (
      !draft.trim() ||
      !Number.isFinite(n) ||
      (parameter.minimum != null && n < parameter.minimum) ||
      (parameter.maximum != null && n > parameter.maximum) ||
      (parameter.type === "integer" && !Number.isInteger(n))
    ) {
      setError(
        `请输入 ${parameter.minimum ?? "有效数值"}–${parameter.maximum ?? "有效数值"}${parameter.type === "integer" ? " 的整数" : ""}`,
      );
      setDraft(text(value));
      return;
    }
    setError("");
    if (n !== value) onChange(n);
  };
  return (
    <>
      <div className="slider-with-value">
        {parameter.minimum != null && parameter.maximum != null && (
          <input
            type="range"
            aria-label={parameter.label}
            min={parameter.minimum}
            max={parameter.maximum}
            step={parameter.step ?? (parameter.type === "integer" ? 1 : 0.01)}
            value={Number(value ?? parameter.default ?? 0)}
            onChange={(e) => onChange(+e.target.value)}
          />
        )}
        <input
          className="numeric schema-number"
          type="number"
          aria-label={`${parameter.label}数值`}
          min={parameter.minimum}
          max={parameter.maximum}
          step={parameter.step ?? "any"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
        />
        <span>{parameter.unit || ""}</span>
      </div>
      {error && <small className="error-text">{error}</small>}
    </>
  );
}

export function RvcParameterPanel({
  modelId,
  task,
  values,
  mode,
  preferences,
  onPreferences,
  onChange,
  onError,
  pitchInfo,
  busy,
  onSchema,
}: {
  modelId?: string;
  task: string;
  values: Bag;
  mode: string;
  preferences: Bag;
  onPreferences: (changes: Bag) => void;
  onChange: (changes: Bag, source: string, presetId?: string) => void;
  onError: (message: string) => void;
  pitchInfo: Bag;
  busy: boolean;
  onSchema?: (schema: RvcCatalog) => void;
}) {
  const [schema, setSchema] = useState<RvcCatalog | null>(null),
    [presets, setPresets] = useState<Bag[]>([]),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [query, setQuery] = useState(""),
    [helpId, setHelpId] = useState(""),
    [presetName, setPresetName] = useState(""),
    [savingPreset, setSavingPreset] = useState(false);
  const valuesKey = JSON.stringify(values);
  useEffect(() => {
    let live = true;
    setLoading(true);
    const timer = setTimeout(() => {
      post<RvcCatalog>("/rvc/parameters", {
        model_id: modelId,
        task,
        values: JSON.parse(valuesKey),
        sources: values.parameter_sources || {},
        has_external_f0: true,
      })
        .then((s) => {
          if (live) {
            setSchema(s);
            onSchema?.(s);
            if (s.presets) setPresets(s.presets);
            setError("");
          }
        })
        .catch((e) => {
          if (live) setError(e.message);
        })
        .finally(() => {
          if (live) setLoading(false);
        });
    }, 200);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [modelId, task, valuesKey]);
  useEffect(() => {
    let live = true;
    request<Bag>(
      `/rvc/presets?task=${encodeURIComponent(task)}${modelId ? "&model_id=" + encodeURIComponent(modelId) : ""}`,
    )
      .then((p) => {
        if (live) setPresets(p.presets || []);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [modelId, task]);
  const setOne = (p: Bag, v: unknown) =>
    onChange({ [p.id]: v }, "user_override");
  const applyDefaults = (recommended: boolean) => {
    if (!schema) return;
    const changes: Bag = {};
    for (const p of schema.parameters) {
      if (p.supported && !p.read_only && p.type !== "diagnostic") {
        const val = recommended ? p.recommendation?.value : p.default;
        if (val !== undefined) changes[p.id] = val;
      }
    }
    onChange(changes, recommended ? "model_default" : "executor_default");
  };
  const applyPreset = (id: string) => {
    const p = presets.find((p) => p.id === id);
    if (p)
      onChange(
        p.parameters,
        p.kind === "personal" || p.source === "personal"
          ? "personal_preset"
          : "task_preset",
        id,
      );
  };
  const savePreset = async () => {
    setSavingPreset(true);
    try {
      const parameters: Bag = {};
      for (const p of schema?.parameters || [])
        if (p.supported && !p.read_only && values[p.id] !== undefined)
          parameters[p.id] = values[p.id];
      await post("/rvc/presets", {
        name: presetName.trim(),
        model_id: modelId,
        parameters,
      });
      const list = await request<Bag>(
        `/rvc/presets?task=${encodeURIComponent(task)}${modelId ? "&model_id=" + encodeURIComponent(modelId) : ""}`,
      );
      setPresets(list.presets || []);
      setPresetName("");
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingPreset(false);
    }
  };
  const isOpen = (group: string) =>
    preferences.rvc_groups?.[group] ??
    (group === "basic" || (mode === "expert" && group === "advanced"));
  const filtered = (group: string) =>
    schema?.parameters.filter(
      (p) =>
        p.group === group &&
        (!query ||
          `${p.id} ${p.label} ${p.label_en} ${p.help}`
            .toLowerCase()
            .includes(query.toLowerCase())),
    ) || [];
  return (
    <section className="rvc-parameter-panel" aria-label="实际 RVC 参数">
      <div className="inspector-heading">
        <strong>实际生效参数</strong>
        <span>RVC</span>
      </div>
      <div className="f0-source-summary">
        <strong>音高来源</strong>
        <p>
          {pitchInfo.artifact_id
            ? `${pitchInfo.estimator || "已检测"} · ${pitchInfo.locked_count || 0} 个锁定区`
            : "尚未检测；生成试听时先由独立 PitchService 分析。"}
        </p>
        {pitchInfo.edits_count > 0 && (
          <small>
            {pitchInfo.edits_count} 项目标编辑，保留清浊音与锁定优先顺序。
          </small>
        )}
        <span>外部目标 F0 进入转换条件，不在 RVC 内重新检测覆盖。</span>
      </div>
      {error && (
        <div className="warning-inline">
          参数说明暂不可用：{error}。保留已保存值，不开放未知控制。
        </div>
      )}
      {!modelId && (
        <p className="info-inline">
          先选择并校验模型版本，才能确定索引、说话人和编码器的适用条件。
        </p>
      )}
      <Field label="任务与个人预设">
        <select
          value={values.preset_id || ""}
          onChange={(e) => applyPreset(e.target.value)}
          disabled={!schema || busy}
        >
          <option value="">选择一个有说明的起点</option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.kind === "personal" || p.source === "personal"
                ? " · 个人"
                : ""}
            </option>
          ))}
        </select>
        {values.preset_id && (
          <small>
            {presets.find((p) => p.id === values.preset_id)?.description ||
              presets.find((p) => p.id === values.preset_id)?.explanation}
          </small>
        )}
      </Field>
      <div className="row wrap parameter-tools">
        <button
          className="quiet-button"
          disabled={!schema || busy}
          onClick={() => applyDefaults(true)}
        >
          推荐设置
        </button>
        <button
          className="quiet-button"
          disabled={!schema || busy}
          onClick={() => applyDefaults(false)}
        >
          恢复模型默认
        </button>
      </div>
      {mode !== "quick" && (
        <Field label="搜索参数与用途">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="如清辅音、音量、阈值"
          />
        </Field>
      )}
      {["basic", "advanced", "diagnostic"].map((group) => {
        const rows = filtered(group);
        if (!rows.length && group !== "diagnostic") return null;
        const opened = query ? true : isOpen(group);
        return (
          <div className="parameter-group" key={group}>
            <button
              type="button"
              className="group-heading"
              aria-expanded={opened}
              onClick={() =>
                onPreferences({
                  rvc_groups: { ...preferences.rvc_groups, [group]: !opened },
                })
              }
            >
              <strong>
                {
                  (
                    {
                      basic: "基础参数",
                      advanced: "高级参数",
                      diagnostic: "诊断与实际条件",
                    } as Bag
                  )[group]
                }
              </strong>
              <span>
                {rows.length} {opened ? "−" : "+"}
              </span>
            </button>
            {opened && (
              <div className="parameter-group-body">
                {rows.map((p) => {
                  const value = values[p.id] ?? p.current_value ?? p.default;
                  const unavailable = !p.supported || p.read_only;
                  return (
                    <div
                      className={`parameter-control ${unavailable ? "unavailable" : ""}`}
                      key={p.id}
                    >
                      <div className="parameter-label">
                        <label>
                          {p.label}
                          <small>{p.label_en || p.id}</small>
                        </label>
                        <Badge tone={unavailable ? "neutral" : "blue"}>
                          {sourceLabel[
                            values.parameter_sources?.[p.id] || p.current_source
                          ] ||
                            p.current_source ||
                            "执行器默认"}
                        </Badge>
                      </div>
                      {unavailable ? (
                        <div className="parameter-readonly">
                          <output>
                            {p.type === "boolean"
                              ? value
                                ? "开启"
                                : "关闭"
                              : text(value)}{" "}
                            {p.unit || ""}
                          </output>
                          <small>
                            {p.support_reason ||
                              (p.read_only
                                ? "由当前模型或时间基准固定"
                                : "当前执行器未提供此控制")}
                          </small>
                        </div>
                      ) : p.enum ? (
                        <select
                          aria-label={p.label}
                          value={text(value)}
                          onChange={(e) =>
                            setOne(
                              p,
                              typeof p.enum[0] === "number"
                                ? Number(e.target.value)
                                : e.target.value,
                            )
                          }
                        >
                          {p.enum.map((v: unknown) => (
                            <option key={text(v)} value={text(v)}>
                              {text(v)}
                            </option>
                          ))}
                        </select>
                      ) : p.type === "boolean" ? (
                        <label className="toggle-row">
                          <span>{p.label}</span>
                          <input
                            type="checkbox"
                            checked={!!value}
                            onChange={(e) => setOne(p, e.target.checked)}
                          />
                        </label>
                      ) : (
                          Array.isArray(p.type)
                            ? p.type.includes("number") ||
                              p.type.includes("integer")
                            : ["number", "integer"].includes(p.type)
                        ) ? (
                        <NumericParameter
                          parameter={p}
                          value={value}
                          onChange={(v) => setOne(p, v)}
                        />
                      ) : (
                        <input
                          aria-label={p.label}
                          value={text(value)}
                          onChange={(e) => setOne(p, e.target.value)}
                        />
                      )}
                      <p className="parameter-help">
                        {p.help}
                        {p.direction && <span> {p.direction}</span>}
                      </p>
                      <div className="row wrap parameter-mini-actions">
                        <button
                          type="button"
                          className="text-button"
                          aria-expanded={helpId === p.id}
                          onClick={() => setHelpId(helpId === p.id ? "" : p.id)}
                        >
                          为什么推荐？
                        </button>
                        {!unavailable && (
                          <button
                            type="button"
                            className="text-button"
                            onClick={() =>
                              onChange(
                                { [p.id]: p.default },
                                "executor_default",
                              )
                            }
                          >
                            恢复默认 {text(p.default)}
                          </button>
                        )}
                      </div>
                      {helpId === p.id && (
                        <div className="parameter-explanation">
                          <p>
                            <strong>建议起点：</strong>
                            {text(p.recommendation?.value)}；
                            {sourceLabel[p.recommendation?.source_type] ||
                              p.recommendation?.source_type ||
                              "通用保守起点"}
                            。
                            {p.recommendation?.reason ||
                              p.recommendation?.basis}
                          </p>
                          {p.conditions && (
                            <p>
                              <strong>适用条件：</strong>
                              {text(p.conditions)}
                            </p>
                          )}
                          {p.risk && (
                            <p>
                              <strong>可能影响：</strong>
                              {text(p.risk)}
                            </p>
                          )}
                          <p>
                            <strong>重算范围：</strong>
                            {text(p.recompute_scope)}
                          </p>
                          {group === "diagnostic" || mode === "expert" ? (
                            <p>
                              <strong>实际映射：</strong>
                              {text(p.native_mapping)}
                            </p>
                          ) : null}
                          {(p.dependencies || []).map((d: Bag, i: number) => (
                            <p key={i}>
                              {d.reason ||
                                `${d.parameter} ${text(d.condition)}`}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
                {group === "diagnostic" && (
                  <div className="diagnostic-values">
                    <p>
                      模型：{modelId || "未选择"}
                      <br />
                      参数 schema：
                      {schema?.parameter_schema_version || "未连接"}
                    </p>
                    {(schema?.f0_order || []).map((s, i) => (
                      <div key={i}>
                        {i + 1}. {s}
                      </div>
                    ))}
                    {pitchInfo.requested_f0_hash && (
                      <p>
                        请求 F0：<code>{pitchInfo.requested_f0_hash}</code>
                        <br />
                        消费 F0：
                        <code>{pitchInfo.consumed_f0_hash || "尚未渲染"}</code>
                      </p>
                    )}
                    <p className="info-inline">
                      请求与消费网格可能不同。是否生效以实际条件变换、哈希与输出复核为准。
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
      {loading && (
        <p className="info-inline" role="status">
          更新参数适用条件…
        </p>
      )}
      <details className="personal-preset">
        <summary>保存为个人预设</summary>
        <Field label="个人预设名称">
          <input
            maxLength={80}
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder="例如：保留气声 · 短句"
          />
        </Field>
        <button
          className="button"
          disabled={!presetName.trim() || savingPreset || !schema}
          onClick={() => void savePreset()}
        >
          <Icon name="save" />
          保存当前可生效参数
        </button>
      </details>
    </section>
  );
}

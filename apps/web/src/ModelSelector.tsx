import { useEffect, useId, useMemo, useRef, useState } from "react";
import { request, post, patch } from "./api";
import { Badge, Field, Icon } from "./components";
import type { Bag, Model } from "./types";

const statusNames: Record<string, string> = {
  ready: "已校验可加载",
  discovered: "待单独校验",
  missing: "文件缺失",
  quarantined: "已排除推理",
  training_only: "训练资源",
};
const evidenceNames: Record<string, string> = {
  static_checkpoint_schema: "检查点结构证据",
  legacy_registered: "已有注册记录",
  filename_inferred: "文件名推测",
  unknown: "未知",
};
export const versionLabel = (m?: Bag | null) =>
  m?.version_label ||
  (m?.epoch != null
    ? `Epoch ${m.epoch}`
    : `版本 ${String(m?.model_hash || m?.id || "未知").slice(-8)}`);
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
type SelectionProps = {
  label?: string;
  value: Bag;
  fallback?: Model;
  usedModelIds: string[];
  revision: number;
  compact?: boolean;
  disabled?: boolean;
  onSelect: (model: Bag, indices: Bag) => Promise<void>;
  onIndex: (id: string | null, hash?: string) => void;
  onNoRetrieval: () => void;
  onCompare?: (models: Bag[]) => Promise<void>;
  onPreview?: (model: Bag) => Promise<void>;
  onManage: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
  switchPolicy: string;
  onSwitchPolicy: (policy: string) => void;
  preferences?: Bag;
  onPreferences?: (changes:Bag)=>void;
};

/** One immutable selection surface reused by project and voice-part bindings. */
export function ModelSelector(props: SelectionProps) {
  const { value, revision } = props;
  const prefix = useId();
  const lastBound = useRef<string | undefined>(undefined);
  const [voices, setVoices] = useState<Bag[]>([]),
    [runs, setRuns] = useState<Bag[]>([]),
    [versions, setVersions] = useState<Bag[]>([]),
    [matches, setMatches] = useState<Bag[]>([]),
    [current, setCurrent] = useState<Bag | null>(null),
    [indices, setIndices] = useState<Bag>({ indices: [] });
  const [voiceId, setVoiceId] = useState(""),
    [runId, setRunId] = useState(""),
    [query, setQuery] = useState(props.preferences?.query || ""),
    [filter, setFilter] = useState(props.preferences?.filter || "all"),
    [expanded, setExpanded] = useState(props.preferences?.expanded ?? !props.compact),
    [loading, setLoading] = useState(false),
    [choosing, setChoosing] = useState(false),
    [error, setError] = useState(""),
    [checked, setChecked] = useState<string[]>([]),
    [totalVersions, setTotalVersions] = useState(0);
  const lastRun=useRef('');
  useEffect(()=>{setQuery(props.preferences?.query||'');setFilter(props.preferences?.filter||'all');setExpanded(props.preferences?.expanded??!props.compact)},[JSON.stringify(props.preferences||{})]);
  useEffect(() => {
    let live = true;
    setLoading(true);
    const timer = setTimeout(
      () => {
        Promise.all([
          request<Bag>("/catalog/voices?limit=250"),
          request<Bag>(
            "/catalog/models?kind=inference_export&limit=250" +
              (query ? `&q=${encodeURIComponent(query)}` : ""),
          ),
        ])
          .then(([v, m]) => {
            if (live) {
              setVoices(v.voices || []);
              setMatches(m.models || []);
              setError("");
            }
          })
          .catch((e) => {
            if (live) setError(message(e));
          })
          .finally(() => {
            if (live) setLoading(false);
          });
      },
      query ? 250 : 0,
    );
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [query, revision]);
  useEffect(() => {
    let live = true;
    const changed = lastBound.current !== value.model_id;
    if (changed) {
      setCurrent(null);
      setIndices({ indices: [] });
    }
    if (!value.model_id) {
      lastBound.current = value.model_id;
      if (changed) {
        setVoiceId("");
        setRunId("");
      }
      return;
    }
    request<Bag>(`/catalog/models/${encodeURIComponent(value.model_id)}`)
      .then((m) => {
        if (live) {
          setCurrent(m);
          if (changed) {
            lastBound.current = value.model_id;
            setVoiceId(m.voice_id || "");
            setRunId(m.run_id || "");
          }
        }
      })
      .catch((e) => {
        if (live) setError(message(e));
      });
    request<Bag>(
      `/catalog/models/${encodeURIComponent(value.model_id)}/indices`,
    )
      .then((i) => {
        if (live) setIndices(i);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [value.model_id, revision]);
  useEffect(() => {
    let live = true;
    setRuns([]);
    if (!voiceId) return;
    request<Bag>(
      `/catalog/runs?voice_id=${encodeURIComponent(voiceId)}&limit=200`,
    )
      .then((r) => {
        if (live) setRuns(r.runs || []);
      })
      .catch((e) => {
        if (live) setError(message(e));
      });
    return () => {
      live = false;
    };
  }, [voiceId, revision]);
  useEffect(() => {
    let live = true;
    if(lastRun.current!==runId){setVersions([]);setChecked([]);lastRun.current=runId;}
    if (!runId) return;
    request<Bag>(
      `/catalog/models?run_id=${encodeURIComponent(runId)}&kind=inference_export&limit=200`,
    )
      .then((r) => {
        if (live) {
          setVersions(r.models || []);
          setTotalVersions(r.total || 0);
        }
      })
      .catch((e) => {
        if (live) setError(message(e));
      });
    return () => {
      live = false;
    };
  }, [runId, revision]);
  const selectedVoice = voices.find((v) => v.id === voiceId);
  const bound = current || props.fallback;
  const matchVoiceIds = new Set(matches.map((m) => m.voice_id));
  const usedVoiceIds = new Set(
    matches
      .filter((m) => props.usedModelIds.includes(m.id))
      .map((m) => m.voice_id),
  );
  const filteredVoices = voices
    .filter(
      (v) =>
        (!query ||
          matchVoiceIds.has(v.id) ||
          `${v.alias || ""} ${v.name} ${(v.tags || []).join(" ")}`
            .toLowerCase()
            .includes(query.toLowerCase())) &&
        (filter !== "favorite" || v.favorite) &&
        (filter !== "recent" || v.last_used_at) &&
        (filter !== "used" || usedVoiceIds.has(v.id)),
    )
    .sort((a, b) =>
      filter === "recent"
        ? String(b.last_used_at || "").localeCompare(
            String(a.last_used_at || ""),
          )
        : Number(!!b.favorite) - Number(!!a.favorite),
    );
  const sorted = useMemo(
    () =>
      [...versions].sort((a, b) =>
        a.epoch != null && b.epoch != null
          ? a.epoch - b.epoch
          : a.global_step != null && b.global_step != null
            ? a.global_step - b.global_step
            : String(a.version_label || a.name).localeCompare(
                String(b.version_label || b.name),
              ),
      ),
    [versions],
  );
  const selectedIndex = indices.indices?.find(
    (i: Bag) =>
      i.id ===
      (value.index_id === undefined
        ? indices.selected_index_id
        : value.index_id),
  );
  const listBoundId = current?.model_artifact_id || value.model_id;
  const selectedPosition = sorted.findIndex(
    (m) =>
      m.id === listBoundId ||
      m.model_artifact_id === listBoundId ||
      m.id === value.model_id,
  );
  const choose = async (id: string,preview=false) => {
    if (!id) return;
    setChoosing(true);
    setError("");
    try {
      const validated = await post<Bag>(
        `/catalog/models/${encodeURIComponent(id)}/validate`,
      );
      if (validated.status !== "ready")
        throw new Error(
          validated.reason || "此版本未通过安全兼容校验，不能推理。",
        );
      const compatible = await request<Bag>(
        `/catalog/models/${encodeURIComponent(validated.id)}/indices`,
      );
      await props.onSelect(validated, compatible);
      if(preview)await props.onPreview?.(validated);
      props.onChanged();
    } catch (e) {
      setError(message(e));
      props.onError(message(e));
    } finally {
      setChoosing(false);
    }
  };
  const compare = async () => {
    setChoosing(true);
    try {
      const resolved: Bag[] = [];
      for (const id of checked) {
        const m = await post<Bag>(
          `/catalog/models/${encodeURIComponent(id)}/validate`,
        );
        if (m.status !== "ready")
          throw new Error(`${m.name}: ${m.reason || "版本不可推理"}`);
        if (Number(value.index_rate || 0) > 0) {
          const ix = await request<Bag>(
            `/catalog/models/${encodeURIComponent(id)}/indices`,
          );
          const boundIndex = ix.indices?.find(
            (i: Bag) =>
              i.id ===
              (value.index_id === undefined
                ? ix.selected_index_id
                : value.index_id),
          );
          if (!boundIndex || boundIndex.compatibility?.status !== "verified")
            throw new Error(
              `${versionLabel(m)} 的索引尚未验证；选择共同兼容索引，或明确设检索率为0。`,
            );
        }
        resolved.push(m);
      }
      await props.onCompare?.(resolved);
      props.onChanged();
    } catch (e) {
      setError(message(e));
      props.onError(message(e));
    } finally {
      setChoosing(false);
    }
  };
  const favorite = async () => {
    if (!selectedVoice) return;
    try {
      await patch(`/catalog/voices/${encodeURIComponent(selectedVoice.id)}`, {
        favorite: !selectedVoice.favorite,
      });
      props.onChanged();
    } catch (e) {
      props.onError(message(e));
    }
  };
  return (
    <section
      className={`model-selector ${props.compact ? "compact" : ""}`}
      aria-label={props.label || "声音与训练版本选择"}
      data-model-selector
    >
      <div className="selector-current">
        <div>
          <strong>{props.label || "声音与训练版本"}</strong>
          <span>
            {bound
              ? `${bound.name} · ${versionLabel(current)}`
              : "尚未绑定声音模型"}
          </span>
          {bound && (
            <small>
              固定版本 · {String(bound.model_hash || "待校验").slice(0, 12)}
              {current?.status
                ? ` · ${statusNames[current.status] || current.status}`
                : ""}
            </small>
          )}
        </div>
        {props.compact && (
          <button
            type="button"
            className="quiet-button"
            aria-expanded={expanded}
            onClick={() => {setExpanded(!expanded);props.onPreferences?.({expanded:!expanded});}}
          >
            {expanded ? "收起" : "选择 / 更换"}
          </button>
        )}
      </div>
      {error && (
        <div className="warning-inline">
          {error}
          <button className="text-button" onClick={props.onManage}>
            打开模型管理
          </button>
        </div>
      )}
      {expanded && (
        <>
          <Field label="搜索声音、实验、Epoch 或哈希">
            <input
              value={query}
              onChange={(e) => {setQuery(e.target.value);props.onPreferences?.({query:e.target.value});}}
              placeholder="例如 Twilight、200 或哈希短码"
              disabled={props.disabled}
            />
          </Field>
          {query.trim() && !loading && (
            <div role="list" aria-label="匹配的模型版本">
              {[...matches].sort((a,b)=>Number(b.run_id===runId)-Number(a.run_id===runId)).slice(0,8).map(m=><button key={m.id} className="button small full" style={{whiteSpace:'normal',textAlign:'left'}} title={m.name} disabled={choosing||props.disabled||m.status==='missing'||m.status==='quarantined'} onClick={()=>void choose(m.id)}>{m.name} · {versionLabel(m)}</button>)}
              {matches.length>8 && <small>还有 {matches.length-8} 项匹配，可在模型管理中分页查看。</small>}
              {!matches.length && !loading && <small>没有匹配的模型版本；可更改搜索词或刷新目录。</small>}
            </div>
          )}
          <div className="selector-filters" role="group" aria-label="声音筛选">
            {[
              ["all", "全部声音"],
              ["recent", "最近使用"],
              ["favorite", "收藏"],
              ["used", "本工程已用"],
            ].map(([id, name]) => (
              <button
                type="button"
                key={id}
                className={`chip ${filter === id ? "active" : ""}`}
                aria-pressed={filter === id}
                onClick={() => {setFilter(id);props.onPreferences?.({filter:id});}}
              >
                {name}
              </button>
            ))}
          </div>
          <Field label={`${props.label || "目标"} · 声音`}>
            <select
              id={`${prefix}-voice`}
              value={voiceId}
              disabled={choosing || props.disabled}
              onChange={(e) => {
                setVoiceId(e.target.value);
                setRunId("");
              }}
            >
              <option value="">先选择声音身份</option>
              {!filteredVoices.some((v) => v.id === voiceId) &&
                selectedVoice && (
                  <option value={voiceId}>
                    {selectedVoice.alias || selectedVoice.name} · 当前浏览
                  </option>
                )}
              {filteredVoices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.favorite ? "★ " : ""}
                  {v.alias || v.name} · {v.version_count ?? 0} 版本
                </option>
              ))}
            </select>
          </Field>
          {voiceId && (
            <>
              <div className="row wrap selector-actions">
                <button className="text-button" onClick={() => void favorite()}>
                  {selectedVoice?.favorite ? "取消收藏" : "收藏此声音"}
                </button>
                {selectedVoice?.default_model_id &&
                  selectedVoice.default_model_id !== value.model_id && (
                    <button
                      className="text-button"
                      disabled={choosing}
                      onClick={() =>
                        void choose(selectedVoice.default_model_id)
                      }
                    >
                      使用此声音的默认版本
                    </button>
                  )}
              </div>
              <Field label="训练实验">
                <select
                  value={runId}
                  onChange={(e) => setRunId(e.target.value)}
                  disabled={choosing || props.disabled}
                >
                  <option value="">选择一个训练实验</option>
                  {runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name} ·{" "}
                      {r.sample_rate
                        ? r.sample_rate / 1000 + " kHz"
                        : "采样率待校验"}
                    </option>
                  ))}
                </select>
              </Field>
            </>
          )}
          {runId && (
            <>
              <Field
                label="固定模型版本"
                hint="按训练顺序浏览，不表示轮次越大声音越好。选择后会校验实际权重，不会改变原始文件。"
              >
                <select
                  value={
                    selectedPosition >= 0 ? sorted[selectedPosition].id : ""
                  }
                  onChange={(e) => void choose(e.target.value)}
                  disabled={choosing || props.disabled}
                >
                  <option value="">选择一个实际推理导出</option>
                  {sorted.map((m) => (
                    <option
                      key={m.id}
                      value={m.id}
                      disabled={[
                        "missing",
                        "quarantined",
                        "training_only",
                      ].includes(m.status)}
                    >
                      {versionLabel(m)} · {statusNames[m.status] || m.status}
                      {m.epoch_source === "filename_inferred"
                        ? " · 编号待核验"
                        : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <div className="row wrap">
                <button
                  className="quiet-button"
                  disabled={choosing || selectedPosition <= 0}
                  onClick={() => void choose(sorted[selectedPosition - 1].id)}
                >
                  ← 上一版本
                </button>
                <button
                  className="quiet-button"
                  disabled={
                    choosing ||
                    selectedPosition < 0 ||
                    selectedPosition >= sorted.length - 1
                  }
                  onClick={() => void choose(sorted[selectedPosition + 1].id)}
                >
                  下一版本 →
                </button>
                <button
                  className="text-button"
                  disabled={!current || choosing}
                  onClick={() =>
                    void post(
                      `/catalog/voices/${encodeURIComponent(current!.voice_id)}/default`,
                      { model_id: current!.id },
                    )
                      .then(props.onChanged)
                      .catch((e) => props.onError(message(e)))
                  }
                >
                  设为声音默认
                </button>
              </div>
              {totalVersions > versions.length && (
                <button
                  className="text-button space-top"
                  onClick={() =>
                    void request<Bag>(
                      `/catalog/models?run_id=${encodeURIComponent(runId)}&kind=inference_export&offset=${versions.length}&limit=200`,
                    )
                      .then((r) => setVersions((v) => [...v, ...r.models]))
                      .catch((e) => props.onError(message(e)))
                  }
                >
                  加载其余版本（{versions.length}/{totalVersions}）
                </button>
              )}
            </>
          )}
          {choosing && (
            <div className="busy-banner" role="status">
              <span className="spinner" />
              正在安全校验所选版本与索引…
            </div>
          )}
          {current && (
            <div className="selector-metadata">
              <Badge tone={current.status === "ready" ? "blue" : "orange"}>
                {statusNames[current.status] || current.status}
              </Badge>
              <span>{current.architecture || "架构待校验"}</span>
              <span>{current.embedder_model || "编码器以训练配置为准"}</span>
              <small>
                编号来源：
                {current.epoch_source === "checkpoint"
                  ? "检查点元数据"
                  : current.epoch_source === "filename_inferred"
                    ? "文件名推测"
                    : "未知"}
                ；
                {evidenceNames[current.metadata_evidence] ||
                  current.metadata_evidence ||
                  "元数据待核验"}
                ；声音分组：{evidenceNames[current.grouping_evidence]||current.grouping_evidence||'目录推测 / 未确认'}
              </small>
            </div>
          )}
          {value.model_id && (
            <Field
              label="检索索引"
              hint="只有经验证匹配的索引可用于检索；检索率不是音色相似度分数。"
            >
              <select
                value={
                  value.index_id === undefined
                    ? indices.selected_index_id || ""
                    : value.index_id || ""
                }
                disabled={choosing}
                onChange={(e) => {
                  const ix = indices.indices?.find(
                    (i: Bag) => i.id === e.target.value,
                  );
                  props.onIndex(
                    e.target.value || null,
                    ix?.sha256 || ix?.index_hash,
                  );
                }}
              >
                <option value="">明确不使用索引</option>
                {(indices.indices || []).map((i: Bag) => (
                  <option
                    key={i.id}
                    value={i.id}
                    disabled={i.compatibility?.status !== "verified"}
                  >
                    {i.name} ·{" "}
                    {i.compatibility?.status === "verified"
                      ? "已验证匹配"
                      : i.compatibility?.status === "incompatible"
                        ? "不兼容"
                        : "未验证"}
                  </option>
                ))}
              </select>
              <small>
                {selectedIndex
                  ? `${selectedIndex.compatibility?.status === "verified" ? "匹配已验证" : "不可用于检索"} · ${(selectedIndex.compatibility?.reasons || []).join("；")}`
                  : "未选择已验证索引。"}
              </small>
              {Number(value.index_rate || 0) > 0 && !selectedIndex && (
                <div className="warning-inline">
                  当前检索率大于0，需要匹配索引。
                  <button
                    type="button"
                    className="text-button"
                    onClick={props.onNoRetrieval}
                  >
                    明确设为不使用检索（0）
                  </button>
                </div>
              )}
            </Field>
          )}
          {!!indices.indices?.some((i:Bag)=>i.compatibility?.status!=='verified')&&<details className="index-diagnostics"><summary>索引兼容性与重新绑定</summary>{indices.indices.filter((i:Bag)=>i.compatibility?.status!=='verified').map((i:Bag)=><div key={i.id} className="index-diagnostic"><strong>{i.name}</strong><p>{(i.compatibility?.reasons||[]).join('；')||'需要核验来源与特征维度。'}</p><button className="quiet-button" disabled={choosing||i.compatibility?.status==='incompatible'} onClick={()=>{setChoosing(true);void post<Bag>(`/catalog/models/${encodeURIComponent(value.model_id)}/bind-index`,{index_id:i.id}).then(async()=>{const next=await request<Bag>(`/catalog/models/${encodeURIComponent(value.model_id)}/indices`);setIndices(next);const index=next.indices?.find((x:Bag)=>x.id===i.id);if(index?.compatibility?.status!=='verified')throw new Error('索引尚未通过匹配校验。');props.onIndex(i.id,index.index_hash||index.sha256);props.onChanged()}).catch(e=>props.onError(message(e))).finally(()=>setChoosing(false))}}>校验并绑定此索引</button></div>)}</details>}
          <Field label="切换版本时的参数策略">
            <select
              value={props.switchPolicy}
              onChange={(e) => props.onSwitchPolicy(e.target.value)}
            >
              <option value="keep_compatible">
                保留用户参数；重新核对模型相关项
              </option>
              <option value="reset_defaults">使用新模型默认参数</option>
            </select>
            <small>手工 F0、音符与旧候选保留；模型绑定变化可撤销。</small>
          </Field>
          {props.onCompare && runId && (
            <details className="version-compare">
              <summary>比较同一实验的多个版本</summary>
              <p className="info-inline">
                勾选2–8个实际导出，使用同一输入、同一F0与完整输入；按队列顺序生成。
              </p>
              <div className="version-checks">
                {sorted.map((m) => (
                  <div className="version-check-row" key={m.id}><label>
                    <input
                      type="checkbox"
                      checked={checked.includes(m.id)}
                      disabled={
                        choosing ||
                        ["missing", "quarantined", "training_only"].includes(
                          m.status,
                        ) ||
                        (!checked.includes(m.id) && checked.length >= 8)
                      }
                      onChange={(e) =>
                        setChecked((v) =>
                          e.target.checked
                            ? [...v, m.id]
                            : v.filter((id) => id !== m.id),
                        )
                      }
                    />
                    <span>
                      {versionLabel(m)}
                      <small>{statusNames[m.status] || m.status}</small>
                    </span>
                  </label>{props.onPreview&&<button className="text-button" disabled={choosing||['missing','quarantined','training_only'].includes(m.status)} onClick={()=>void choose(m.id,true)}>切换并试听</button>}</div>
                ))}
              </div>
              <button
                type="button"
                className="button"
                disabled={choosing || checked.length < 2 || !props.onCompare}
                onClick={() => void compare()}
              >
                <Icon name="branch" />
                比较所选 {checked.length} 个版本
              </button>
            </details>
          )}
          <div className="row wrap space-top">
            <button
              type="button"
              className="text-button"
              onClick={props.onManage}
            >
              扫描 / 批量登记 / 修复模型
            </button>
            {loading && <small className="muted">读取目录…</small>}
          </div>
        </>
      )}
    </section>
  );
}

export function CatalogManager({
  onChanged,
  onError,
  onSelect,
}: {
  onChanged: () => void;
  onError: (m: string) => void;
  onSelect: (m: Bag) => Promise<void>;
}) {
  const [query, setQuery] = useState(""),
    [status, setStatus] = useState(""),
    [rows, setRows] = useState<Bag[]>([]),
    [offset, setOffset] = useState(0),
    [total, setTotal] = useState(0),
    [refresh, setRefresh] = useState(0),
    [scan, setScan] = useState<Bag | null>(null),
    [paths, setPaths] = useState(""),
    [roots, setRoots] = useState(""),
    [busy, setBusy] = useState(false),
    [relink, setRelink] = useState(""),
    [selected, setSelected] = useState<Bag | null>(null),
    [notice, setNotice] = useState("");
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      request<Bag>(
        `/catalog/models?kind=&offset=${offset}&limit=30${query ? "&q=" + encodeURIComponent(query) : ""}${status ? "&status=" + encodeURIComponent(status) : ""}`,
      )
        .then((r) => {
          if (live) {
            setRows(r.models || []);
            setTotal(r.total || 0);
          }
        })
        .catch((e) => onError(message(e)));
    }, 180);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [query, status, offset, refresh]);
  useEffect(() => {
    if (!scan?.id || !["queued", "running", "preparing"].includes(scan.state))
      return;
    let live = true;
    const timer = setInterval(() => {
      request<Bag>(`/catalog/scans/${scan.id}`)
        .then((s) => {
          if (live) {
            setScan(s);
            if (!["queued", "running", "preparing"].includes(s.state)) {
              setRefresh((v) => v + 1);
              onChanged();
            }
          }
        })
        .catch((e) => onError(message(e)));
    }, 1000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [scan?.id, scan?.state]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      setRefresh((v) => v + 1);
      onChanged();
    } catch (e) {
      onError(message(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="catalog-manager">
      <p className="info-inline">
        只读扫描授权目录，推理导出、训练检查点和索引分别登记。原始文件不移动、不修改；未知结构不会自动执行。
      </p>
      <div className="field-grid">
        <Field label="搜索目录与版本">
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOffset(0);
            }}
            placeholder="声音 / 实验 / Epoch / 文件 / 哈希"
          />
        </Field>
        <Field label="资源状态">
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">全部状态与训练资源</option>
            <option value="ready">已校验推理导出</option>
            <option value="discovered">待校验</option>
            <option value="missing">文件缺失</option>
            <option value="quarantined">失败 / 隔离</option>
            <option value="training_only">训练检查点</option>
          </select>
        </Field>
      </div>
      <details className="catalog-scan" open={!total}>
        <summary>发现已有版本 / 批量登记</summary>
        <Field
          label="扫描目录（可选，每行一个）"
          hint="留空使用本项目配置的授权模型目录；不会扫描整块磁盘。"
        >
          <textarea
            rows={2}
            value={roots}
            onChange={(e) => setRoots(e.target.value)}
            placeholder="E:\voice\aponyvoice\resources\models\haysay"
          />
        </Field>
        <button
          className="button"
          disabled={
            busy ||
            (!!scan && ["queued", "running", "preparing"].includes(scan.state))
          }
          onClick={() =>
            void act(async () => {
              const s = await post<Bag>("/catalog/scan", {
                ...(roots.trim()
                  ? {
                      roots: roots
                        .split(/\r?\n/)
                        .map((x) => x.trim())
                        .filter(Boolean),
                    }
                  : {}),
                max_files: 5000,
                max_seconds: 30,
              });
              setScan(s);
              setNotice("扫描已提交，只更新 SONORA 目录记录。");
            })
          }
        >
          扫描已授权模型目录
        </button>
        {scan && (
          <div className="scan-status" role="status">
            {scan.state} · 已检查 {scan.scanned_files ?? 0} 个文件{" "}
            {scan.counts && JSON.stringify(scan.counts)}
            {scan.error && (
              <p className="error-text">
                {String(scan.error.message || scan.error)}
              </p>
            )}
            {["queued", "running", "preparing"].includes(scan.state) && (
              <button
                className="text-button"
                onClick={() =>
                  void act(() => post(`/catalog/scans/${scan.id}/cancel`))
                }
              >
                取消扫描
              </button>
            )}
          </div>
        )}
        <Field label="批量登记文件路径（每行一个）">
          <textarea
            rows={3}
            value={paths}
            onChange={(e) => setPaths(e.target.value)}
            placeholder="粘贴已有推理导出或索引的完整路径"
          />
        </Field>
        <button
          className="button ghost"
          disabled={!paths.trim() || busy}
          onClick={() =>
            void act(async () => {
              const result = await post<Bag>("/catalog/import-batch", {
                paths: paths
                  .split(/\r?\n/)
                  .map((s) => s.trim())
                  .filter(Boolean),
              });
              setNotice(
                `批量登记已返回：${JSON.stringify(result.summary || result.counts || { items: result.models?.length || result.results?.length || 0 })}`,
              );
            })
          }
        >
          批量登记与检查
        </button>
      </details>
      {notice && (
        <div className="highlight-strip" role="status">
          {notice}
        </div>
      )}
      <div className="models-list catalog-list">
        {rows.map((m) => (
          <div className="model-card" key={m.id}>
            <div className="row">
              <div className="model-text">
                <strong>{m.name}</strong>
                <small>
                  {m.kind === "training_checkpoint"
                    ? "训练检查点 · 不能直接推理"
                    : versionLabel(m)}{" "}
                  · {statusNames[m.status] || m.status}
                </small>
              </div>
              <button
                className="quiet-button"
                onClick={() => {
                  setSelected(m);
                  setRelink("");
                }}
              >
                详情
              </button>
              {m.kind === "inference_export" && (
                <button
                  className="button small"
                  disabled={
                    busy || ["missing", "quarantined"].includes(m.status)
                  }
                  onClick={() =>
                    void act(async () => {
                      const v = await post<Bag>(
                        `/catalog/models/${encodeURIComponent(m.id)}/validate`,
                      );
                      if (v.status !== "ready")
                        throw new Error(v.reason || "版本校验失败");
                      await onSelect(v);
                    })
                  }
                >
                  {m.status === "ready" ? "使用版本" : "校验并使用"}
                </button>
              )}
            </div>
            {m.reason && <p className="info-inline">{m.reason}</p>}
            <div className="model-meta">
              <span>
                {m.metadata_evidence
                  ? evidenceNames[m.metadata_evidence] || m.metadata_evidence
                  : "未知结构"}
              </span>
              <span>
                {m.model_hash
                  ? `SHA-256 ${m.model_hash.slice(0, 16)}…`
                  : "尚未计算权重哈希"}
              </span>
            </div>
          </div>
        ))}
      </div>
      <div className="row pagination">
        <button
          className="quiet-button"
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 30))}
        >
          上一页
        </button>
        <span>
          {total ? offset + 1 : 0}–{Math.min(offset + rows.length, total)} /{" "}
          {total}
        </span>
        <button
          className="quiet-button"
          disabled={offset + rows.length >= total}
          onClick={() => setOffset(offset + 30)}
        >
          下一页
        </button>
        <button
          className="text-button"
          onClick={() => setRefresh((r) => r + 1)}
        >
          刷新状态
        </button>
      </div>
      {selected && (
        <section className="catalog-detail">
          <div className="flex-between">
            <h3>{selected.name}</h3>
            <button className="text-button" onClick={() => setSelected(null)}>
              收起详情
            </button>
          </div>
          <p className="info-inline">
            {selected.kind} · {statusNames[selected.status] || selected.status}
            <br />
            Epoch：{selected.epoch ?? "未知"}（{selected.epoch_source || "未知"}
            ）；Step：{selected.global_step ?? "未知"}（
            {selected.step_source || "未知"}）<br />
            分组来源：{selected.grouping_evidence || "未知"}
            <br />
            {selected.path || selected.origin_path || ""}
          </p>
          {selected.kind === "training_checkpoint" && (
            <div className="warning-inline">
              这是继续训练的资源，不是已确认的推理导出。需要受支持的可信导出工具另存推理副本；本界面不会执行未知
              pickle 或改写原件。
            </div>
          )}
          <Field label="重新链接匹配文件">
            <input
              value={relink}
              onChange={(e) => setRelink(e.target.value)}
              placeholder="选择原哈希匹配文件的完整路径"
            />
          </Field>
          <button
            className="button"
            disabled={!relink || busy}
            onClick={() =>
              void act(async () => {
                const m = await post<Bag>(
                  `/catalog/models/${encodeURIComponent(selected.id)}/relink`,
                  { path: relink },
                );
                setSelected(m);
                setNotice("已检查重新链接结果；原模型身份保留。");
              })
            }
          >
            检查并重新链接
          </button>
        </section>
      )}
    </div>
  );
}

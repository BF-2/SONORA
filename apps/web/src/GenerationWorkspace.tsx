import type { ReactNode } from 'react';
import { Badge, Card, Slider } from './components';
import type { Bag } from './types';

const sequence = [['input', '1 · 选择输入'], ['edit', '2 · 编辑输入'], ['model', '3 · 选择模型'],
  ['parameters', '4 · 设置参数'], ['check', '5 · Generate / Check']];

export function GenerationWorkspace({ stage, onStage, input, versions, model, parameters, candidates,
  values, onValues, onPitchEditor, onGenerate, ready, busy, hasSource, hasModel, hasPitch }: {
  stage: string; onStage: (stage: string) => void; input: ReactNode; versions: ReactNode;
  model: ReactNode; parameters: ReactNode; candidates: ReactNode; values: Bag;
  onValues: (changes: Bag) => void; onPitchEditor: () => void; onGenerate: () => void;
  ready: boolean; busy: boolean; hasSource: boolean; hasModel: boolean; hasPitch: boolean;
}) {
  const current = Math.max(0, sequence.findIndex(([id]) => id === stage));
  return <div className="stack" aria-label="单轨处理工作区">
    <div className="step-tabs">{sequence.map(([id, title], i) => <button key={id}
      className={id === stage ? 'active' : ''} disabled={busy || (i > 0 && !hasSource) || (i > 2 && !hasModel)}
      onClick={() => onStage(id)}>{title}</button>)}</div>
    {stage === 'input' && <>{input}{versions}</>}
    {stage === 'edit' && <>{versions}<Card title="输入编辑" icon="note" aside={<Badge>派生处理 · 原音频 Lock</Badge>}>
      <div className="card-pad">
        <Slider label="输入音高整体移调" value={values.transpose ?? 0} min={-36} max={36} step={1} suffix="半音"
          onChange={v => onValues({ transpose: v })} hint="推荐从 0 开始；只修改当前 F0 编辑版本，保持输入时间轴。"/>
        <Slider label="音高曲线平滑半径" value={values.filter_radius ?? 0} min={0} max={7} step={1}
          onChange={v => onValues({ filter_radius: v })} hint="推荐 0 保留颤音；只在连续有声区平滑，不跨越静音。"/>
        <button className="button" onClick={onPitchEditor}>打开曲线编辑器 · 提取 / 绘制 / 音符 / 新词导唱</button>
        <p className="info-inline">{hasPitch ? '当前输入已有独立 F0。曲线修改自动保存到所选版本。' : '请先提取当前输入的 F0，再选择模型生成。'}</p>
      </div>
    </Card></>}
    {stage === 'model' && <Card title="声音与具体 Epoch / checkpoint" icon="box"><div className="card-pad">{model}</div></Card>}
    {stage === 'parameters' && <Card title="RVC 参数 · 默认值 / 推荐设置 / 高级能力" icon="sliders"><div className="card-pad">{parameters}</div></Card>}
    {stage === 'check' && <>
      <Card title="Generate → Check" icon="spark"><div className="card-pad">
        <button className="button primary" disabled={!ready || busy} onClick={onGenerate}>Generate · 生成当前单轨候选</button>
        <p className="info-inline">使用当前输入、F0 版本与 checkpoint。候选等待人工试听；接受加入 Final List，重试返回输入编辑，丢弃只移除该候选。</p>
        {!hasPitch && <p role="status">当前输入尚无有效 F0，请先返回音高提取。</p>}
      </div></Card>{candidates}
    </>}
    {current < sequence.length - 1 && <button className="button primary" disabled={busy || !hasSource || (current === 2 && !hasModel)}
      onClick={() => onStage(sequence[current + 1][0])}>继续 · {sequence[current + 1][1]}</button>}
  </div>;
}

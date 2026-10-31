# 本机音频引擎与复用记录

2026-10-31专项增量：模型选择现由SQLite模型目录管理，保留旧29份注册身份并发现全部既有Epoch导出；`resources.json`保留兼容种子与引擎配置。RVC实际参数、动态禁用条件、F0处理顺序和同源版本对比见 `docs/models-rvc.md`，统一实测见 `docs/acceptance.md`。原始 Applio/权重/索引/环境未升级或改写。

SONORA 的执行器实际调用本机模型：audio-separator 0.44.2 / MelBand RoFormer、独立 PitchService、Applio RVC，以及 Nishiren Gard AI DiffSinger 2.0 的 ONNX 歌声合成。运行入口是 `workers/<engine>/run.py --request <JSON> --response <JSON>`；协议见 `packages/contracts/API.md`。每个作业使用独立进程，取消由编排服务终止该进程树。普通同权限进程不构成操作系统安全沙箱。

## 资源准备

在项目目录运行 `python scripts/bootstrap_resources.py`。脚本读取上一级 `E:/voice`，使用已有 Python 环境，**不安装依赖、不下载模型、不训练、也不修改原始文件**。只在 SONORA 的 `resources/` 和 `engine-envs/` 中写入内容。

- Applio 原始 revision：`0724dd7737e8e155ed8385b420fb73fd080fcdd3`；原目录已有三个修改文件。快照保留这些现有修改，另用源码/配置/内容编码器/预测器组合哈希固定实际输入，而不是把上游 commit 当作完整本机状态。
- `resources/applio`：45 个推理必需源码、配置、许可证及预训练资源文件，通过硬链接复用。不要直接编辑这些链接。需要修改上游文件时，先将目标链接替换成项目私有副本。
- `resources/models`：每个已有模型目录最新的一份推理 checkpoint 及索引，通过硬链接复用；当前共 29 个 checkpoint。所有 29 个通过静态不安全 pickle globals 检查及 `weights_only=True` 元数据探测。其余 checkpoint 原封不动保留，可按需导入。
- `resources/separation`：两组已有 RoFormer 权重和配套 YAML，通过硬链接复用。默认 `mel_band_roformer_vocals_fv4_gabox` 已执行技术信号测试；`melband_roformer_big_beta6x` 只登记，未实测。
- `resources/svs/nishiren-diffsinger-v2`：完整复用现有 OpenUtau 声库的 109 个文件，包含声学模型、声码器、音素字典、时长/音高/表现预测器和说话人 embedding。ONNX 文件不转换、不修改。执行器使用已锁定的 audio-separator Python 环境中的 ONNX Runtime，仅以 CPU 后端运行，渲染进程独立。
- `resources/tools`：优先固定系统 PATH 中 FFmpeg/ffprobe 的解析后文件。本机是 9.0 full build；跨 C:/E: 卷时复制。没有选用 Applio 目录中较旧的 4.3 工具。
- Python 环境分别复用 `E:/voice/Applio/.venv` 和 `E:/voice/audio-separator-venv`，不向其安装内容。包版本保存在 `engine-envs/locked-environments.json`；这是一份已安装环境清单，并非可离线重建环境的全部 wheel 仓库。
- PitchService 新建项目专用 `engine-envs/pitch/.venv`，通过 `.pth` 引用现有 RVC site-packages，只在这个独立 overlay 中安装 SHA-256 固定的 `pyworld==0.3.5`。大依赖继续只读复用；原环境不安装、不升级。显式安装入口是 `python scripts/bootstrap_resources.py --install-pitch-overlay`；普通准备和首次播放不会隐式联网安装。

`engine-envs/resources.json` 是本地路径注册表；`resource-links.json` 记录资源原路径、链接方式与哈希；`media-tools.json` 记录 FFmpeg 编译选项和 SHA-256。跨机器要重新运行准备脚本，不能假定旧绝对路径仍有效。

## 各执行器的实际行为

分离器只接受解码后的本地 WAV/FLAC/AIFF。它跳过上游在线模型目录，直接使用登记的权重和配置，渲染时没有自动下载。配置由 SafeLoader 读取，只为已知 `python/tuple` 数据结构增加纯数据构造器，不启用任意 Python 对象标签。明确输出 Vocals→人声、Instrumental→伴奏；不把这些结果称为主唱与和声分别分离。中间 stem 为 float WAV，保留立体声及源时长。模型能力目前声明整节点重算，尚无经过验证的任意局部无缝缓存保证。

后续增加了独立的 `lead_backing` 任务和专用Karaoke权重，输入为已经提取的混合人声，输出主唱估计和背景残余；它与上段通用任务的模型、语义和能力分开登记。两种分离都直接取得神经估计，再生成互补残余，恢复共享推理增益，避免上游对每条分轨独立归一化改变原尺度。详情见 `docs/harmony-engine.md`。

PitchService 与 RVC 独立：输出 16 kHz、160 样本 hop、中心对齐的曲线，未知音高用 null，另存 voiced mask。已接入 RMVPE、FCPE、CREPE full/tiny、pYIN、Harvest、DIO+StoneMask。后两个 CPU 算法通过项目专用 pyworld overlay 运行，输入统一重采样到 16 kHz，未把 8 kHz 输入直接交给 WORLD。RMVPE 原生分数为 salience，CREPE 为 periodicity，pYIN 为 voiced probability；WORLD 没有同类原生分数，明确记 unavailable，不混为音准正确率。额外静音 RMS 门控与估计器阈值分开。

Pitch JSON 的 `f0_hz` 是原始检测，`target_layer` 是按作业编辑计算的目标预览；RVC 始终从原始检测应用一次编辑。可编辑区间以 48000 ticks/s 定位，支持区间半音/音分移调、指定 Hz/音符音高和清音。支持带 `snapshot: {sample_rate,hop,frame_center_offset,f0_hz}` 的绝对曲线锁定，锁定快照在其他修改之后应用，重新分析或全局移调不能覆盖它。

RVC 执行器只加载经过 `weights_only=True` 限制的 F0 条件模型与本地 ContentVec，禁用自动下载。检索率大于零但索引缺失、维度不符或哈希改变均报错。模型权重哈希变化也报错，不默默使用新权重。默认每次作业使用外部目标 F0；未提供曲线时先调用独立 PitchService，再把结果作为条件输入。

外部 F0 直接替换当前 Pipeline 实例的 `get_f0`，不会在内部重新检测覆盖目标。记录 `requested_f0_hash`、`consumed_f0_hash`、有声区间 log-F0 插值和反射上下文变换，以及每次实际 `generator.infer` 收到的条件张量哈希。请求与消费哈希因为 padding 和网格变换可以不同。ContentVec 的边缘特征少约 20 ms，因此末尾追加 320 个 16 kHz 反射上下文样本后再裁取有效输出；不拿尾部补零掩盖被截断的末字。实际端到端声学延迟未实测，结果不能称为经过回环验证的零延迟。

RVC 支持独立短预览：`range_ticks: [start,end]` 采用工程时间，导唱轨可指定 `source_timeline_offset_ticks`。选区前后各取至少一秒上下文，实际转换后裁取有效选区；产物记录原始 `source_offset`、`source_sample_rate`、`timeline_offset_ticks` 与上下文范围。音高编辑先在原工程时间中解析，再映射到片段上下文。此模式明确标记 `contextual_excerpt_not_full_equivalence`，不宣称与整段转换逐样本一致，也不冒充无缝局部缓存。

## 已完成验证

设备：NVIDIA GeForce RTX 4070 Ti SUPER；Python 3.12.13；PyTorch 2.11.0+cu128。记录在 `engine-envs/verification.json`，原始请求、响应、日志和音频在忽略提交的 `tests/results/workers/`。

| 测试 | 结果与范围 |
| --- | --- |
| RMVPE → RVC + 外部 F0，3 秒谐波元音 | Twilight Sparkle r18m 模型，219.22 Hz → 438.73 Hz，+1201.16 cents；生成器实际消费哈希变化，证明曲线真实生效；追加上下文后的原始输出恰好 120000 帧，无尾部补零 |
| RoFormer 分离，13.4 秒原创合成短曲 | 生成 44.1 kHz、双声道、590940 帧人声及伴奏；两条均来自模型推理，时长匹配 |
| FCPE CPU | 3 秒技术信号，约 2.94 秒完成 |
| CREPE-tiny CPU | 3 秒技术信号，约 2.64 秒完成 |
| pYIN CPU | 3 秒技术信号，约 6.55 秒完成 |
| Harvest / DIO+StoneMask CPU | pyworld 0.3.5，3 秒技术信号分别约 2.68 / 2.44 秒；稳态中位数约 220.00 / 220.01 Hz |
| RVC 上下文短预览与导唱偏移 | 0.9–1.5 秒选区导出恰好 24000 帧 / 40 kHz；导唱偏移两秒后正确落在 2.9–3.5 秒，全局音高编辑生效，目标升八度测得 438.64 Hz |
| 7 项单元/安全测试 | 清浊音不跨界插值、ticks 局部编辑、锁定快照、音符覆盖保留清音、未知控制拒绝、恶意 pickle 不执行、显式不安全加载也被限制 |
| DiffSinger 实际歌词/音符合成 | 日语「きらきら」「はるかな」使用相同 C4/C4/G4/G4 乐谱合成，两份均为 44.1 kHz、132300 帧；不同音素条件产生实际不同音频，CPU 各约 2.8–2.9 秒 |
| DiffSinger 输出音高复核 | 在四个已指定音符的稳态区间，RMVPE 测得音符中位数距目标约 0.35–0.81 cents；这是有参考音符的技术测量，不是音质评分 |
| 资源完整性复核 | 218 个资源，216 个硬链接与 2 个跨卷副本；原始路径及复用路径 SHA-256 全部与准备时记录一致 |

这些是信号与工程链路验收，**不是真人歌声音质验收**。其他 RVC checkpoint 的生产 API 运行记录以主项目验收文档为准；不能从一个模型的技术信号测试推出所有模型音质。CREPE full、CPU RVC、第二分离器、长混响与跨块听感、多模型接缝都需要独立验证。真实歌声测试及最终听感选择应保留来源和试听区间。

运行技术测试：`E:/voice/Applio/.venv/Scripts/python.exe tests/test_workers_real.py --run-real`。该命令按顺序执行 GPU 作业，运行前避免和其他重 GPU 作业并行。单元/安全测试：同一个 Python 执行 `-m unittest discover -s tests -p test_workers_unit.py -v`。

## DiffSinger 合成接口与边界

SVS 作业无需输入人声文件，实际从新歌词、音素和音符生成新演唱。请求 `parameters` 使用 `svs_model_id: "nishiren-diffsinger-v2"`、`language: "ja"`、`duration_ticks`、`timing_policy: "locked_window"`、`steps`（5–30）以及 `notes`。每个音符有 `start_tick`、`end_tick`、`pitch_midi_float`、`lyric`；可通过 `phonemes: ["ja/k", "ja/i"]` 覆盖发音。前端当前只验证日语假名，不能把声库声明的跨语言能力等同于已集成全部语言前端。

时长预测器为音素提出时长分布，执行器在每个音符窗口内分配整数帧，并保留静音间隙和全曲总长。音素过多、最短时长不足、重叠音符或未知音素会报出具体冲突。表现预测器输出 breathiness/voicing/tension，再传入真实声学模型和配套声码器。输入乐谱直接决定 F0；没有把其他未接入的表现旋钮显示为可用。

相邻音符的 `lyric: "+"` 或 `slur: true` 表示延音：合并音素段，保持新的音符音高，避免复制歌词重发辅音。当前只提供固定窗口短句（0.1–60 秒），不宣称执行全轨 ripple 编辑。ONNX 模型自带随机算子，固定 seed 用于固定本机运行条件，不承诺跨设备逐样本复现。

条件文件 `synthesis-conditions.json` 保存实际歌词、音素、分配帧数、音符、种子和说话人；产物记录组合声库 hash 与条件 hash。验证见 `engine-envs/svs-verification.json`；运行 `E:/voice/audio-separator-venv/Scripts/python.exe tests/test_workers_svs.py --run-real` 重验真实链路，省略参数则运行轻量契约测试。

## 来源与许可

Applio 与 audio-separator 的代码许可证原文保存在 `engine-envs/licenses/`。Applio 源码许可为 MIT；它不自动覆盖角色权重、ContentVec、RMVPE/FCPE 或输入素材。现有本地权重的单独再分发许可尚未确认，因此注册表明确写 `unconfirmed`，发布安装包不携带这些资源。

FFmpeg 9.0 的完整构建选项与 GPL 标志已记录，不能把整个组合应用笼统标为 MIT。环境清单还含 PyTorch、Transformers、FAISS、NumPy、SciPy 等依赖；正式分发需要逐项保留适用版权和源码提供义务。当前交付以复用用户已经拥有的本地引擎环境运行。

Nishiren Gard 声库使用作者自定义条款，发布音频要求标注声库名，商业用途需作者个别批准；本项目不分发其权重。现有完整声库保持未修改，以类似歌声合成程序直接运行 ONNX。原文：[v2.0 Terms of Service](https://github.com/Gardanana/Nishiren-AI-Diffsinger/blob/v2.0/terms-of-service.md)。接口核对参考固定版本 [OpenUtau DiffSingerRenderer](https://github.com/openutau/OpenUtau/blob/0.1.569-beta/OpenUtau.Core/DiffSinger/DiffSingerRenderer.cs) 及 [DiffSingerVariance](https://github.com/openutau/OpenUtau/blob/0.1.569-beta/OpenUtau.Core/DiffSinger/DiffSingerVariance.cs)，具体张量形状以本机 ONNX graph 检查为准。

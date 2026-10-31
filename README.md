# 声序 SONORA

Windows 优先的本地音频制作应用。以用户提供的 `reference/sonora-design-kit/codex-build-prompt.txt` 为完整需求，以 UI 源码、设计报告和架构/流程图为参考实施。前端为 React/TypeScript，桌面壳为 Tauri 2，编排为 FastAPI + SQLite，模型在独立子进程中运行。

当前已实际运行声音转换、歌曲分离后翻唱、独立音高编辑、日语歌词与音符合成、片段拼选、混音和导出；这不是原型中的模拟任务。完整需求中的高级功能仍按 `docs/acceptance.md` 区分已验证、部分支持和未接入，不能把本版本当作所有 23 节都已完整验收。

新增：混合人声可再分离为主唱/和声，分别绑定模型、F0、编辑与候选，也可单独保留原声或静音。已用用户指定《BREAK FROM THE LINE》1:29–1:55 片段完成两级分离、两种模型分别转换、混音与分轨导出。操作与验证见 `docs/harmony-usage.md`。

## 在本机启动

双击 **`启动声序.bat`**。它优先启动交互与混音修复版 `apps/desktop/target/release/sonora-recovery.exe`，不存在时依次回退 `sonora-specialist.exe`、`sonora-harmony.exe`、`sonora.exe`。桌面端管理本地 Python 服务和当前会话凭据。关闭桌面应用会回收自己启动的服务与 worker；不会结束其他工作台。本轮按用户要求用网页实际操作代替 Windows 桌面点击验收；EXE 已统一构建，请关闭旧版后再启动新版。

本项目监听 `127.0.0.1:8765`。如果端口被占用，会明确报错；请关闭另一个 SONORA 会话后再打开。已有 `E:/voice` 旧工作台的端口和配置没有修改。

需要浏览器模式时，从 PowerShell 运行：

```powershell
Set-Location E:\voice\aponyvoice
.\scripts\start-sonora.ps1 -Browser
```

保留该终端直到制作结束。浏览器地址中的临时凭据由页面读取后移除，不写进音频配方。

## 制作一条作品

1. **导入诊断**：先确认语音变声 / 歌曲翻唱 / 改词翻唱，再明确本次素材是原曲、干人声、导唱或独立分轨，然后导入。workflow 动作条和底部继续按钮依据实际前置条件更新；导航只切换页面，处理任务通过明确的执行按钮启动。
2. **人声整理**：歌曲先用通用 MelBand RoFormer 分离混合人声/伴奏，再按需要用专用 Karaoke 模型分离主唱/和声；干人声可直接进入第二步。已有外部分轨可明确导入为主唱或和声。
3. **音高与歌词**：运行独立音高检测，框选并修改半音/Hz/清音；锁定会保存绝对目标曲线，后续重检测不会覆盖。手工音符会形成实际 F0 目标，MIDI 可导入和导出。歌词保留明确时间窗口和发音字段。
4. **声音转换**：先选输入，再按声音→训练实验→Epoch/Step选定具体版本和已验证索引。参数面板分基础、高级、诊断，显示实际支持范围和推荐依据。可搜索/收藏、切换版本或勾选同实验多个版本，对同一8–15秒选区及F0生成候选并对齐A/B。采纳试听设置后生成完整版本，或把短试听局部采用到已有完整版；工程、轨道、片段覆盖保留具体模型哈希。外部F0不被RVC内部重检覆盖，短段与整曲边缘不保证逐样本一致。详见 `docs/models-rvc.md`。
5. **混音**：修改推子、声像、静音/独奏、偏移或效果后，自动用FFmpeg重新渲染当前选区，底部试听与电平表使用真实效果后音频。更新期间暂停旧预览，完成后点击播放；可独听单轨的推子/效果后结果。监听缓存不进入工程编辑历史，完整输出仍需显式「渲染混音」。默认不叠加原始整曲，导入与上游分离会使旧混音失效。
6. **导出**：WAV / FLAC / MP3、选区、分轨、制作配方。WAV可选16/24位PCM或32位浮点。选择输出时即测量该源或选区的LUFS与真峰值，导出后另存实际编码测量。文件名冲突自动另存版本；没有真实模型血缘的音频不能作为模型转换成品导出。

现为「快速完成 / 专家设置」两档；旧工程的精细编辑设置兼容显示为专家模式。切阶段、缩放、播放头和信息密度不占用撤销历史，切工程恢复各自的有效播放位置。空格播放暂停，I/O标入出点，A/B同位置切换，Ctrl+Z / Ctrl+Shift+Z撤销重做。A/B音频复用下载缓存，等响按共同选区实测RMS计算；循环只使用试听源与选区的有效交集，并在回卷边缘短暂淡出淡入。

检测参数改变后，声音转换页的「选择输入与音高」提供「按当前参数重新检测」「清除旧绑定并重新试听」；任务队列中的旧分析错误提供「重新检测并重试」。手工编辑和绝对锁定区保留。候选卡片可「移除试听片段」，在「已移除片段与候选」中恢复或撤销；正在采用或混音的候选需先取消使用，原音频与血缘不物理删除。

## 改词路线

- **新导唱**：导入或在页面主动录制唱着新词的音频，设置时间偏移和目标音高，再用已有 RVC 模型转换并混回原伴奏。VC 不会从文本生成新歌词。
- **实际 SVS**：已有 Nishiren Gard AI DiffSinger 2.0 声库已接入原生 ONNX 时长、表现、声学模型和声码器。当前已验证 **日语假名 / 已支持的日语音素** 与手工乐谱，单句 0.1–60 秒、固定窗口。可以在无原音频的工程里编写音符与歌词后合成。其他语言不伪装为可用，请用新导唱路线。
- 锁定窗口不延长后续时间轴。当前未开放全轨 ripple 时间伸缩。日语合成的字词可懂度、自然度和实际成品听感仍需用户试听。

## 资源与模型

专项注册表保留旧29个模型ID，并可有界发现授权目录中的全部推理版本。本机已发现180份RVC推理导出、29份索引和10份训练checkpoint；G/D训练文件不会当作可推理模型。首次需要更多版本时，在「声音与模型」展开「发现已有版本 / 批量登记」，执行「扫描已授权模型目录」。选中版本才做受限加载与哈希校验，180份已发现不代表全部经过真实音频或音质评价。既有两组通用分离模型、一组主唱/和声专用模型和一套DiffSinger声库继续使用。

`resources/` 优先用 NTFS 硬链接复用 `E:/voice` 既有文件。216 个资源硬链接、两个 FFmpeg 工具跨卷复制；准备后的完整性复核覆盖 218 个文件，原件与复用件哈希一致。**不要直接修改这些链接**。修改上游代码需要先断开目标链接，创建本项目私有副本。执行器与适配器代码放在 `workers/`，不是修改原始程序包。

本次和声功能额外下载约 913 MB 专用 checkpoint 及配套配置，仅保存在本项目资源目录，固定来源与 SHA-256。显式准备命令为 `.venv\Scripts\python.exe scripts\prepare_harmony_model.py --download`；渲染时不会下载。权重许可未确认，未随桌面壳打包再分发。

RVC 导入支持已授权目录内的 `.pth` 路径，先进行静态 pickle globals 检查和受限权重读取。没有自动 `weights_only=False` 降级。导入模型不会把同目录任意索引绑定给它；检索率非零需要正确索引。未知模型架构、任意压缩包或未知序列化文件会明确拒绝。

模型权重许可与代码许可分别记录。现有角色权重标“未确认”，未放入可再分发安装包；Nishiren 声库要求署名，商用需按声库许可取得作者许可。这里提供的是本机使用的项目，不能据此推断可打包再分发所有现有模型。详见 `docs/engines.md`、`docs/licenses.md`。

## 工程与文件位置

| 路径 | 内容 |
| --- | --- |
| `apps/web` | React 编辑器，锁定 npm 依赖 |
| `apps/desktop` | Tauri 桌面源代码、Cargo.lock、Windows EXE |
| `services/orchestrator` | API、工程事务、任务队列、缓存与 DSP |
| `workers` | 分离、独立音高、RVC、DiffSinger ONNX 执行器 |
| `packages/contracts` | API v1、JSON Schema、由 schema 生成的 TypeScript 类型 |
| `engine-envs` | 引擎版本、资源与许可证清单、验证证据、独立 Pitch overlay |
| `runtime/sonora.sqlite` | 工程、编辑历史、作业与事件序列 |
| `runtime/cache` | 内容哈希寻址的媒体、波形与 F0 |
| `runtime/exports` | 实际导出，已存在文件不覆盖 |
| `runtime/jobs` | 请求快照、worker 日志、条件和失败证据 |
| `tests/results` | 本机测试音频与原始证据；不提交私有素材 |
| `reference` | 用户提供的完整需求、原型、报告、架构和流程图 |

保存配方不默认携带媒体或模型。恢复配方按哈希关联已有缓存，缺失媒体保留待重连信息；后端重连接口可恢复轨道增益、偏移及候选引用。单独找回音频但缺少原推理记录时，只恢复为可播放素材，不伪造推理血缘。未知未来 schema 版本拒绝覆盖。

## 开发与重新构建

本机核心 `.venv` 已准备。重新准备时使用 Python 3.12 + 项目专用环境，避免向系统或原始 Applio 环境安装包：

```powershell
uv venv --python 3.12 .venv
uv pip sync --python .venv\Scripts\python.exe requirements.lock.txt
.venv\Scripts\python.exe scripts\bootstrap_resources.py
# 只有需要重新安装 WORLD overlay 时显式执行：
.venv\Scripts\python.exe scripts\bootstrap_resources.py --install-pitch-overlay
npm --prefix apps/web ci
.\scripts\build-desktop.ps1
```

模型与大型依赖默认使用已有 `E:/voice` 安装。引擎清单记录精确已安装版本，但不是所有大型依赖的离线 wheel 仓库；迁移机器需要准备相应环境和自有模型。启动/首次播放不会自动下载模型。

```powershell
.venv\Scripts\python.exe -m pytest tests/test_core.py tests/test_domain.py tests/test_review_regressions.py -q
E:\voice\Applio\.venv\Scripts\python.exe -m unittest discover -s tests -p test_workers_unit.py -v
.venv\Scripts\python.exe scripts\generate_contracts.py
```

实际模型测试较耗资源，按 `docs/engines.md` 单独运行。当前验证结果和限制见 `docs/acceptance.md`。构建与分发记录见 `docs/desktop.md`；本轮未制作包含未知许可权重的独立安装包。

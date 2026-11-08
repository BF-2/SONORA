# 声序 SONORA


标准流程已接入现有经典/柔和双 UI：导入与信息 → 两级分离 → 独立 F0 编辑版本 → 单轨生成 → 人工 Check → Final List → 混音 → 导出。Background、Main、Follower 为 Lock 源轨；生成不再自动采用，重试复用分离结果，同一输入可保留多条已验收成品。技术说明与 Mermaid 图见 [标准流程](docs/standard-workflow.md)。

混音页新增可调「人声厚度修复 + C 空间」方案，支持 C / C+ / C++、效果排序与旁路。新建房间混响使用立体声密集尾声，旧工程可明确升级并保留原版兼容。参见 [混音空间效果](docs/mix-vocal-space.md)。

编辑器现支持切页回顶部、拖动擦除 F0 和按无音高区域静音转换。浅色弹窗已统一。二级分离新增双 Karaoke 模型错位融合与立体声保护，见 [算法及实测](docs/harmony-ensemble.md)。

Windows 优先的本地音频制作应用。以用户提供的 `reference/sonora-design-kit/codex-build-prompt.txt` 为完整需求，以 UI 源码、设计报告和架构/流程图为参考实施。前端为 React/TypeScript，桌面壳为 Tauri 2，编排为 FastAPI + SQLite，模型在独立子进程中运行。

当前已实际运行声音转换、歌曲分离后翻唱、独立音高编辑、日语歌词与音符合成、混音和导出；这不是原型中的模拟任务。完整需求中的高级功能仍按 `docs/acceptance.md` 区分已验证、部分支持和未接入，不能把本版本当作所有 23 节都已完整验收。

新增：混合人声可再分离为主唱/和声，分别绑定模型、F0、编辑与候选，也可单独保留原声或静音。已用用户指定《BREAK FROM THE LINE》1:29–1:55 片段完成两级分离、两种模型分别转换、混音与分轨导出。操作与验证见 `docs/harmony-usage.md`。

音高编辑现支持时间轴缩放/平移、连续绘制、整笔撤销、精确目标预览和直接 A/B 试听；分离页新增 Gabox Karaoke V2 与窗口/步进/三次增强推理参数；标准流程保留全部候选，接受、重试和丢弃分别记录。使用说明、调研来源与实测范围见 [编辑与分离改进](docs/editor-improvements.md)。这些更新已构建到浏览器版，已运行的服务请正常关闭后重新启动；旧桌面 EXE 本轮未重新构建。

## 在本机启动

停止程序：双击根目录的 **`停止声序.bat`**。脚本只停止本项目的服务/桌面进程及其子进程，保留工程、模型、缓存和导出文件。有排队或正在运行的音频任务时默认不停止；需要中断时运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop-sonora.ps1 -Force`。加 `-WhatIf` 可以只查看目标。浏览器启动器保存使用 Windows 当前用户加密的停止会话，优先平滑关闭；旧版/桌面会话兼容读取本地任务状态后停止已核实的进程。

双击 **`启动声序.bat`**，默认启动浏览器版：开启本地服务并在默认浏览器打开同一套制作界面。请保留启动终端，结束后在终端按回车关闭本次服务。`启动声序-浏览器.bat`是同模式的独立入口。

本项目监听 `127.0.0.1:8765`。如果端口被占用，会明确报错；请关闭另一个 SONORA 会话后再打开。已有 `E:/voice` 旧工作台的端口和配置没有修改。

需要原来的桌面壳时，使用 **`启动声序-桌面.bat`**；它仍优先选择 `apps/desktop/target/release/sonora-recovery.exe`。默认入口不再尝试启动该EXE，不更改Windows安全设置。

需要浏览器模式时，从 PowerShell 运行：

```powershell
Set-Location E:\voice\aponyvoice
.\scripts\start-sonora.ps1 -Browser
```

保留该终端直到制作结束。浏览器地址中的临时凭据由页面读取后移除，不写进音频配方。

## 制作一条作品

1. **导入与信息**：选择语音变声、普通翻唱或改词翻唱，确认素材角色，读取时长、采样率、声道与哈希。
2. **两级分离**：一级产生 Background / Voice，二级将 Voice 分为 Main / Follower，保存成套结果和缓存。源轨 Lock 保护原始文件，允许预览与派生处理。干声可明确跳过分离；二级缺失或失败会明确报告。
3. **音高与歌词**：分别提取各源 F0，使用 Edit 1、Edit 2、Edit 3 等独立版本。保留曲线绘制、音符、MIDI、新导唱及日语 SVS，支持保存、切换、撤销；版本编辑不改原音。
4. **生成与验收**：按选择输入 → 编辑输入 → 选择声音/checkpoint → 设置 RVC 参数 → Generate/Check 操作。生成只创建待验收候选。A/B 试听后接受加入 Final List；重试返回编辑且不重分离；丢弃不影响已有成品。
5. **成品与混音**：Final List 支持同源多版本、独立试听、名称、删除、音量、声像、静音及时间偏移。只混入一条 Background 与启用的成品；48 kHz 立体声、时间轴对齐、-1 dBFS 总线限幅。提供选区监听与全长混音预览。
6. **导出**：沿用 WAV / FLAC / MP3、位深、采样率、分轨、配方及响度测量；同名文件另存版本。项目保存源轨、F0 版本、生成配置、候选、成品和作业进度；失败或中断不删除已完成结果。

现为「快速完成 / 专家设置」两档；旧工程的精细编辑设置兼容显示为专家模式。切阶段、缩放、播放头和信息密度不占用撤销历史，切工程恢复各自的有效播放位置。空格播放暂停，I/O标入出点，A/B同位置切换，Ctrl+Z / Ctrl+Shift+Z撤销重做。A/B音频复用下载缓存，等响按共同选区实测RMS计算；循环只使用试听源与选区的有效交集，并在回卷边缘短暂淡出淡入。

检测参数改变后，声音转换页的「选择输入与音高」提供「按当前参数重新检测」「清除旧绑定并重新试听」；任务队列中的旧分析错误提供「重新检测并重试」。手工编辑和绝对锁定区保留。候选卡片可「移除试听片段」，在「已移除片段与候选」中恢复或撤销；正在采用或混音的候选需先取消使用，原音频与血缘不物理删除。

## 改词路线

- **新导唱**：导入或在页面主动录制唱着新词的音频，设置时间偏移和目标音高，再用已有 RVC 模型转换并混回原伴奏。VC 不会从文本生成新歌词。
- **实际 SVS**：已有 Nishiren Gard AI DiffSinger 2.0 声库已接入原生 ONNX 时长、表现、声学模型和声码器。当前已验证 **日语假名 / 已支持的日语音素** 与手工乐谱，单句 0.1–60 秒、固定窗口。可以在无原音频的工程里编写音符与歌词后合成。其他语言不伪装为可用，请用新导唱路线。
- 锁定窗口不延长后续时间轴。当前未开放全轨 ripple 时间伸缩。日语合成的字词可懂度、自然度和实际成品听感仍需用户试听。

## 资源与模型

角色声库仅登记 Hay Say 的 M6 六套 RVC 权重及各自索引，模型位于 `resources/models/haysay/`。默认扫描和资源准备均使用专用清单，不再读取旧 Applio 训练声库。每套模型固定来源 revision、权重/索引 SHA-256 和本地验证的 speaker 0；Fluttershy 为 40kHz，其余五角色为 48kHz。通用分离、音高、ContentVec 与 SVS 资源继续作为工作流依赖保留。模型和索引未打入可分发安装包。详见 [Hay Say 接入说明](docs/models-rvc.md)。

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

标准流程统一检查：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-standard.ps1`。真实引擎验收：`.venv\Scripts\python.exe scripts/verify_standard_workflow.py`；浏览器验收：`python scripts/verify_standard_browser.py`（系统 Python 已安装 Playwright）。真实流程证据保存在 `runtime/standard-workflow-acceptance/`，听感验收仍需用户试听。

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

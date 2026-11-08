# 标准音频制作流程

本轮在原 React/TypeScript、FastAPI/SQLite、audio-separator、独立 Pitch、RVC 和 FFmpeg 上整合；保留经典/柔和主题、参数系统、模型目录、歌词/导唱/SVS、时间轴和媒体缓存。没有新增第二套编辑器。

正式路径为：导入与信息 → 两级分离 → 各源轨 F0 版本 → 单轨选择 → 输入编辑 → 模型/checkpoint → 参数 → Generate → Check → Final List → 多轨混音 → 导出。界面仍使用六个制作阶段，生成阶段内部按五步排列。流程图见 [workflow.mmd](workflow.mmd)。

## 数据约束

| 状态 | 实现与语义 |
| --- | --- |
| 源音频 | `assets[].locked`、`tracks[].locked` 标记原音保护；不是静音，也不是禁止派生处理。应用不原位改写源文件。轨道更新不能改换 artifact、role、candidate 或锁定身份；媒体读取检查 SHA-256。 |
| 一级分离 | `vocal` 对应 Voice，`instrumental` 对应 Background；成套登记在原 `separation_runs`。 |
| 二级分离 | `lead_vocal` 对应 Main，`backing_vocal` 对应 Follower。继续使用专用模型与显式输出映射，缺失或不完整输出直接失败。 |
| 分离缓存 | 沿用内容哈希、模型/config 哈希、执行器代码及分离参数缓存。RVC 更换模型和重试不提交分离作业；需要重分离时明确点击分离按钮。 |
| F0 版本 | 工程级 `pitch_versions` 保存稳定 ID、source ID、名称、revision、检测绑定、目标参数、编辑与音符。`active_pitch_versions[source_id]` 指向各自的当前版本。默认 Edit 1，复制产生 Edit 2、3……，不限制版本数。每版最多保留 100 次本地编辑撤销快照，原有工程历史另行保留。 |
| 输入配置 | Main/Follower 沿用独立 `voice_parts`。单轨/干声源通过 `input_workspaces` 保存和恢复参数及编辑，换源时不继承另一源的曲线、模型或输入编辑。公共 device/seed/cpu_threads 保持工程级选择。 |
| 候选 | 每次生成新 candidate ID；相同内容可以复用缓存 artifact。保存 source ID/hash、checkpoint ID/hash、F0 ID/revision/快照、实际消费 F0 hash、参数快照及结果 ID。修改原版本不会改写已生成候选快照。 |
| 人工验收 | `review_status` 为 pending / accepted / retry / discarded。生成默认 pending，不自动采用。接受为 Final List 新增独立条目；重复接受同一候选幂等。重试只返回编辑，不隐式开始任务。丢弃保留底层缓存与血缘用于历史恢复，不物理删文件。 |
| 成品 | `final_list[]` 保存稳定 final ID、candidate ID、track ID、接受时间。实际音量、声像、静音、solo、偏移、效果、名称复用 `tracks`。删除条目只撤销该条混音引用，候选/源文件保留。 |
| 工程保存 | 沿用 SQLite WAL 事务、revision 乐观锁、任务快照、历史、哈希媒体和 `.sonora.json` 配方。失败、取消和重启保留完成的产物；重启时未完成任务标记 interrupted，可按当前状态重试。 |

F0 分析继续使用原来的静音门限、非有限值过滤与有声区插值；无声帧用 `null/voiced=false` 表示，不跨静音插值。源检测在源局部时间保存，目标编辑使用工程 tick（48,000 tick/s），生成前按源轨偏移转换。后台分析完成只绑定到提交任务时的版本；用户在执行中切换或修改版本时，不覆盖新编辑。

音高编辑器提供“擦除音高”工具，拖动覆盖的时间段被写为无音高。擦除可以裁开已有锁定编辑，整笔操作支持撤销和重做。每个 Edit 独立保存无音高静音开关、最小静音区间和平滑长度，关闭开关时直接保留 RVC 输出。开启时，连续无音高区达到最小长度才静音，较短间隙保留转换声音。最小区间可调 0–2000 ms，默认 0。平滑长度为每侧淡入或淡出的时长，可调 0–200 ms，默认 3，有声段较短时自动缩短。已有 Edit 默认开启，复制新版本会复制当前配置，之后独立编辑。静音参数和曲线一起参与版本保存、撤销/重做及工程配方恢复。

生成固定选定 Edit 的 `parameters.voicing_settings` 快照，并纳入音频缓存键与候选配置签名。切换开关或时间参数后重新生成，避免复用不同静音配置的音频。最小区间按完整 F0 曲线判定，选区试听沿用完整区间的判断。无音高也包括检测为无声的气声和辅音。阶段页、音高子页与生成子页切换时统一回到顶部。

二级分离新增双模型、时间错位与立体声保护算法，配置、缓存和实测记录见 [和声增强分离](harmony-ensemble.md)。浅色模式覆盖实际使用的原生 `dialog`，包括模型、设置和任务队列弹窗。

Lock 限制的是源文件及其身份，不限制预览、读取、音高派生、增益或混音对齐。外部手工篡改文件会在哈希校验时报错。

## 混音与旧工程

`mixing.mix_tracks()` 对标准工程只返回 `background_track_id` 加 `final_list[].track_id`，因此 Voice、Main/Follower 原声、待验收候选、旧混音不会重复叠入。可接受同一源的多个生成版本；只有明确接受的版本才会并行混入。

混音使用 48 kHz 立体声浮点中间文件，按整数样本处理正/负偏移，原始时间轴长度作为最短输出长度，并保留正偏移造成的延长。整段渲染与选区监听共用 DSP 路径，总线增加延迟补偿的 -1 dBFS limiter，最终导出继续测量 LUFS/true peak 并检查整数编码削波。单轨独听使用该轨推子与效果，不含总线 limiter。轨道效果尾音仍按既有规则裁到工程音频时间轴末端。

更换下一候选的模型/F0 不使已接受成品混音失效；改变 Final List、Background 或实际混音轨道设置时才要求重混。成品页提供完整渲染、选区监听和全长混音监听。

旧工程首次打开或提交作业时，在原数据上补全 `production_version=1`。源轨、音频、历史候选和导出保留；旧自动选中的候选不能证明人工验收，因此迁入 pending，历史混音标记待重确认。缺失推理血缘的配方媒体可以恢复试听，但不会恢复成已验收的模型成品。新流程的工程/配方恢复保留 Final List 和 F0 ID。

旧 `pipeline`、双声部自动生成混音、局部自动采用与旧拼选入口不再在标准流程中执行；返回明确错误要求单轨生成和 Check。音高编辑中的短选区试听保留，其结果不能直接当作全长成品接受。普通语音、单人干声翻唱直接跳过分离；改词翻唱继续使用新导唱或已有日语 SVS。

## API 增量

所有修改请求使用当前 `revision`，并返回完整新工程。

| 接口 | 请求内容 |
| --- | --- |
| `POST /projects/{pid}/pitch-versions/{action}` | `source_artifact_id`、`version_id`（新建可省略）、`name`（重命名）。action = create/select/save/undo/redo/rename。 |
| `POST /projects/{pid}/candidates/{id}/review` | `action`: accept/retry/discard。 |
| `DELETE /projects/{pid}/final-list/{id}` | `revision`。删除引用、保留音频。 |
| `POST /projects/{pid}/background` | `track_id`。选定唯一 Background。 |
| `PATCH /projects/{pid}` | 既有 `track_updates` 修改成品名称、gain_db、pan、mute、offset_ticks 和 effects；F0 编辑自动保存到对应活动版本。 |

直接写 `selected_candidate_id` 不能绕过 Check，直接替换轨道身份不能绕过 Lock，导出待验收 converted 音频会被拒绝。模型选择与高级参数接口沿用原实现。

## 验证与边界

新增 `tests/test_standard_workflow.py` 覆盖锁定、三个独立版本、撤销/重做、Main/Follower 生成条件、候选验收、重试/丢弃、多个成品、混合采样率/偏移/限幅、数据库恢复、禁止绕过验收与中断保留。使用显式 DSP fixture 检查状态，不能宣称神经推理通过。

`scripts/verify_standard_workflow.py` 在 `runtime/standard-workflow-acceptance/data` 独立数据库中使用本机真实 RoFormer、Karaoke、RMVPE、Hay Say RVC 和 FFmpeg：12 秒歌曲，两级分离，Follower 三个版本，Main/Follower 转换，预期失败后的重试，三条成品，50 ms 偏移，48 kHz 双声道 12.05 秒 WAV，以及配方/数据库恢复。证据为该目录的 `verification.json`。

`scripts/verify_standard_browser.py` 使用系统 Python 的 Playwright 和独立临时后端，在真实验收工程的副本中验证新界面；截图及 `browser-verification.json` 保存在同目录。`scripts/verify_local.py` 改为显式标准阶段，在独立 `runtime/standard-regression/data` 中回归普通翻唱、语音和改词导唱。测试脚本的接受按钮只模拟状态转换，音色听感结论留待用户试听。

已安装 Hay Say checkpoint 不一定包含可验证的训练 Epoch 数字，UI 如实显示未提供，仍固定真实 checkpoint ID/hash；不生成虚构的 epoch。Follower 的 F0 是单条主导音高曲线，现有单音高 RVC 不会自动恢复多声部同时发声的全部音符。二级分离属于模型估计，残留、串音与音色自然度不能由技术指标代替听感验收。SVS 继续限定已验证的日语声库/发音，其它语言使用导唱路线。


统一自动检查命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-standard.ps1
.venv\Scripts\python.exe scripts/verify_standard_workflow.py
python scripts/verify_standard_browser.py
.venv\Scripts\python.exe scripts/verify_local.py
.venv\Scripts\python.exe tests/test_workers_svs.py --run-real
E:\voice\Applio\.venv\Scripts\python.exe -m unittest discover -s tests -p test_rvc_parameters_unit.py -v
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1 -SkipWebBuild
```

核心测试环境的 `noisereduce` 用例会跳过，但在真实 RVC 环境单独运行的 21 个参数/目标 F0 测试中已执行通过。另一个跳过来自 Windows 当前账户没有创建符号链接权限；不修改系统权限来绕过它。

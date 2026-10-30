# 主唱与和声分组执行器

新增的是第二步 **合并人声 → 主唱组 / 背景人声组**，可分别交给已有 RVC 模型。原有歌曲 → 人声/伴奏分离保持独立。两个新声部不是说话人身份识别，也不保证把重唱中的每位歌手拆开。

## 固定资源和准备入口

模型为 aufr33 / viperx 的 MelBand RoFormer Karaoke，采用 audio-separator **0.44.2** 已安装执行层。新权重约 913 MB，下载仅写入本项目 `resources/separation/harmony/`。运行时不联网、不临时下载。

显式准备命令：`python scripts/prepare_harmony_model.py --download`。不带参数只显示资源信息；Ctrl+C 或 `--cancel-file <marker>` 可以取消，`.part` 保留供断点续传。内容长度与 SHA-256 均匹配后才原子提交并注册。中断文件不能当作可用权重。

| 资源 | 固定身份 |
| --- | --- |
| checkpoint | `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt` |
| checkpoint SHA-256 | `1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813` |
| config SHA-256 | `b35077d94861f068097cce1a5e54633c055e7dcc2613eade4e4dc7c7c9c3f48b` |
| 发布资源 | nomadkaraoke/python-audio-separator，release `model-configs`，release ID `175147634`，asset IDs `192841139` / `192850484` |
| 注册 ID | `melband-karaoke-aufr33-viperx-10.1956` |

来源依据包括固定版本的 [audio-separator 模型目录](https://github.com/nomadkaraoke/python-audio-separator/blob/v0.44.2/audio_separator/models.json)、该项目公开发布的模型及配置，以及 [MVSEP 的作者模型说明](https://mvsep.com/algorithms/76)。后者明确区分直接处理歌曲与先提取合并人声的路线。单独的权重再分发许可未确认，记录为 `unconfirmed`；不因执行器代码是 MIT 就宣称权重也为 MIT，发布包不内置这些权重。

## 角色如何确定

本次固定发布配置的 native stems 叫 `Vocals` / `Instrumental`，直接预测目标叫 `Vocals`。这些名字不足以判定它们的含义。任务注册表另外保存 `task: lead_backing`、来源依据和明确映射：

- `Vocals` → `lead_vocal`：专用 checkpoint 的主唱估计。
- `Instrumental` → `backing_vocal`：**输入合并人声减去主唱估计**。如果输入仍有乐器、混响或噪声残留，它们也可能在这个组内。

上述方向还用两条已知自然录制人声的受控混合实际核对，没有根据听起来更像哪一路而临时交换标签。普通人声/伴奏模型不能通过给文件改名就注册为和声分离器；执行器会核对任务、配置 native stems、目标与映射。

合并人声以 float PCM 进入模型。主唱/背景分组与普通人声/伴奏分离均直接调用固定版本的 `demix`，避免每条输出分别归一化破坏源重构关系。需要时统一缩小模型输入峰值，神经估计完成后恢复这项共享增益，再以原尺度输入减去目标估计得到另一组。float 中间轨允许峰值超过 0 dBFS，交给最终混音电平检查，不在每步偷偷限制峰值。

输出保留单/双声道，使用 44.1 kHz float WAV；源采样率、采样数、时间偏移和重采样映射另存。短于模型窗口的输入先增加反射上下文，再裁回原长。和声默认窗口 801 帧，audio-separator 0.44.2 的 overlap 参数实际为滑动步长秒数，本适配器记录为 `overlap_step_seconds`。

每个输出保留 model/config 哈希、native stem、`separation_task`、实际采样长度及角色。背景组虽然是减法得到，但依赖本次真实模型主唱估计，响应明确记录这一来源。没有模型输出就不能产生成功的声部分组。

## 接口

调用现有 `workers/separation/run.py`，参数包含 `separation_task: "lead_backing"`，可选 `harmony_model_id`。资源注册表在 `separation.models` 追加此模型，并设定 `separation.default_harmony_model`。完整工程/候选 API 契约见 `packages/contracts/HARMONY.md`。

`source_timeline_offset_ticks` 传递工程偏移；`source_offset_samples` 传递原样本偏移。输入本身仍是不可变的合并人声产物。输出角色 `lead_vocal` 与 `backing_vocal` 成对提交，工程中应静音原合并人声，保留原伴奏，避免三者叠加重复人声。

只改变其中一个声部的模型、音高或候选时，另一个声部的绑定与产物仍应保留。模式可选转换、保留原声或静音；分离失败不清空已有编辑。分离模型不声明任意局部渲染与整曲逐样本等价。

## 验证及其限制

实际设备为 RTX 4070 Ti SUPER / PyTorch 2.11.0+cu128。短 GPU 测试约 5.7–6.3 秒，进程峰值 CUDA 分配约 1.84 GB；这不是所有歌曲与并行负载的资源保证。CPU 也完成一秒音频、128 帧窗口的真实推理，约 7.14 秒，仅证明该路径可运行。

受控自然录制人声测试：选用用户已有的两条唱歌片段，较强的人声居中，较弱的人声分布在左右声道。四秒测试保持了 176400 帧/44.1 kHz/双声道；两输出相加的最大误差为 `7.45e-9`。对已知输入分量做样本对齐的最小二乘投影：

| 输出 | 主参考分量系数 | 背景参考分量系数 |
| --- | ---: | ---: |
| 主唱组 | 0.98389 | 0.02373 |
| 背景组 | 0.01611 | 0.97627 |

每路仍有约 0.00845 RMS 不能用两条参考的线性组合解释。这证明此受控案例中模型确实产生了有用的分组，并非单纯复制输入；系数不是通用分离准确率，受控拼合也不代替自然合唱听感验收。证据见 `engine-envs/harmony/controlled-real-projection.json`。

保留失败结果：由两份实际 DiffSinger 生成音频组成的合成混合，未得到有用分离，几乎全部落入背景组。两个自然歌曲的初选片段也主要落在主唱组，不能据此认定没有和声。相关输出、参数、日志和投影数据全部保留，没有将它们标为音质通过。若一组能量接近静音，执行器显示“需对照原人声试听”的提示，不推断歌手数量。

用户随后指定 `BREAK FROM THE LINE` 的 **1:29–1:55** 为实际重叠验收片段。只针对该片段完成两步分离，四条结果均为 26 秒、1146600 帧、44.1 kHz、立体声。RMS 分别为：合并人声 0.2410、伴奏 0.1993、主唱 0.2312、背景组 0.04972。普通两轨相加重构片段的最大误差 `5.96e-8`；主唱加背景组重构合并人声的最大误差 `2.98e-8`。处理约 7.89 / 7.62 秒，原始 MP3 及裁剪源 SHA 前后未变。数字说明信号与时间正确，不能代替对串音、歌词完整性和接缝的试听。

这次实测还发现并修复上游输出电平问题：原路径对输入和人声分别归一化，两轨相加对源最大差值约 0.1905。分别恢复人声/伴奏增益后误差接近浮点精度，证明问题来自独立电平缩放。上述新直接估计/补偿残差路径修复了这个问题，没有修改原始 audio-separator 包。前后证据在 `general-normalization-before.json`、`user-sample-before-level-fix.json` 和新版 `user-sample-verification.json`。

音频位于 `tests/results/harmony/user-89-115/`；主项目验收记录负责后续两声部独立 RVC、混音和实际试听状态。

六项契约/PCM 单元测试覆盖任务误用、角色交换、来源缺失、原任务兼容、无独立归一化的精确分解、下载取消。原始资源及用户指定 MP3 的完整性复核共 222 项，全部与原记录哈希一致，见 `engine-envs/harmony/original-integrity.json`。

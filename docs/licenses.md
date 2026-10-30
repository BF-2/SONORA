# 来源、许可与本机发布边界

本项目没有将所有组合内容统一声明为 MIT。代码、权重、内容编码器、声码器、输入音频与身份素材的许可分别记录。本轮仅构建本机应用，不发布附带私人音频或未知许可权重的安装包。

| 组件 | 锁定依据 | 本机记录 |
| --- | --- | --- |
| 原型与规格 | 用户提供的两个 ZIP | `reference/` 保留原文；未据此推断可公开再分发 |
| Applio | commit `0724dd7737e8e155ed8385b420fb73fd080fcdd3` + 实际文件组合哈希 | MIT 原文在 `engine-envs/licenses/Applio-MIT.txt`；原目录本来有局部修改，已保留并按哈希固定 |
| audio-separator | 0.44.2 | MIT 原文在 `engine-envs/licenses/audio-separator-MIT.txt` |
| 角色 RVC 与分离权重 | 每份 checkpoint/config/index 的 SHA-256 | 用户既有文件，只读复用；未确认权重再分发许可，不附安装包 |
| ContentVec、RMVPE、FCPE 等组件 | `resource-links.json` 单文件哈希 | 不能把 Applio 的代码许可自动覆盖第三方 checkpoint；本机复用，不增加商用保证 |
| Nishiren Gard AI DiffSinger 2.0 | 整套声库组件哈希 + 本地原始许可 | Gardanana，自定义许可，署名要求、商用须取得作者许可；未修改声库，不随应用再分发 |
| FFmpeg/ffprobe | 9.0 full build 文件哈希与全部 build flags | 实际启用 GPL/version3；不是纯 LGPL 构建，记录于 `engine-envs/media-tools.json` |
| Tauri/Rust 组件 | Cargo.lock | Windows 目标依赖/许可表达式在 `apps/desktop/dependencies.windows.json` |
| 前端 npm 组件 | `apps/web/package-lock.json` | 精确版本与 npm 完整性信息；实际清单在 `docs/sbom.json` |
| 核心 Python | `requirements.lock.txt` | 独立 `.venv`，实际版本/许可证元数据在 `docs/sbom.json` |
| 大型推理环境 | `engine-envs/locked-environments.json` | 复用既有环境；只读，不向旧环境安装新库 |
| WORLD overlay | `engine-envs/pitch/requirements-overlay.lock` | pyworld 0.3.5 的 wheel hash 与新 overlay 环境；不修改原 RVC 环境 |

本地真实歌曲/语音仅用于用户工作区的技术验证，测试结果保存在忽略提交的目录。可公开的技术信号由代码生成，不代表真人或特定声音权利已被授予。

模型下载、商业分发和数据/声音身份使用权是不同问题。任何对外分发都应按实际包含的文件重新核对清单；独立进程划分不自动消除相应组件的义务。

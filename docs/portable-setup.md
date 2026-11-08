# 声序便携版

解压 → 双击 **一键准备声序.bat** → 选择模型 → 双击 **启动声序.bat**。

| 选项 | 下载内容 |
| --- | --- |
| 1 · 基础模型 | ContentVec、RMVPE、FCPE、FV4 人声/伴奏分离、两套 Karaoke 主唱/和声分离 |
| 2 · M6 RVC | 六角色权重、各自配套 index，以及 ContentVec / F0 资源 |
| 3 · 全部模型 | 基础模型 + M6 RVC，约 4 GB |
| 4 · 运行环境 | 检查并补齐 Python、音频引擎、FFmpeg 和界面依赖 |

下载自动测速选择源站与镜像，失败后换源；支持断点续传、SHA-256 校验与已下载文件复用。再次运行可补充模型或修复依赖。

| 组件 | 存放位置 |
| --- | --- |
| Python 与依赖 | `.tools/`，随解压目录一起移动 |
| 基础模型、M6 和 index | `resources/` |
| 工程与导出 | `runtime/` |

自动模式为支持的 NVIDIA 显卡选择 CUDA 12.8，其余使用 CPU。发布包预置 CPU 运行环境与构建好的界面。

## 命令行

```powershell
# 全部模型；自动选择下载源与计算后端
.\scripts\prepare-sonora.ps1 -Models All -NonInteractive

# M6 + index；优先镜像；使用 CPU
.\scripts\prepare-sonora.ps1 -Models M6 -Source Mirror -TorchBackend CPU -NonInteractive

# 查看文件与下载大小
.\scripts\prepare-sonora.ps1 -Models All -Plan
```

`-Source Origin` 优先源站；`-Source Mirror` 优先镜像。两种模式均保留失败换源。

源码目录中的界面由脚本自动准备 Node.js 并构建；发布包直接使用预置界面。

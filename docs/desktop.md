# Windows 桌面运行与构建

桌面壳使用 Tauri 2，编辑器随 EXE 嵌入。Python 核心、独立模型环境与权重分别保存在本地项目内/既有资源路径，当前交付为可运行的本地开发分发，不是一个已签名、内含全部模型的安装包。

## 启动

完成根目录 README 的核心依赖与资源准备后，双击 `启动声序.bat`。已有 `apps/desktop/target/release/sonora.exe` 时启动桌面窗口，否则使用本地浏览器界面。

桌面壳先建立后台进程生命周期约束，启动 `.venv/Scripts/python.exe -m services.orchestrator.app --port 8765`，完成协议和认证握手后显示编辑器。凭据在每次启动时随机生成，仅经受限 `get_connection` IPC 交给内置主窗口。服务只监听 `127.0.0.1:8765`；已有程序占用端口时不接管或停止它。

关闭桌面应用会关闭它拥有的 Windows Job Object，从而停止这次会话的后台服务及推理子进程。未提交作业由核心下次启动恢复/标记；已提交工程和媒体仍在磁盘。不要通过关闭窗口代替正常的导出完成确认。

浏览器开发入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-sonora.ps1 -Browser
```

浏览器会话需要保留启动终端，按 Enter 结束本次服务。令牌通过 URL fragment 传递，前端读取后移除，HTTP 请求与日志不含该 fragment。不要分享包含令牌的启动地址。

## 构建

准备 Rust MSVC 工具链、Visual Studio C++ Build Tools、Windows SDK、Node.js。运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
```

构建使用 `apps/web/package-lock.json` 和 `apps/desktop/Cargo.lock`。首次构建会下载这些已锁定的开发依赖。输出为 `apps/desktop/target/release/sonora.exe`，脚本显示其 SHA-256。桌面应用启动和首次播放不会安装依赖或下载模型。

`apps/desktop/dependencies.windows.json` 记录 Windows 实际依赖图的名称、版本、许可证表达式和 Cargo.lock 哈希；构建脚本自动更新它。这是桌面依赖清单，发布时仍须随包保留第三方许可证原文，不能把模型与整个组合软件统一标作同一许可证。

当前 EXE 从自身的祖先目录寻找完整项目；也可以将 `SONORA_ROOT` 设为包含 `services/orchestrator/app.py` 的项目根目录。仅复制 EXE 不会复制 Python 与模型环境。独立安装包、代码签名及离线安装材料仍需单独准备，发布时须包含实际依赖与模型的再分发清单。

无需打开窗口的运行时检查：`apps/desktop/target/release/sonora.exe --check-runtime`。它真实启动核心、验证协议和认证，再清理自己启动的服务；输出不包含会话令牌。运行前应关闭其他声序会话。

## 生命周期与故障定位

- `runtime/logs/desktop-service.stdout.log` 与 `desktop-service.stderr.log`：桌面服务日志。
- `runtime/logs/browser-service.stdout.log` 与 `browser-service.stderr.log`：浏览器启动日志。
- 缺 `.venv` 或前端构建：按 README 准备环境后重试。
- 端口占用：关闭另一个声序会话，或自行确认占用程序；应用不会自动杀死未知进程。
- 协议不匹配：同步桌面与编排服务的版本。
- 45 秒握手超时：检查核心日志和依赖；本次启动的子进程会清理。

Tauri 权限仅开放内置主窗口的核心操作与固定连接 IPC，没有任意 shell 执行或全文件系统前端权限。CSP 禁止远端脚本和框架，仅允许本机核心、IPC 与本地媒体。

## 本机验证记录（2026-10-30）

- Windows MSVC 工具链：Rust 1.98.1；Tauri 2.12.5、tauri-build 2.7.4，版本与传递依赖写入 Cargo.lock。
- `cargo build --release --locked` 成功生成真实 Windows EXE；WebView2 156.0.4314.50 已安装。
- `--check-runtime` 真实启动 FastAPI，验证协议 1 和 Bearer 认证，返回成功后服务停止；实际重新绑定端口 8765 验证释放。
- 浏览器启动脚本以 `-Browser -NoOpenBrowser -RunForSeconds 1` 运行通过；退出后服务释放端口。模拟端口已被监听时，脚本拒绝启动且保留原监听器。
- 通过 `node_repl` 的 `@oai/sky` 打开实际桌面 EXE，读取窗口截图与无障碍树：显示“本地服务已连接”、持久化验收工程、29 个实际模型、真实候选及混音轨道。窗口后续被最小化，因此没有把后续点击导航记作通过。
- 对这次独立测试的原生进程执行强制关闭后，其 Windows Job Object 自动终止后台核心；实际验证 8765 释放。此项覆盖桌面进程意外退出时的子进程清理，不代替模型作业恢复验收。
- 当前没有验证跨电脑安装、签名安装包、macOS/Linux 桌面行为或不同 WebView2 版本。桌面验证不代表所有音频引擎功能与音质已验收；这些以引擎和集成测试记录为准。

实现参考：[Tauri Rust IPC](https://v2.tauri.app/develop/calling-rust/)、[Tauri capabilities](https://v2.tauri.app/security/capabilities/)。

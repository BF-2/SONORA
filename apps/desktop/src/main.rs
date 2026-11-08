#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rand::RngCore;
use serde::Serialize;
use std::{
    fs::{self, File},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::Manager;

const PORT: u16 = 8765;
const ENDPOINT: &str = "http://127.0.0.1:8765";

#[derive(Clone, Serialize)]
struct Connection {
    endpoint: String,
    token: String,
    protocol_version: u32,
}

struct LocalService {
    connection: Connection,
    child: Mutex<Option<Child>>,
    #[cfg(windows)]
    job: Mutex<Option<OwnedJob>>,
}

#[cfg(windows)]
struct OwnedJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for OwnedJob {}

#[cfg(windows)]
impl Drop for OwnedJob {
    fn drop(&mut self) {
        // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates only descendants assigned here.
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

impl LocalService {
    fn stop(&self) {
        #[cfg(windows)]
        if let Ok(mut job) = self.job.lock() {
            job.take();
        }
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

impl Drop for LocalService {
    fn drop(&mut self) {
        self.stop();
    }
}

#[tauri::command]
fn get_connection(
    window: tauri::WebviewWindow,
    service: tauri::State<'_, LocalService>,
) -> Result<Connection, String> {
    if window.label() != "main" {
        return Err("Only the main window may access the local session".into());
    }
    Ok(service.connection.clone())
}

fn is_workspace(path: &Path) -> bool {
    path.join("services/orchestrator/app.py").is_file()
        && path.join("packages/contracts/API.md").is_file()
}

fn workspace_root() -> Result<PathBuf, String> {
    if let Some(value) = std::env::var_os("SONORA_ROOT") {
        let root = PathBuf::from(value);
        return if is_workspace(&root) {
            fs::canonicalize(root).map_err(|e| e.to_string())
        } else {
            Err("SONORA_ROOT 没有包含 services/orchestrator/app.py 的有效应用目录。".into())
        };
    }
    if let Ok(exe) = std::env::current_exe() {
        for path in exe.ancestors().skip(1).take(8) {
            if is_workspace(path) {
                return Ok(path.to_path_buf());
            }
        }
    }
    Err("找不到声序核心运行时。请从完整项目目录启动，或将 SONORA_ROOT 设置为项目目录。".into())
}

fn local_request(path: &str, token: Option<&str>) -> Result<(u16, String), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], PORT));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(400))
        .map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;
    let authorization = token
        .map(|t| format!("Authorization: Bearer {t}\r\n"))
        .unwrap_or_default();
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n{authorization}Connection: close\r\n\r\n"
    )
    .map_err(|e| e.to_string())?;
    let mut response = Vec::new();
    stream
        .take(2 * 1024 * 1024)
        .read_to_end(&mut response)
        .map_err(|e| e.to_string())?;
    // The authenticated projects response can be larger than our startup bound.
    // Only its status is needed; cutting a UTF-8 codepoint must not break startup.
    let response = String::from_utf8_lossy(&response);
    let (head, body) = response
        .split_once("\r\n\r\n")
        .ok_or("Invalid local HTTP response")?;
    let status = head
        .lines()
        .next()
        .and_then(|x| x.split_whitespace().nth(1))
        .and_then(|x| x.parse().ok())
        .unwrap_or(0);
    Ok((status, body.to_owned()))
}

#[cfg(windows)]
fn assign_job(child: &Child) -> Result<OwnedJob, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::*;
    unsafe {
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if handle.is_null() {
            return Err(std::io::Error::last_os_error().to_string());
        }
        let job = OwnedJob(handle);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _,
            std::mem::size_of_val(&limits) as u32,
        ) == 0
        {
            return Err(std::io::Error::last_os_error().to_string());
        }
        if AssignProcessToJobObject(handle, child.as_raw_handle()) == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(job)
    }
}

fn start_service() -> Result<LocalService, String> {
    let root = workspace_root()?;
    let python = root.join(if cfg!(windows) {
        ".venv/Scripts/python.exe"
    } else {
        ".venv/bin/python"
    });
    if !python.is_file() {
        return Err("尚未准备核心 Python 环境。请先按 README 安装项目的 .venv 依赖；应用不会在启动时隐式下载。".into());
    }
    let guard = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], PORT)))
        .map_err(|_| format!("本地端口 {PORT} 已被占用。请关闭另一个声序会话，或检查占用程序后重试。未终止任何已有进程。"))?;
    let logs = root.join("runtime/logs");
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let stdout =
        File::create(logs.join("desktop-service.stdout.log")).map_err(|e| e.to_string())?;
    let stderr =
        File::create(logs.join("desktop-service.stderr.log")).map_err(|e| e.to_string())?;
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    let token: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    let connection = Connection {
        endpoint: ENDPOINT.into(),
        token: token.clone(),
        protocol_version: 1,
    };
    let mut command = Command::new(python);
    command
        .args([
            "-m",
            "services.orchestrator.app",
            "--port",
            &PORT.to_string(),
        ])
        .current_dir(&root)
        .env("SONORA_SESSION_TOKEN", &token)
        .env("SONORA_ROOT", &root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    drop(guard);
    let mut child = command
        .spawn()
        .map_err(|e| format!("启动核心服务失败：{e}"))?;
    #[cfg(windows)]
    let job = match assign_job(&child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("无法建立后台进程生命周期约束：{error}"));
        }
    };
    let service = LocalService {
        connection,
        child: Mutex::new(Some(child)),
        #[cfg(windows)]
        job: Mutex::new(Some(job)),
    };
    let started = Instant::now();
    loop {
        if let Ok(mut child) = service.child.lock() {
            if let Some(child) = child.as_mut() {
                if let Ok(Some(status)) = child.try_wait() {
                    return Err(format!("核心服务提前退出 ({status})。请查看 runtime/logs/desktop-service.stderr.log。"));
                }
            }
        }
        if let Ok((200, body)) = local_request("/api/v1/health", None) {
            if let Ok(health) = serde_json::from_str::<serde_json::Value>(&body) {
                if health["protocol_version"] != 1 {
                    return Err(
                        "核心服务协议不匹配，需要 protocol_version=1。请保持桌面与核心版本一致。"
                            .into(),
                    );
                }
                if matches!(
                    local_request("/api/v1/projects", Some(&token)),
                    Ok((200, _))
                ) {
                    break;
                }
            }
        }
        if started.elapsed() > Duration::from_secs(45) {
            return Err("核心服务未在 45 秒内完成认证握手。请查看 runtime/logs/desktop-service.stderr.log；确认依赖已安装。".into());
        }
        thread::sleep(Duration::from_millis(200));
    }
    Ok(service)
}

fn show_error(message: &str) {
    eprintln!("SONORA: {message}");
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        let title: Vec<u16> = "声序 SONORA · 启动失败\0".encode_utf16().collect();
        let text: Vec<u16> = format!("{message}\0").encode_utf16().collect();
        MessageBoxW(
            std::ptr::null_mut(),
            text.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

fn main() {
    if std::env::args().any(|argument| argument == "--check-runtime") {
        match start_service() {
            Ok(service) => {
                service.stop();
                println!("{{\"protocol_version\":1,\"authenticated\":true,\"owned_service_stopped\":true}}");
            }
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(1);
            }
        }
        return;
    }
    let result = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_connection])
        .setup(|app| {
            let service = start_service().map_err(std::io::Error::other)?;
            app.manage(service);
            if let Some(window) = app.get_webview_window("main") {
                // No remote navigation is allowed to acquire the session IPC.
                window.show()?;
            }
            Ok(())
        })
        .build(tauri::generate_context!());
    match result {
        Ok(app) => app.run(|handle, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Some(service) = handle.try_state::<LocalService>() {
                    service.stop();
                }
            }
        }),
        Err(error) => show_error(&error.to_string()),
    }
}

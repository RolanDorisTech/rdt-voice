#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use reqwest::blocking::{Client, RequestBuilder};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::TcpListener;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{
    menu::{IsMenuItem, Menu, MenuItem},
    tray::TrayIconBuilder,
    webview::WebviewWindowBuilder,
    AppHandle, Emitter, Manager, State, WebviewUrl,
};
#[cfg(target_os = "macos")]
use tauri::menu::{PredefinedMenuItem, Submenu};
use tauri_plugin_deep_link::DeepLinkExt;
use sha2::{Digest, Sha256};

// Keep OS-specific file protections behind one helper. Unix uses POSIX modes;
// Windows secures app-owned data with an explicit current-user ACL via icacls.
#[cfg(unix)]
fn set_private_permissions(path: &Path, mode: u32) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(windows)]
fn set_private_permissions(path: &Path, _mode: u32) -> std::io::Result<()> {
    let username = std::env::var("USERNAME").map_err(|error| {
        std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            format!("USERNAME is unavailable: {error}"),
        )
    })?;
    let identity = std::env::var("USERDOMAIN")
        .map(|domain| format!("{domain}\\{username}"))
        .unwrap_or(username);
    let inheritance = if path.is_dir() { "(OI)(CI)F" } else { "F" };
    let grant = format!("{identity}:{inheritance}");
    let mut acl_command = Command::new("icacls.exe");
    acl_command.creation_flags(0x08000000);
    let output = acl_command
        .arg(path)
        .arg("/inheritance:r")
        .arg("/grant:r")
        .arg(&grant)
        .output()?;
    if output.status.success() {
        Ok(())
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            format!(
                "icacls could not secure {}: {}",
                path.display(),
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        ))
    }
}

#[cfg(not(any(unix, windows)))]
fn set_private_permissions(_path: &Path, _mode: u32) -> std::io::Result<()> {
    Ok(())
}

const SIGNAL_TERM: i32 = 15;
const SIGNAL_KILL: i32 = 9;

fn application_support_dir(app_name: &str) -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        return Ok(home_dir()?.join("Library").join("Application Support").join(app_name));
    }
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| home_dir().ok().map(|home| home.join("AppData").join("Local")))
            .ok_or_else(|| "Could not determine the current user's local application-data folder.".to_string())?;
        return Ok(base.join(app_name));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| home_dir().ok().map(|home| home.join(".local").join("share")))
            .ok_or_else(|| "Could not determine the current user's application-data folder.".to_string())?;
        return Ok(base.join(app_name));
    }
}

fn application_log_dir(app_name: &str) -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        return Ok(home_dir()?.join("Library").join("Logs").join(app_name));
    }
    #[cfg(target_os = "windows")]
    {
        return Ok(application_support_dir(app_name)?.join("Logs"));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .or_else(|| home_dir().ok().map(|home| home.join(".local").join("state")))
            .ok_or_else(|| "Could not determine the current user's log folder.".to_string())?;
        return Ok(base.join(app_name));
    }
}

fn app_version_label() -> String {
    let version = env!("CARGO_PKG_VERSION");
    if let Some((before_counter, counter)) = version.rsplit_once('.') {
        if !counter.is_empty() && counter.bytes().all(|byte| byte.is_ascii_digit()) {
            if let Some((base, family)) = before_counter.rsplit_once('.') {
                let bytes = family.as_bytes();
                if bytes.len() == 2 && bytes[0].is_ascii_digit() && bytes[1].is_ascii_uppercase() {
                    return format!("v{base}");
                }
            }
        }
    }
    format!("v{version}")
}
const BUILD_ID: &str = env!("RDT_BUILD_ID");

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AppBuildInfo {
    version: String,
    build_id: String,
}

// Stop Previous Server captures a detailed, secret-free trace in memory and in
// a dedicated log so the next Diagnostics report can show OS-level evidence.
static LAST_STOP_TRACE: OnceLock<Mutex<String>> = OnceLock::new();

fn stop_trace_mutex() -> &'static Mutex<String> {
    LAST_STOP_TRACE.get_or_init(|| Mutex::new(String::new()))
}

fn stop_trace_log_path() -> Option<PathBuf> {
    application_log_dir("RDT Connect")
        .ok()
        .map(|directory| directory.join("stop-previous-server.log"))
}

fn unix_epoch_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn reset_stop_trace(message: &str) {
    let first = format!("[epoch_ms={}] {}\n", unix_epoch_millis(), message);
    if let Ok(mut trace) = stop_trace_mutex().lock() {
        *trace = first.clone();
    }
    if let Some(path) = stop_trace_log_path() {
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(path)
        {
            let _ = file.write_all(first.as_bytes());
            let _ = file.flush();
        }
    }
}

fn append_stop_trace(message: impl AsRef<str>) {
    let line = format!("[epoch_ms={}] {}\n", unix_epoch_millis(), message.as_ref());
    if let Ok(mut trace) = stop_trace_mutex().lock() {
        trace.push_str(&line);
        const TRACE_MAX_BYTES: usize = 48_000;
        if trace.len() > TRACE_MAX_BYTES {
            let discard = trace.len() - TRACE_MAX_BYTES;
            let cut = trace
                .get(discard..)
                .and_then(|tail| tail.find('\n'))
                .map(|offset| discard + offset + 1)
                .unwrap_or(discard);
            trace.drain(..cut);
        }
    }
    if let Some(path) = stop_trace_log_path() {
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
            let _ = file.write_all(line.as_bytes());
            let _ = file.flush();
        }
    }
}

fn stop_trace_snapshot() -> String {
    stop_trace_mutex()
        .lock()
        .map(|trace| trace.clone())
        .unwrap_or_else(|_| {
            "Stop trace is temporarily unavailable because its lock is poisoned.".into()
        })
}

#[cfg(unix)]
fn signal_name(signal: i32) -> &'static str {
    match signal {
        SIGNAL_TERM => "SIGTERM",
        SIGNAL_KILL => "SIGKILL",
        _ => "UNKNOWN_SIGNAL",
    }
}

#[derive(Default)]
struct BackendInner {
    child: Option<Child>,
    process_group: Option<u32>,
    port: Option<u16>,
    model: Option<String>,
    uses_https: bool,
    managed_serve_port: Option<u16>,
    // Holds an OS-level flock for as long as this manager owns a backend.
    managed_lock: Option<File>,
}

struct BackendState {
    inner: Mutex<BackendInner>,
    // Serialize lifecycle operations so concurrent Start/Stop invocations cannot orphan a process.
    lifecycle: Mutex<()>,
    model_download_status: Arc<Mutex<ModelDownloadStatus>>,
    // Exact spawned-process identity, including start time and command. PID alone is
    // not sufficient ownership evidence for signaling a process group.
    model_download_pid: Arc<Mutex<Option<ProcessSnapshot>>>,
    model_download_log_path: Arc<Mutex<Option<PathBuf>>>,
    model_download_cancel_requested: Arc<AtomicBool>,
    resource_dir: PathBuf,
    token_path: PathBuf,
    log_path: PathBuf,
    lock_path: PathBuf,
    menu_bar_pref_path: PathBuf,
    https_dir: PathBuf,
    tls_config_path: PathBuf,
    model_storage_config_path: PathBuf,
    app_language_pref_path: PathBuf,
    last_server_choice_path: PathBuf,
    menu_start_in_progress: AtomicBool,
    keep_in_menu_bar: AtomicBool,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BackendStatus {
    running: bool,
    pid: Option<u32>,
    port: Option<u16>,
    model: Option<String>,
    local_url: Option<String>,
    log_path: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ModelDownloadStatus {
    active: bool,
    model: Option<String>,
    destination_root: Option<String>,
    phase: String,
    message: String,
    progress_percent: u8,
    downloaded_bytes: u64,
    total_bytes: Option<u64>,
    planned_files: Option<usize>,
    size_is_estimate: bool,
}
impl Default for ModelDownloadStatus {
    fn default() -> Self {
        Self {
            active: false,
            model: None,
            destination_root: None,
            phase: "idle".into(),
            message: "Choose a Whisper model to check its local availability.".into(),
            progress_percent: 0,
            downloaded_bytes: 0,
            total_bytes: None,
            planned_files: None,
            size_is_estimate: false,
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ModelCacheDeleteResult {
    deleted_repositories: usize,
    locations_checked: usize,
    approximate_bytes_freed: u64,
    message: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct PlatformModelOption {
    id: String,
    name: String,
    description: String,
    size: String,
    recommended: bool,
}

fn native_ggml_spec(model: &str) -> Option<(&'static str, &'static str, &'static str)> {
    // Hashes are candidate pins from the approved immutable HF revision. The downloader
    // independently requires pinned metadata + complete bytes to match before promotion.
    const REVISION: &str = "fb5ed9369c26eaf08392504d8446da431e0fffb9";
    const MODELS: [(&str, &str, &str); 5] = [
        ("tiny", "ggml-tiny-q8_0.bin", "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca"),
        ("base", "ggml-base-q8_0.bin", "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9"),
        ("small", "ggml-small-q8_0.bin", "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f"),
        ("large-v3-turbo", "ggml-large-v3-turbo-q5_0.bin", "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"),
        ("large-v3", "ggml-large-v3-q5_0.bin", "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1"),
    ];
    let _ = REVISION;
    MODELS.iter().find(|(id, _, _)| *id == model).map(|(_, filename, sha)| (*filename, *sha, REVISION))
}

fn platform_model_ids() -> Vec<&'static str> {
    if cfg!(any(target_os = "windows", target_os = "linux")) {
        vec!["tiny", "base", "small", "large-v3-turbo", "large-v3"]
    } else {
        vec![
            "mlx-community/whisper-large-v3-turbo",
            "mlx-community/whisper-large-v3-mlx",
            "mlx-community/whisper-small-mlx",
            "mlx-community/whisper-base-mlx",
            "mlx-community/whisper-tiny-mlx",
        ]
    }
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(default, rename_all = "camelCase")]
struct LocalTlsConfig {
    // HTTPS is the secure default even when no certificate has been configured.
    mode: String,
    certificate_path: Option<String>,
    private_key_path: Option<String>,
    ca_certificate_path: Option<String>,
    generated: bool,
    // SHA-256 pins are populated for RDT-generated Windows TLS files. They allow
    // status checks to detect a replaced certificate/key before the key is used.
    windows_certificate_sha256: Option<String>,
    windows_private_key_sha256: Option<String>,
    windows_ca_sha256: Option<String>,
    // Only fingerprints explicitly imported by RDT Connect may be removed by its UI.
    managed_trust_fingerprints: Vec<String>,
}
impl Default for LocalTlsConfig {
    fn default() -> Self {
        Self {
            mode: if cfg!(any(target_os = "macos", target_os = "windows")) { "https" } else { "http" }.into(),
            certificate_path: None,
            private_key_path: None,
            ca_certificate_path: None,
            generated: false,
            windows_certificate_sha256: None,
            windows_private_key_sha256: None,
            windows_ca_sha256: None,
            managed_trust_fingerprints: Vec::new(),
        }
    }
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct LocalHttpsStatus {
    mode: String,
    configured: bool,
    trusted: bool,
    generated: bool,
    certificate_path: Option<String>,
    private_key_path: Option<String>,
    ca_certificate_path: Option<String>,
    rdt_added_trust_count: usize,
    message: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct PairingQr {
    pairing_url: String,
    qr_data_url: String,
    expires_at: f64,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BackendConnectionCheck {
    running: bool,
    pid: Option<u32>,
    port: Option<u16>,
    local_url: Option<String>,
    health_ok: bool,
    auth_ok: Option<bool>,
    auth_http_status: Option<u16>,
    message: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct OrphanedBackend {
    pid: u32,
    port: Option<u16>,
    description: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct OrphanScan {
    manager_locked_elsewhere: bool,
    instances: Vec<OrphanedBackend>,
}

fn home_dir() -> Result<PathBuf, String> {
    #[cfg(windows)]
    {
        if let Some(home) = std::env::var_os("USERPROFILE") {
            return Ok(PathBuf::from(home));
        }
        if let (Some(drive), Some(path)) = (std::env::var_os("HOMEDRIVE"), std::env::var_os("HOMEPATH")) {
            let mut combined = PathBuf::from(drive);
            combined.push(path);
            return Ok(combined);
        }
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "Could not determine the current user's home folder.".to_string())
}

fn backend_executable_name() -> &'static str {
    if cfg!(windows) { "rdt_voice_backend.exe" } else { "rdt_voice_backend" }
}

fn backend_executable_path(resource_dir: &Path) -> PathBuf {
    resource_dir.join("rdt_voice_backend").join(backend_executable_name())
}

fn resolve_resource_dir(app: &AppHandle) -> Result<PathBuf, String> {
    if let Ok(dir) = app.path().resource_dir() {
        if dir.join("rdt_voice_backend").exists() || dir.join("rdt_voice_web").exists() {
            return Ok(dir);
        }
    }
    // In `tauri dev`, resources are left in the source tree. In a packaged app,
    // the first branch points at Contents/Resources.
    let source_resources = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources");
    if source_resources.exists() {
        return Ok(source_resources);
    }
    Err("Desktop bundle resources are missing. Run the platform-specific desktop resource-preparation script before building.".into())
}

impl BackendState {
    fn new(resource_dir: PathBuf) -> Result<Self, String> {
        let app_support = application_support_dir("RDT Voice Server")?;
        fs::create_dir_all(&app_support)
            .map_err(|e| format!("Could not create the desktop application-support folder: {e}"))?;
        set_private_permissions(&app_support, 0o700)
            .map_err(|e| format!("Could not secure the desktop application-support folder: {e}"))?;
        let token_path = app_support.join("server-token");
        let lock_path = app_support.join("rdt-connect-backend.lock");
        let menu_bar_pref_path = app_support.join("show-in-menu-bar");
        let rdt_support = application_support_dir("RDT Connect")?;
        let https_dir = rdt_support.join("https");
        fs::create_dir_all(&https_dir)
            .map_err(|e| format!("Could not create the local HTTPS settings folder: {e}"))?;
        let _ = set_private_permissions(&rdt_support, 0o700);
        let _ = set_private_permissions(&https_dir, 0o700);
        let tls_config_path = rdt_support.join("local-https.json");
        let model_storage_config_path = rdt_support.join("model-storage.json");
        let app_language_pref_path = rdt_support.join("app-language");
        let last_server_choice_path = rdt_support.join("last-server-choice.json");
        let keep_in_menu_bar = fs::read_to_string(&menu_bar_pref_path)
            .map(|v| v.trim().eq_ignore_ascii_case("true"))
            .unwrap_or(true);
        // Keep existing application-support/auth/recordings paths for compatibility,
        // while giving the newly branded app its own visible log directory.
        let log_dir = application_log_dir("RDT Connect")?;
        fs::create_dir_all(&log_dir)
            .map_err(|e| format!("Could not create the RDT Connect log folder: {e}"))?;
        let log_path = log_dir.join("backend.log");
        let legacy_log = application_log_dir("RDT Voice Server")?.join("backend.log");
        if !log_path.exists() && legacy_log.is_file() {
            let _ = fs::copy(&legacy_log, &log_path);
        }
        Ok(Self {
            inner: Mutex::new(BackendInner::default()),
            lifecycle: Mutex::new(()),
            model_download_status: Arc::new(Mutex::new(ModelDownloadStatus::default())),
            model_download_pid: Arc::new(Mutex::new(None)),
            model_download_log_path: Arc::new(Mutex::new(None)),
            model_download_cancel_requested: Arc::new(AtomicBool::new(false)),
            resource_dir,
            token_path,
            log_path,
            lock_path,
            menu_bar_pref_path,
            https_dir,
            tls_config_path,
            model_storage_config_path,
            app_language_pref_path,
            last_server_choice_path,
            menu_start_in_progress: AtomicBool::new(false),
            keep_in_menu_bar: AtomicBool::new(keep_in_menu_bar),
        })
    }

    fn status(&self) -> BackendStatus {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        let child_alive = match inner.child.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(Some(_)) => false,
                Ok(None) => true,
                Err(_) => false,
            },
            None => false,
        };
        // The Python/MLX parent can exit before a spawned worker does. Treat the
        // managed process group as live until every member has gone away.
        let group_alive = inner
            .process_group
            .map(process_group_alive)
            .unwrap_or(false);
        if !child_alive && !group_alive {
            inner.child = None;
            inner.process_group = None;
            inner.port = None;
            inner.model = None;
            inner.managed_serve_port = None;
            inner.uses_https = false;
            inner.managed_lock = None;
        }
        let running = (child_alive || group_alive) && inner.port.is_some();
        BackendStatus {
            running,
            pid: inner
                .child
                .as_ref()
                .map(|child| child.id())
                .or(inner.process_group),
            port: inner.port,
            model: inner.model.clone(),
            local_url: inner.port.map(|p| {
                format!(
                    "{}://127.0.0.1:{p}",
                    if inner.uses_https { "https" } else { "http" }
                )
            }),
            log_path: self.log_path.display().to_string(),
        }
    }

    fn keep_in_menu_bar(&self) -> bool {
        self.keep_in_menu_bar.load(Ordering::SeqCst)
    }

    fn set_keep_in_menu_bar(&self, enabled: bool) -> Result<(), String> {
        fs::write(
            &self.menu_bar_pref_path,
            if enabled { "true\n" } else { "false\n" },
        )
        .map_err(|e| format!("Could not save the menu-bar preference: {e}"))?;
        self.keep_in_menu_bar.store(enabled, Ordering::SeqCst);
        Ok(())
    }

    fn manager_lock_is_held(&self) -> bool {
        use fs2::FileExt;
        let file = match OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&self.lock_path)
        {
            Ok(f) => f,
            Err(_) => return true,
        };
        if file.try_lock_exclusive().is_err() {
            return true;
        }
        let _ = FileExt::unlock(&file);
        false
    }

    fn api_credentials(&self) -> Result<(u16, String), String> {
        let port = self
            .status()
            .port
            .ok_or_else(|| "Start the local backend first.".to_string())?;
        let token = fs::read_to_string(&self.token_path)
            .map_err(|_| {
                "The backend token has not been created yet. Start the server and try again."
                    .to_string()
            })?
            .trim()
            .to_string();
        if token.len() < 32 {
            return Err("The backend token file is empty or invalid. Stop the server and inspect the token file.".into());
        }
        Ok((port, token))
    }

    fn acquire_manager_lock(&self) -> Result<File, String> {
        use fs2::FileExt;
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&self.lock_path)
            .map_err(|e| format!("Could not open RDT Connect's backend lock file: {e}"))?;
        file.try_lock_exclusive().map_err(|_| "Another RDT Connect instance is already managing a backend. No new process was started; use that instance or stop its server cleanly first.".to_string())?;
        Ok(file)
    }

    fn stop(&self) -> Result<BackendStatus, String> {
        let _lifecycle = self.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
        let (mut child, process_group, managed_serve_port, managed_lock, old_port, old_model) = {
            let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
            (
                inner.child.take(),
                inner.process_group.take(),
                inner.managed_serve_port.take(),
                inner.managed_lock.take(),
                inner.port,
                inner.model.clone(),
            )
        };
        if let Some(port) = managed_serve_port {
            disable_serve_port(port);
        }
        let terminated = if let Some(group) = process_group {
            terminate_process_group(group, child.as_mut())
        } else if let Some(child) = child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
            !process_group_alive(child.id())
        } else {
            true
        };
        if !terminated {
            // Keep ownership and the lock if any descendant is still alive. The UI
            // must not claim STOPPED or allow another Start while workers remain.
            let group = process_group;
            let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
            inner.child = child;
            inner.process_group = group;
            inner.port = old_port;
            inner.model = old_model;
            inner.managed_serve_port = None;
            inner.managed_lock = managed_lock;
            return Err(format!("Stop did not complete: managed process group {} is still alive after termination was requested. Ownership was retained; retry Stop or inspect the process details.", group.unwrap_or(0)));
        }
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.child = None;
        inner.process_group = None;
        inner.port = None;
        inner.model = None;
        inner.uses_https = false;
        drop(inner);
        // Release the process-wide lock only after the complete process group is gone.
        drop(managed_lock);
        Ok(self.status())
    }
}

#[cfg(unix)]
fn process_group_alive(group_id: u32) -> bool {
    let rc = unsafe { libc::kill(-(group_id as i32), 0) };
    if rc == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

// A zombie no longer owns a listening socket. For orphan cleanup, don't let a
// zombie awaiting reaping keep Stop stuck forever after the server has exited.

#[cfg(windows)]
fn process_group_alive(group_id: u32) -> bool {
    let snapshot = process_snapshot();
    // An empty snapshot means process enumeration failed, not that the group
    // was proven absent. Fail closed so Stop cannot report success on uncertainty.
    if snapshot.is_empty() {
        return true;
    }
    snapshot.iter().any(|item| item.pid == group_id && !item.stat.starts_with('Z'))
        || snapshot.iter().any(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
        || !windows_descendant_pids(group_id, &snapshot).is_empty()
}

#[cfg(windows)]
fn windows_descendant_pids(root_pid: u32, snapshot: &[ProcessSnapshot]) -> std::collections::HashSet<u32> {
    // ParentProcessId remains useful when the root exits first. Keep this as a
    // liveness signal only: descendants are not terminated unless their identities
    // can be revalidated, because a PID can eventually be reused by Windows.
    let mut ancestry = std::collections::HashSet::from([root_pid]);
    let mut descendants = std::collections::HashSet::new();
    loop {
        let mut added = false;
        for item in snapshot {
            if item.pid != root_pid && ancestry.contains(&item.ppid) && ancestry.insert(item.pid) {
                descendants.insert(item.pid);
                added = true;
            }
        }
        if !added {
            break;
        }
    }
    descendants
}

#[cfg(windows)]
fn send_signal_checked(target: i32, signal: i32) -> Result<(), String> {
    let is_tree = target < 0;
    let pid = target.unsigned_abs();
    if pid < 2 {
        return Err("Safety check refused to signal an invalid Windows process ID.".into());
    }

    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return Err("Could not inspect the Windows process table; no termination request was sent.".into());
    }
    let process_is_present = snapshot
        .iter()
        .any(|item| item.pid == pid && !item.stat.starts_with('Z'));
    let descendants = windows_descendant_pids(pid, &snapshot);
    let present = if is_tree {
        process_is_present
            || snapshot.iter().any(|item| item.pgid == pid && !item.stat.starts_with('Z'))
            || !descendants.is_empty()
    } else {
        process_is_present
    };
    if !present {
        return Ok(());
    }
    if is_tree {
        // taskkill /T is keyed by the root PID. If that process has exited but
        // descendants remain, using the old PID could target an unrelated process
        // after PID reuse. Refuse instead of pretending the surviving tree is gone.
        let Some(root) = snapshot.iter().find(|item| item.pid == pid && !item.stat.starts_with('Z')) else {
            if !descendants.is_empty() {
                return Err("The Windows backend leader exited while descendants remain. RDT Connect cannot safely target the old root PID; no termination request was sent.".into());
            }
            return Err("The Windows process-tree root could not be verified; no termination request was sent.".into());
        };
        if !is_windows_executable(&root.command, "rdt_voice_backend.exe")
            && !is_windows_executable(&root.command, "rdt_voice_backend")
        {
            return Err("Safety check refused to terminate a Windows process tree because the root PID no longer identifies the expected RDT backend.".into());
        }
    }

    append_stop_trace(format!(
        "WINDOWS PROCESS TERMINATION REQUEST: PID={pid}; tree={is_tree}; force={}",
        signal == SIGNAL_KILL
    ));
    let pid_text = pid.to_string();
    let mut command = Command::new("taskkill.exe");
    command.arg("/PID").arg(pid_text);
    if is_tree {
        command.arg("/T");
    }
    if signal == SIGNAL_KILL {
        command.arg("/F");
    }
    let output = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("Could not run Windows taskkill for PID {pid}: {error}"))?;
    if output.status.success() {
        return Ok(());
    }

    let after = process_snapshot();
    if after.is_empty() {
        return Err(format!(
            "Windows taskkill did not confirm termination of PID {pid}, and RDT Connect could not inspect the process table to verify the result."
        ));
    }
    let still_present = if is_tree {
        after.iter().any(|item| item.pid == pid && !item.stat.starts_with('Z'))
            || after.iter().any(|item| item.pgid == pid && !item.stat.starts_with('Z'))
            || !windows_descendant_pids(pid, &after).is_empty()
    } else {
        after.iter().any(|item| item.pid == pid && !item.stat.starts_with('Z'))
    };
    if !still_present {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
    Err(format!(
        "Windows taskkill did not confirm termination of PID {pid}: {}",
        if detail.is_empty() {
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        } else {
            detail
        }
    ))
}

#[cfg(windows)]
fn terminate_process_group(group_id: u32, mut child: Option<&mut Child>) -> bool {
    // taskkill /T requests termination of the captured root and its child tree;
    // bounded revalidation decides whether force termination is needed.
    let _ = send_signal_checked(-(group_id as i32), SIGNAL_TERM);
    let deadline = Instant::now() + Duration::from_secs(4);
    while Instant::now() < deadline {
        if let Some(child_ref) = child.as_mut() {
            let _ = child_ref.try_wait();
        }
        if !process_group_alive(group_id) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    if process_group_alive(group_id) {
        let _ = send_signal_checked(-(group_id as i32), SIGNAL_KILL);
    }
    if let Some(child_ref) = child.as_mut() {
        let wait_deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < wait_deadline {
            if matches!(child_ref.try_wait(), Ok(Some(_))) {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline && process_group_alive(group_id) {
        thread::sleep(Duration::from_millis(100));
    }
    !process_group_alive(group_id)
}

#[cfg(unix)]
fn process_group_has_live_member(group_id: u32) -> bool {
    process_snapshot()
        .iter()
        .any(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
}

#[cfg(windows)]
fn process_group_has_live_member(group_id: u32) -> bool {
    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return true;
    }
    snapshot.iter().any(|item| item.pid == group_id && !item.stat.starts_with('Z'))
        || snapshot.iter().any(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
        || !windows_descendant_pids(group_id, &snapshot).is_empty()
}

#[cfg(unix)]
fn send_signal_checked(target: i32, signal: i32) -> Result<(), String> {
    let target_kind = if target < 0 { "process-group" } else { "PID" };
    append_stop_trace(format!(
        "SIGNAL ATTEMPT: {} target={} {}",
        signal_name(signal),
        target,
        target_kind
    ));
    let rc = unsafe { libc::kill(target, signal) };
    if rc == 0 {
        append_stop_trace(format!(
            "SIGNAL RESULT: {} target={} returned success (kill syscall returned 0).",
            signal_name(signal),
            target
        ));
        return Ok(());
    }
    let error = std::io::Error::last_os_error();
    // ESRCH is harmless during shutdown: a captured process may already have exited.
    if error.raw_os_error() == Some(libc::ESRCH) {
        append_stop_trace(format!("SIGNAL RESULT: {} target={} errno={:?} (process/group already absent; treated as exited).", signal_name(signal), target, error.raw_os_error()));
        return Ok(());
    }
    let detail = format!(
        "kill({target}, {} / signal {}) failed: {} (errno={:?})",
        signal_name(signal),
        signal,
        error,
        error.raw_os_error()
    );
    append_stop_trace(format!("SIGNAL ERROR: {detail}"));
    Err(detail)
}

/// Terminate the complete MLX/Python process group, not just PyInstaller's parent.
#[cfg(unix)]
fn terminate_process_group(group_id: u32, mut child: Option<&mut Child>) -> bool {
    unsafe {
        libc::kill(-(group_id as i32), SIGNAL_TERM);
    }
    let deadline = Instant::now() + Duration::from_secs(4);
    while Instant::now() < deadline {
        if let Some(child_ref) = child.as_mut() {
            let _ = child_ref.try_wait();
        }
        if !process_group_alive(group_id) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    if process_group_alive(group_id) {
        unsafe {
            libc::kill(-(group_id as i32), SIGNAL_KILL);
        }
    }
    if let Some(child_ref) = child.as_mut() {
        let _ = child_ref.wait();
    }
    let reap_deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < reap_deadline && process_group_alive(group_id) {
        thread::sleep(Duration::from_millis(100));
    }
    !process_group_alive(group_id)
}

#[cfg(unix)]
fn describe_port_owner(port: u16) -> String {
    let port_arg = format!("-iTCP:{port}");
    let run_lsof = |program: &str| {
        Command::new(program)
            .args(["-nP", port_arg.as_str(), "-sTCP:LISTEN", "-Fpc"])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
    };
    let output = run_lsof("/usr/sbin/lsof").or_else(|_| run_lsof("lsof"));
    let Ok(output) = output else {
        return "The owning process could not be identified automatically.".into();
    };
    if !output.status.success() {
        return "No listener details were returned by lsof.".into();
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut pid: Option<String> = None;
    let mut name: Option<String> = None;
    let mut found: Vec<String> = Vec::new();
    let add_current = |pid: &Option<String>, name: &Option<String>, found: &mut Vec<String>| {
        if let Some(pid_value) = pid {
            let command_name = name.as_deref().unwrap_or("unknown process");
            let item = format!("{command_name} (PID {pid_value})");
            if !found.contains(&item) {
                found.push(item);
            }
        }
    };
    for field in stdout.lines() {
        if let Some(value) = field.strip_prefix('p') {
            add_current(&pid, &name, &mut found);
            pid = Some(value.to_string());
            name = None;
        } else if let Some(value) = field.strip_prefix('c') {
            name = Some(value.to_string());
        }
    }
    add_current(&pid, &name, &mut found);
    if found.is_empty() {
        "The owning process could not be identified automatically.".into()
    } else {
        format!(
            "Listener found: {}.",
            found.into_iter().take(4).collect::<Vec<_>>().join(", ")
        )
    }
}

#[cfg(unix)]
fn listening_pids_on_port(port: u16) -> Result<Vec<u32>, String> {
    let port_arg = format!("-iTCP:{port}");
    let mut last_error = String::new();
    for program in ["/usr/sbin/lsof", "lsof"] {
        let output = match Command::new(program)
            .args(["-nP", port_arg.as_str(), "-sTCP:LISTEN", "-t"])
            .stdin(Stdio::null())
            .output()
        {
            Ok(value) => value,
            Err(error) => {
                last_error = format!("{program}: {error}");
                continue;
            }
        };
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if output.status.success() {
            let mut pids = Vec::new();
            for line in stdout
                .lines()
                .map(str::trim)
                .filter(|line| !line.is_empty())
            {
                let value = line.parse::<u32>().map_err(|_| {
                    format!("lsof returned an invalid PID while checking port {port}: {line}")
                })?;
                if !pids.contains(&value) {
                    pids.push(value);
                }
            }
            return Ok(pids);
        }
        // lsof returns status 1 with empty output when no process is listening.
        if output.status.code() == Some(1) && stdout.trim().is_empty() && stderr.is_empty() {
            return Ok(Vec::new());
        }
        last_error = format!(
            "{program} exited with status {:?}: {}",
            output.status.code(),
            if stderr.is_empty() {
                stdout.trim().to_string()
            } else {
                stderr
            }
        );
    }
    Err(format!(
        "Could not verify whether port {port} is free: {last_error}"
    ))
}


#[cfg(windows)]
fn powershell_json_u32_list(script: &str, description: &str) -> Result<Vec<u32>, String> {
    let mut command = Command::new("powershell.exe");
    command.creation_flags(0x08000000);
    let output = command
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script])
        .stdin(Stdio::null()).stderr(Stdio::piped()).output()
        .map_err(|error| format!("Could not inspect {description} with PowerShell: {error}"))?;
    if !output.status.success() {
        return Err(format!("Could not inspect {description}: {}", String::from_utf8_lossy(&output.stderr).trim()));
    }
    let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if raw.is_empty() || raw == "null" { return Ok(Vec::new()); }
    let value: Value = serde_json::from_str(&raw)
        .map_err(|error| format!("Could not parse Windows {description} output: {error}"))?;
    let values: Vec<Value> = match value {
        Value::Array(items) => items,
        Value::Null => Vec::new(),
        single => vec![single],
    };
    Ok(values.into_iter().filter_map(|value| value.as_u64().or_else(|| value.as_str().and_then(|s| s.parse().ok())).map(|n| n as u32)).collect())
}

#[cfg(windows)]
fn listening_pids_on_port(port: u16) -> Result<Vec<u32>, String> {
    let script = format!("$ErrorActionPreference='Stop'; $values=@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); ConvertTo-Json -InputObject $values -Compress");
    let mut values = powershell_json_u32_list(&script, &format!("TCP listener ownership for port {port}"))?;
    values.sort_unstable(); values.dedup(); Ok(values)
}

#[cfg(windows)]
fn listening_ports_for_pid(pid: u32) -> Result<Vec<u16>, String> {
    let script = format!("$ErrorActionPreference='Stop'; $values=@(Get-NetTCPConnection -State Listen -OwningProcess {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort -Unique); ConvertTo-Json -InputObject $values -Compress");
    let values = powershell_json_u32_list(&script, &format!("listening TCP ports for PID {pid}"))?;
    let mut ports: Vec<u16> = values.into_iter().filter_map(|value| u16::try_from(value).ok()).filter(|port| *port > 0).collect();
    ports.sort_unstable(); ports.dedup(); Ok(ports)
}

#[cfg(windows)]
fn describe_port_owner(port: u16) -> String {
    match listening_pids_on_port(port) {
        Ok(pids) if pids.is_empty() => "No listener details were returned by the Windows TCP table.".into(),
        Ok(pids) => {
            let processes = process_snapshot();
            let names = pids.iter().take(4).map(|pid| {
                processes.iter().find(|item| item.pid == *pid)
                    .map(|item| format!("PID {pid}: {}", item.command.split_whitespace().take(4).collect::<Vec<_>>().join(" ")))
                    .unwrap_or_else(|| format!("PID {pid}: process details unavailable"))
            }).collect::<Vec<_>>();
            format!("Listener found: {}.", names.join(", "))
        }
        Err(error) => format!("The owning process could not be identified automatically: {error}"),
    }
}

/// Read the actual listening TCP ports owned by a specific PID. This is a
/// read-only fallback for packaged helper processes (for example a Python
/// resource_tracker) that inherit a listener but do not have `--port` in argv.
#[cfg(unix)]
fn listening_ports_for_pid(pid: u32) -> Result<Vec<u16>, String> {
    let pid_arg = pid.to_string();
    let mut last_error = String::new();
    for program in ["/usr/sbin/lsof", "lsof"] {
        let output = match Command::new(program)
            .args([
                "-nP",
                "-a",
                "-p",
                pid_arg.as_str(),
                "-iTCP",
                "-sTCP:LISTEN",
                "-Fn",
            ])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
        {
            Ok(value) => value,
            Err(error) => {
                last_error = format!("{program}: {error}");
                continue;
            }
        };
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if output.status.success() {
            let mut ports = Vec::new();
            for line in stdout.lines().map(str::trim) {
                let Some(endpoint) = line.strip_prefix('n') else {
                    continue;
                };
                let Some((_, raw_port)) = endpoint.rsplit_once(':') else {
                    continue;
                };
                let Ok(port) = raw_port.parse::<u16>() else {
                    continue;
                };
                if (1..=65535).contains(&port) && !ports.contains(&port) {
                    ports.push(port);
                }
            }
            ports.sort_unstable();
            return Ok(ports);
        }
        if output.status.code() == Some(1) && stdout.trim().is_empty() && stderr.is_empty() {
            return Ok(Vec::new());
        }
        last_error = format!(
            "{program} exited with status {:?}: {}",
            output.status.code(),
            if stderr.is_empty() {
                stdout.trim().to_string()
            } else {
                stderr
            }
        );
    }
    Err(format!(
        "Could not inspect listening TCP sockets for PID {pid}: {last_error}"
    ))
}

fn infer_verified_process_port(pid: u32, command: &str) -> Result<Option<u16>, String> {
    if let Some(port) = parsed_port_from_command(command) {
        return Ok(Some(port));
    }
    // Do not scan or adopt arbitrary ports. Ask lsof which listening ports this
    // exact PID owns, then require a unique result before any stop is attempted.
    let ports = listening_ports_for_pid(pid)?;
    match ports.as_slice() {
        [] => Ok(None),
        [port] => Ok(Some(*port)),
        _ => Err(format!(
            "Verified packaged PID {pid} owns multiple TCP listening ports ({}); no process was stopped. Inspect Developer Options → Diagnostics.",
            ports.iter().map(u16::to_string).collect::<Vec<_>>().join(", ")
        )),
    }
}

/// Require the selected port to remain free across several checks. This catches
/// the case where an older manager immediately respawns its backend after Stop.
fn verify_port_released(port: u16, stopped_pid: u32) -> Result<(), String> {
    append_stop_trace(format!("PORT RELEASE CHECK: begin for TCP port {port}; original target PID={stopped_pid}; require 3 consecutive free checks within 3s."));
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut consecutive_free = 0;
    loop {
        let listeners = match listening_pids_on_port(port) {
            Ok(pids) => pids,
            Err(error) => {
                append_stop_trace(format!("PORT RELEASE CHECK ERROR: {error}"));
                return Err(error);
            }
        };
        if listeners.is_empty() {
            consecutive_free += 1;
            append_stop_trace(format!("PORT RELEASE CHECK: TCP {port} has no listeners ({consecutive_free}/3 consecutive free checks)."));
            if consecutive_free >= 3 {
                append_stop_trace(format!(
                    "PORT RELEASE VERIFIED: TCP {port} remained free for 3 consecutive checks."
                ));
                return Ok(());
            }
        } else {
            consecutive_free = 0;
            let detail = format!(
                "TCP {port} listener process IDs: {listener_pids}. Port owner details: {port_owner_details}",
                listener_pids = listeners
                    .iter()
                    .map(u32::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                port_owner_details = describe_port_owner(port)
            );
            append_stop_trace(format!("PORT STILL OCCUPIED: {detail}"));
            if !listeners.contains(&stopped_pid) {
                let error = format!("Backend PID {stopped_pid} was stopped, but port {port} is still occupied by these listener process IDs: {listener_pids}. RDT Connect did not stop any other processes. {port_owner_details}", listener_pids = listeners.iter().map(u32::to_string).collect::<Vec<_>>().join(", "), port_owner_details = describe_port_owner(port));
                append_stop_trace(format!("PORT RELEASE FAILURE: {error}"));
                return Err(error);
            }
        }
        if Instant::now() >= deadline {
            let listeners = match listening_pids_on_port(port) {
                Ok(pids) => pids,
                Err(error) => {
                    append_stop_trace(format!("FINAL PORT CHECK ERROR: {error}"));
                    return Err(error);
                }
            };
            if listeners.is_empty() {
                append_stop_trace(format!(
                    "PORT RELEASE VERIFIED: TCP {port} is free at deadline check."
                ));
                return Ok(());
            }
            let error = format!(
                "Stop could not verify that port {port} is free. Listener process IDs: {listener_pids}. Port owner details: {port_owner_details}",
                listener_pids = listeners
                    .iter()
                    .map(u32::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                port_owner_details = describe_port_owner(port)
            );
            append_stop_trace(format!("PORT RELEASE FAILURE: {error}"));
            return Err(error);
        }
        thread::sleep(Duration::from_millis(150));
    }
}

#[cfg(unix)]
fn find_existing_backend_pids() -> Vec<String> {
    let output = match Command::new("/usr/bin/pgrep")
        .args(["-fl", "rdt_voice_backend"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(value) => value,
        Err(_) => return Vec::new(),
    };
    if !output.status.success() {
        return Vec::new();
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let pid = fields.next()?;
            let command = fields.collect::<Vec<_>>().join(" ");
            if pid.chars().all(|c| c.is_ascii_digit()) && command.contains("rdt_voice_backend") {
                Some(pid.to_string())
            } else {
                None
            }
        })
        .take(8)
        .collect()
}

#[cfg(windows)]
fn find_existing_backend_pids() -> Vec<String> {
    process_snapshot()
        .into_iter()
        .filter(|item| item.command.to_ascii_lowercase().contains("rdt_voice_backend"))
        .map(|item| item.pid.to_string())
        .take(8)
        .collect()
}

#[cfg(unix)]
fn process_command_and_group(pid: u32) -> Option<(String, u32)> {
    let command = Command::new("/bin/ps")
        .args(["-ww", "-p", &pid.to_string(), "-o", "command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !command.status.success() {
        return None;
    }
    let command_text = String::from_utf8_lossy(&command.stdout).trim().to_string();
    let group = Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "pgid="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    let group_text = String::from_utf8_lossy(&group.stdout).trim().to_string();
    Some((command_text, group_text.parse().ok()?))
}

#[derive(Clone)]
struct ProcessSnapshot {
    pid: u32,
    ppid: u32,
    pgid: u32,
    stat: String,
    started: String,
    command: String,
}

#[cfg(unix)]
fn process_snapshot() -> Vec<ProcessSnapshot> {
    let output = match Command::new("/bin/ps")
        // lstart lets us distinguish the captured process from a later process
        // that reuses the same PID and command line.
        .args(["-ww", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(value) if value.status.success() => value,
        _ => return Vec::new(),
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            // The ps `lstart` column is five whitespace-delimited words.
            // Keep the remaining command column separate so spaces in command
            // arguments do not corrupt process-start identity.
            if fields.len() < 10 {
                return None;
            }
            Some(ProcessSnapshot {
                pid: fields[0].parse().ok()?,
                ppid: fields[1].parse().ok()?,
                pgid: fields[2].parse().ok()?,
                stat: fields[3].to_string(),
                started: fields[4..9].join(" "),
                command: fields[9..].join(" "),
            })
        })
        .collect()
}


#[cfg(windows)]
fn process_command_and_group(pid: u32) -> Option<(String, u32)> {
    process_snapshot()
        .into_iter()
        .find(|item| item.pid == pid)
        .map(|item| (item.command, item.pgid))
}

#[cfg(windows)]
fn windows_executable_path_from_command(command: &str) -> &str {
    if let Some(rest) = command.strip_prefix('"') {
        rest.split_once('"').map(|(path, _)| path).unwrap_or(rest)
    } else {
        command.split_whitespace().next().unwrap_or(command)
    }
}

#[cfg(windows)]
fn is_windows_executable(command: &str, expected_name: &str) -> bool {
    let path = windows_executable_path_from_command(command);
    let name = path.rsplit(|character| character == '\\' || character == '/')
        .next().unwrap_or(path);
    name.eq_ignore_ascii_case(expected_name)
}

#[cfg(windows)]
fn is_rdt_manager_command(command: &str) -> bool {
    is_windows_executable(command, "rdt-connect-desktop.exe")
        || is_windows_executable(command, "rdt-connect-desktop")
}

#[cfg(unix)]
fn is_rdt_manager_command(command: &str) -> bool {
    command.split_whitespace().any(|part| {
        Path::new(part).file_name().and_then(|name| name.to_str()) == Some("rdt-connect-desktop")
    })
}

#[cfg(windows)]
fn process_snapshot() -> Vec<ProcessSnapshot> {
    // Win32_Process supplies stable process start times and parent IDs. The
    // synthetic pgid is the outermost RDT backend ancestor PID; this makes the
    // existing ownership/descendant checks meaningful on Windows without
    // treating all processes with the same image name as one group.
    const SCRIPT: &str = "$ErrorActionPreference='Stop'; Get-CimInstance -ClassName Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress";
    let mut command = Command::new("powershell.exe");
    command.creation_flags(0x08000000);
    let output = match command
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", SCRIPT])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(output) if output.status.success() => output,
        _ => return Vec::new(),
    };
    let Ok(value) = serde_json::from_slice::<Value>(&output.stdout) else {
        return Vec::new();
    };
    let rows: Vec<&Value> = match &value {
        Value::Array(rows) => rows.iter().collect(),
        Value::Object(_) => vec![&value],
        _ => return Vec::new(),
    };
    let mut snapshot: Vec<ProcessSnapshot> = rows.into_iter().filter_map(|row| {
        let pid = row.get("ProcessId").and_then(|value| value.as_u64().or_else(|| value.as_str()?.parse().ok()))? as u32;
        let ppid = row.get("ParentProcessId").and_then(|value| value.as_u64().or_else(|| value.as_str()?.parse().ok())).unwrap_or(0) as u32;
        let started = row.get("CreationDate").and_then(Value::as_str)?.to_string();
        let exe = row.get("ExecutablePath").and_then(Value::as_str).unwrap_or_default();
        let args = row.get("CommandLine").and_then(Value::as_str).unwrap_or_default();
        let command = if exe.is_empty() { args.to_string() } else if args.is_empty() { format!("\"{exe}\"") } else { format!("\"{exe}\" {args}") };
        if command.is_empty() { return None; }
        Some(ProcessSnapshot { pid, ppid, pgid: pid, stat: "R".into(), started, command })
    }).collect();

    let parent_map: std::collections::HashMap<u32, (u32, bool)> = snapshot.iter()
        .map(|item| (item.pid, (item.ppid, is_windows_executable(&item.command, "rdt_voice_backend.exe")
            || is_windows_executable(&item.command, "rdt_voice_backend"))))
        .collect();
    for item in &mut snapshot {
        let mut cursor = item.pid;
        let mut group_root = item.pid;
        let mut seen = std::collections::HashSet::new();
        for _ in 0..128 {
            if !seen.insert(cursor) { break; }
            let Some((parent, is_backend)) = parent_map.get(&cursor) else { break; };
            if *is_backend { group_root = cursor; }
            if *parent == 0 || *parent == cursor { break; }
            cursor = *parent;
        }
        item.pgid = group_root;
    }
    snapshot
}

fn process_tree_snapshot(root_pid: u32) -> Vec<ProcessSnapshot> {
    let all = process_snapshot();
    let Some(root) = all.iter().find(|item| item.pid == root_pid).cloned() else {
        return Vec::new();
    };
    let mut tree = vec![root];
    loop {
        let mut added = false;
        for item in &all {
            if tree.iter().any(|known| known.pid == item.ppid)
                && !tree.iter().any(|known| known.pid == item.pid)
            {
                tree.push(item.clone());
                added = true;
            }
        }
        if !added {
            break;
        }
    }
    tree
}

#[cfg(unix)]
fn snapshot_process_is_alive(snapshot: &ProcessSnapshot, current: &[ProcessSnapshot]) -> bool {
    current.iter().any(|item| {
        item.pid == snapshot.pid
            && item.pgid == snapshot.pgid
            && item.started == snapshot.started
            && item.command == snapshot.command
            && !item.stat.starts_with('Z')
    })
}

#[cfg(windows)]
fn snapshot_process_is_alive(snapshot: &ProcessSnapshot, current: &[ProcessSnapshot]) -> bool {
    // Windows pgid is synthesized from the current parent tree; it can change
    // when the root exits. PID + creation time + command identifies the captured
    // process without falsely treating a still-running child as gone.
    current.iter().any(|item| {
        item.pid == snapshot.pid
            && item.started == snapshot.started
            && item.command == snapshot.command
            && !item.stat.starts_with('Z')
    })
}

/// Terminate a verified orphan and its captured descendants. For a process that
/// owns an isolated group, signal that group too. For legacy processes with a
/// shared group, signal only the verified backend and descendants captured from
/// its parent/child tree; never signal the shared process group.
fn terminate_verified_backend_tree(
    pid: u32,
    group_id: u32,
    expected_exe: &Path,
) -> Result<(), String> {
    append_stop_trace(format!("PROCESS TREE CAPTURE: requested PID={pid}, expected PGID={group_id}, expected executable={}", sanitize_log_line(&expected_exe.display().to_string(), &home_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default())));
    let initial = process_tree_snapshot(pid);
    append_stop_trace(format!(
        "PROCESS TREE CAPTURE RESULT: {} process(es).",
        initial.len()
    ));
    let home = home_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    for item in &initial {
        append_stop_trace(format!(
            "TREE MEMBER: PID={} PPID={} PGID={} STAT={} COMMAND={}",
            item.pid,
            item.ppid,
            item.pgid,
            item.stat,
            sanitize_log_line(&item.command, &home)
        ));
    }
    let Some(root) = initial.first() else {
        append_stop_trace(format!(
            "SAFETY/STATE ERROR: requested backend PID {pid} was absent from the process snapshot."
        ));
        return Err(format!(
            "Verified backend PID {pid} is no longer present. Refresh server status."
        ));
    };
    if !command_is_expected_backend(&root.command, expected_exe) {
        append_stop_trace(format!("SAFETY REFUSAL: root PID {pid} command did not match the verified backend executable. No signal sent."));
        return Err("Safety check refused to stop this process tree because its root is not the expected packaged RDT Connect backend. No process was stopped.".into());
    }
    let owns_group = group_id == pid;
    append_stop_trace(format!(
        "PROCESS GROUP DECISION: PGID={group_id}; isolated_group_owned_by_backend={owns_group}."
    ));
    let mut signal_errors: Vec<String> = Vec::new();

    // Signal the isolated process group when it is genuinely led by this backend.
    // Still signal each captured member individually as a fallback if the group
    // signal is denied or the process was launched by an older manager build.
    if owns_group {
        if let Err(error) = send_signal_checked(-(group_id as i32), SIGNAL_TERM) {
            signal_errors.push(format!("process group {group_id}: {error}"));
        }
    }
    for item in initial.iter().skip(1).rev() {
        let current = process_snapshot();
        if snapshot_process_is_alive(item, &current) {
            if let Err(error) = send_signal_checked(item.pid as i32, SIGNAL_TERM) {
                signal_errors.push(format!("PID {}: {error}", item.pid));
            }
        }
    }
    let root_now = process_command_and_group(pid);
    if let Some((command, current_group)) = root_now {
        if current_group == group_id && command_is_expected_backend(&command, expected_exe) {
            // Also target the exact verified root PID. This is a fallback if the
            // group signal was denied, and harmless if the group signal already
            // began shutting the root down.
            if let Err(error) = send_signal_checked(pid as i32, SIGNAL_TERM) {
                signal_errors.push(format!("PID {pid}: {error}"));
            }
        } else {
            return Err("The backend PID or process group changed during Stop verification. No further signal was sent; refresh diagnostics and retry.".into());
        }
    }

    let deadline = Instant::now() + Duration::from_secs(4);
    loop {
        let current = process_snapshot();
        let tree_alive = initial
            .iter()
            .any(|item| snapshot_process_is_alive(item, &current));
        let group_alive = owns_group && process_group_has_live_member(group_id);
        if !tree_alive && !group_alive {
            append_stop_trace("TERM PHASE RESULT: all captured non-zombie process identities exited; isolated group has no live members.");
            return Ok(());
        }
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }

    // Escalate to SIGKILL, but only after re-verifying every process identity.
    let term_snapshot = process_snapshot();
    let term_remaining = initial
        .iter()
        .filter(|item| snapshot_process_is_alive(item, &term_snapshot))
        .map(|item| {
            format!(
                "PID {} (PPID {} PGID {} STAT {})",
                item.pid, item.ppid, item.pgid, item.stat
            )
        })
        .collect::<Vec<_>>();
    append_stop_trace(format!("TERM TIMEOUT after 4s: remaining captured members=[{}]; live isolated group={}. Escalating verified identities to SIGKILL.", term_remaining.join(", "), if owns_group { process_group_has_live_member(group_id).to_string() } else { "not group-owned".into() }));
    if owns_group {
        if let Err(error) = send_signal_checked(-(group_id as i32), SIGNAL_KILL) {
            signal_errors.push(format!("process group {group_id}: {error}"));
        }
    }
    for item in initial.iter().skip(1).rev() {
        let current = process_snapshot();
        if snapshot_process_is_alive(item, &current) {
            if let Err(error) = send_signal_checked(item.pid as i32, SIGNAL_KILL) {
                signal_errors.push(format!("PID {}: {error}", item.pid));
            }
        }
    }
    let root_now = process_command_and_group(pid);
    if let Some((command, current_group)) = root_now {
        if current_group == group_id && command_is_expected_backend(&command, expected_exe) {
            if let Err(error) = send_signal_checked(pid as i32, SIGNAL_KILL) {
                signal_errors.push(format!("PID {pid}: {error}"));
            }
        }
    }

    let final_deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let current = process_snapshot();
        let tree_alive = initial
            .iter()
            .any(|item| snapshot_process_is_alive(item, &current));
        let group_alive = owns_group && process_group_has_live_member(group_id);
        if !tree_alive && !group_alive {
            append_stop_trace("KILL PHASE RESULT: all captured non-zombie process identities exited; isolated group has no live members.");
            return Ok(());
        }
        if Instant::now() >= final_deadline {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    let current = process_snapshot();
    let mut remaining: Vec<String> = initial
        .iter()
        .filter(|item| snapshot_process_is_alive(item, &current))
        .map(|item| format!("PID {}", item.pid))
        .collect();
    if owns_group && process_group_has_live_member(group_id) {
        remaining.push(format!("live process group {group_id}"));
    }
    let signal_note = if signal_errors.is_empty() {
        String::new()
    } else {
        format!(" Signal errors: {}.", signal_errors.join("; "))
    };
    append_stop_trace(format!("STOP TREE FAILURE: remaining verified identities=[{}]; live isolated group={}; signal_errors=[{}].", remaining.join(", "), if owns_group { process_group_has_live_member(group_id).to_string() } else { "not group-owned".into() }, signal_errors.join("; ")));
    Err(format!("Could not confirm the verified RDT Connect backend tree stopped: {}. Stop did not report success; refresh diagnostics and retry.{}", remaining.join(", "), signal_note))
}

fn parsed_port_from_command(command: &str) -> Option<u16> {
    let fields: Vec<&str> = command.split_whitespace().collect();
    fields.windows(2).find_map(|w| {
        (w[0] == "--port")
            .then(|| w[1].parse::<u16>().ok())
            .flatten()
    })
}

#[cfg(unix)]
fn other_manager_instance_exists() -> bool {
    let output = match Command::new("/usr/bin/pgrep")
        .args(["-fl", "rdt-connect-desktop"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(value) if value.status.success() => value,
        _ => return false,
    };
    String::from_utf8_lossy(&output.stdout).lines().any(|line| {
        let mut fields = line.split_whitespace();
        let Some(pid_text) = fields.next() else {
            return false;
        };
        let Ok(pid) = pid_text.parse::<u32>() else {
            return false;
        };
        let executable = fields.next().unwrap_or_default();
        pid != std::process::id()
            && Path::new(executable)
                .file_name()
                .and_then(|name| name.to_str())
                == Some("rdt-connect-desktop")
    })
}

#[cfg(windows)]
fn other_manager_instance_exists() -> bool {
    let snapshot = process_snapshot();
    // If enumeration fails, conservatively avoid treating the system as clear.
    snapshot.is_empty() || snapshot.iter().any(|item| {
        item.pid != std::process::id() && is_rdt_manager_command(&item.command)
    })
}

fn command_is_expected_backend(command: &str, expected_exe: &Path) -> bool {
    // Match only this project's packaged executable across Tauri dev/resource-dir layouts.
    // This must never turn into a broad Python/Whisper process matcher.
    let mut candidates = vec![expected_exe.to_path_buf()];
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(parent) = current_exe.parent() {
            candidates.push(backend_executable_path(parent));
        }
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    candidates.push(backend_executable_path(&manifest.join("target").join("debug")));
    candidates.push(backend_executable_path(&manifest.join("resources")));
    let command_cmp = if cfg!(windows) {
        command.replace('/', "\\").to_ascii_lowercase()
    } else {
        command.to_string()
    };
    command_cmp.contains("rdt_voice_backend")
        && candidates.iter().any(|candidate| {
            let canonical = fs::canonicalize(candidate).unwrap_or_else(|_| candidate.clone());
            let candidate_text = if cfg!(windows) {
                candidate.to_string_lossy().replace('/', "\\").to_ascii_lowercase()
            } else {
                candidate.display().to_string()
            };
            let canonical_text = if cfg!(windows) {
                canonical.to_string_lossy().replace('/', "\\").to_ascii_lowercase()
            } else {
                canonical.display().to_string()
            };
            command_cmp.contains(&canonical_text) || command_cmp.contains(&candidate_text)
        })
}

fn scan_orphaned_backends(state: &BackendState) -> OrphanScan {
    if state.status().running {
        return OrphanScan {
            manager_locked_elsewhere: false,
            instances: Vec::new(),
        };
    }
    // Even when another manager holds the lock, report a verified matching backend PID/port
    // so the user can explicitly stop a stale instance from this window. The Stop command
    // revalidates executable, PID, process group, descendants and direct manager ownership.
    // A stale backend may have inherited the lock descriptor from an older app build.
    // The live manager process, not the lock bit alone, determines whether another
    // instance currently owns the server. Exact executable verification still guards Stop.
    let manager_locked_elsewhere = other_manager_instance_exists();
    let expected_exe = backend_executable_path(&state.resource_dir);
    let mut instances = Vec::new();
    for value in find_existing_backend_pids() {
        let Ok(pid) = value.parse::<u32>() else {
            continue;
        };
        if pid == std::process::id() {
            continue;
        }
        let process_state = process_snapshot().into_iter().find(|item| item.pid == pid);
        if process_state
            .as_ref()
            .map(|item| item.stat.starts_with('Z'))
            .unwrap_or(true)
        {
            continue;
        }
        let Some((command, _pgid)) = process_command_and_group(pid) else {
            continue;
        };
        if !command_is_expected_backend(&command, &expected_exe) {
            continue;
        }
        let compact = format!(
            "{} · process group {}",
            command
                .split_whitespace()
                .take(8)
                .collect::<Vec<_>>()
                .join(" "),
            _pgid
        );
        instances.push(OrphanedBackend {
            pid,
            port: parsed_port_from_command(&command),
            description: compact,
        });
    }
    OrphanScan {
        manager_locked_elsewhere,
        instances,
    }
}

fn stop_orphaned_backend_safely(state: &BackendState, pid: u32) -> Result<(), String> {
    if state.status().running {
        return Err("This RDT Connect instance already owns a backend. Stop it with the normal Stop button.".into());
    }
    // This is a user-confirmed stop of one PID displayed by the orphan scanner.
    // Do not reject it merely because the old manager is still alive: that was the
    // reason the in-app button could fail while its own menu-bar Stop worked.
    // The process executable, PID identity, process group and descendants are still
    // revalidated below before any signal is sent.
    let expected_exe = backend_executable_path(&state.resource_dir);
    let Some((command, group_id)) = process_command_and_group(pid) else {
        return Err("That process has already exited. Refresh the server status.".into());
    };
    if !command_is_expected_backend(&command, &expected_exe) {
        return Err("Safety check refused to stop this PID because its command does not match this project's packaged RDT Connect backend. No process was stopped.".into());
    }
    // Recheck identity and process group immediately before walking/killing the tree.
    let Some((command_again, group_again)) = process_command_and_group(pid) else {
        return Err("That process has already exited. Refresh the server status.".into());
    };
    if command_again != command
        || group_again != group_id
        || !command_is_expected_backend(&command_again, &expected_exe)
    {
        return Err("The process changed during verification, so no signal was sent. Refresh and inspect again.".into());
    }
    terminate_verified_backend_tree(pid, group_id, &expected_exe)
}

/// Stop the backend owned by this instance, then clean up only verified orphan
/// processes from this same packaged app when no other live manager owns them.
fn stop_owned_and_orphan_backends(state: &BackendState) -> Result<(), String> {
    if state.status().running {
        state.stop()?;
    }
    if other_manager_instance_exists() {
        return Ok(());
    }
    let scan = scan_orphaned_backends(state);
    for instance in scan.instances {
        stop_orphaned_backend_safely(state, instance.pid)?;
    }
    let remaining = scan_orphaned_backends(state);
    if !remaining.instances.is_empty() {
        let detail = remaining
            .instances
            .iter()
            .map(|item| {
                format!(
                    "PID {} on port {}",
                    item.pid,
                    item.port
                        .map(|port| port.to_string())
                        .unwrap_or_else(|| "unknown".into())
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        return Err(format!("Stop did not complete. Remaining verified RDT Connect backend activity: {detail}. The app has not reported the server as stopped."));
    }
    Ok(())
}

fn check_preferred_port(preferred: u16) -> Result<u16, String> {
    if preferred < 1024 {
        return Err("Choose a preferred port between 1024 and 65535.".into());
    }
    match TcpListener::bind(("127.0.0.1", preferred)) {
        Ok(listener) => { drop(listener); Ok(preferred) }
        Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => {
            let owner = describe_port_owner(preferred);
            Err(format!("Preferred port 127.0.0.1:{preferred} is already occupied. {owner} No backend was started and no model was loaded. Choose a different Preferred local port and retry."))
        }
        Err(error) => Err(format!("Could not check preferred port 127.0.0.1:{preferred}: {error}. No backend was started. Choose another port and retry.")),
    }
}

fn write_log_prefix(file: &mut File, line: &str) {
    let _ = writeln!(file, "\n===== {} =====", line);
    let _ = file.flush();
}

fn tail_log(path: &Path, max_lines: usize) -> String {
    // Read only a bounded window from the end; a long-running server log must not
    // cause the diagnostics action to load the entire file into memory.
    const MAX_TAIL_BYTES: u64 = 1_000_000;
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    let size = match file.metadata() {
        Ok(m) => m.len(),
        Err(_) => return String::new(),
    };
    let start = size.saturating_sub(MAX_TAIL_BYTES);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut bytes = Vec::with_capacity((size - start) as usize);
    if file.read_to_end(&mut bytes).is_err() {
        return String::new();
    }
    let mut data = String::from_utf8_lossy(&bytes).into_owned();
    if start > 0 {
        if let Some(newline) = data.find('\n') {
            data = data[newline + 1..].to_string();
        }
    }
    let lines: Vec<&str> = data.lines().collect();
    lines
        .iter()
        .rev()
        .take(max_lines)
        .copied()
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n")
}

fn load_local_tls_config(state: &BackendState) -> Result<LocalTlsConfig, String> {
    if !state.tls_config_path.is_file() {
        return Ok(LocalTlsConfig::default());
    }
    let raw = fs::read(&state.tls_config_path)
        .map_err(|e| format!("Could not read local HTTPS settings: {e}"))?;
    serde_json::from_slice(&raw)
        .map_err(|e| format!("Local HTTPS settings are invalid JSON; no server was started: {e}"))
}

fn save_local_tls_config(state: &BackendState, config: &LocalTlsConfig) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(config)
        .map_err(|e| format!("Could not encode local HTTPS settings: {e}"))?;
    let temporary = state
        .tls_config_path
        .with_extension(format!("json.tmp.{}", std::process::id()));
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&temporary)
        .map_err(|e| format!("Could not create a temporary HTTPS settings file: {e}"))?;
    file.write_all(&bytes)
        .map_err(|e| format!("Could not write HTTPS settings: {e}"))?;
    file.sync_all()
        .map_err(|e| format!("Could not flush HTTPS settings: {e}"))?;
    fs::rename(&temporary, &state.tls_config_path).map_err(|e| {
        let _ = fs::remove_file(&temporary);
        format!("Could not save HTTPS settings: {e}")
    })?;
    let _ = set_private_permissions(&state.tls_config_path, 0o600);
    Ok(())
}

fn sha256_file_hex(path: &Path) -> Result<String, String> {
    let bytes = fs::read(path)
        .map_err(|error| format!("Could not read a file for integrity verification: {error}"))?;
    Ok(format!("{:x}", Sha256::digest(&bytes)))
}

#[cfg(windows)]
fn powershell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(windows)]
fn run_powershell_script(script: &str, what: &str) -> Result<std::process::Output, String> {
    let encoded_script = BASE64.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<u8>>(),
    );
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
        ])
        .arg(encoded_script)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.creation_flags(0x08000000);
    let output = command
        .output()
        .map_err(|error| format!("Could not start Windows PowerShell to {what}: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let detail = if !stderr.is_empty() { stderr } else { stdout };
        return Err(format!(
            "Windows PowerShell could not {what}{}",
            if detail.is_empty() { ".".into() } else { format!(": {detail}") }
        ));
    }
    Ok(output)
}

#[cfg(windows)]
fn powershell_read_certificate(path: &Path) -> Result<String, String> {
    let path = path
        .to_str()
        .ok_or_else(|| "The certificate path is not valid Unicode on Windows.".to_string())?;
    Ok(format!(
        r#"function Read-RdtCertificate([string]$p) {{
  $pem = [System.IO.File]::ReadAllText($p)
  $m = [regex]::Match($pem, '-----BEGIN CERTIFICATE-----\s*(?<body>[A-Za-z0-9+/=\r\n]+)\s*-----END CERTIFICATE-----')
  if (-not $m.Success) {{ throw 'certificate_pem_invalid' }}
  $der = [Convert]::FromBase64String(($m.Groups['body'].Value -replace '\s', ''))
  return [System.Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]]$der)
}}
$cert = Read-RdtCertificate {path}
"#,
        path = powershell_quote(path)
    ))
}

#[cfg(windows)]
fn windows_certificate_thumbprint(cert_path: &Path) -> Result<String, String> {
    let mut script = powershell_read_certificate(cert_path)?;
    script.push_str("[Console]::Out.Write($cert.Thumbprint.ToUpperInvariant())\n");
    let output = run_powershell_script(&script, "read a certificate thumbprint")?;
    let thumbprint = String::from_utf8_lossy(&output.stdout).trim().to_ascii_uppercase();
    if thumbprint.len() != 40 || !thumbprint.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("Windows returned an invalid certificate thumbprint.".into());
    }
    Ok(thumbprint)
}

#[cfg(windows)]
fn windows_root_store_contains(thumbprint: &str) -> Result<bool, String> {
    if thumbprint.len() != 40 || !thumbprint.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("A malformed certificate fingerprint was refused.".into());
    }
    let script = format!(
        r#"$ErrorActionPreference = 'Stop'
$thumb = {}
$store = [System.Security.Cryptography.X509Certificates.X509Store]::new([System.Security.Cryptography.X509Certificates.StoreName]::Root, [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
try {{
  $found = @($store.Certificates | Where-Object {{ $_.Thumbprint.ToUpperInvariant() -eq $thumb }}).Count -gt 0
  if ($found) {{ [Console]::Out.Write('true') }} else {{ [Console]::Out.Write('false') }}
}} finally {{ $store.Close() }}
"#,
        powershell_quote(&thumbprint.to_ascii_uppercase())
    );
    let output = run_powershell_script(&script, "inspect the current-user trusted-root store")?;
    match String::from_utf8_lossy(&output.stdout).trim() {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err("Windows returned an invalid trusted-root store result.".into()),
    }
}

#[cfg(windows)]
fn windows_add_root_to_current_user_store(ca_path: &Path) -> Result<String, String> {
    // Resolve the fingerprint before any store mutation so an unexpected result
    // can be rolled back without searching for or touching an unrelated root.
    let expected_thumbprint = windows_certificate_thumbprint(ca_path)?;
    let mut script = powershell_read_certificate(ca_path)?;
    script.push_str(
        r#"$caExtensions = @($cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' })
if ($caExtensions.Count -lt 1 -or -not ([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]$caExtensions[0]).CertificateAuthority) { throw 'selected_certificate_is_not_a_ca' }
if ($cert.Subject -ne $cert.Issuer) { throw 'selected_ca_is_not_self_issued' }
$thumb = $cert.Thumbprint.ToUpperInvariant()
$store = [System.Security.Cryptography.X509Certificates.X509Store]::new([System.Security.Cryptography.X509Certificates.StoreName]::Root, [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {
  $existing = @($store.Certificates | Where-Object { $_.Thumbprint.ToUpperInvariant() -eq $thumb }).Count -gt 0
  if ($existing) { throw 'root_certificate_already_present_not_added_by_this_operation' }
  $publicOnly = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]]$cert.RawData)
  $store.Add($publicOnly)
} finally { $store.Close() }
[Console]::Out.Write($thumb)
"#,
    );
    let output = run_powershell_script(&script, "add the RDT root CA to the current user's trust store")?;
    let thumbprint = String::from_utf8_lossy(&output.stdout).trim().to_ascii_uppercase();
    if thumbprint != expected_thumbprint {
        let _ = windows_remove_root_from_current_user_store(&expected_thumbprint);
        return Err("Windows reported a root fingerprint different from the generated CA; the newly added CA was rolled back where possible.".into());
    }
    match windows_root_store_contains(&thumbprint) {
        Ok(true) => Ok(thumbprint),
        Ok(false) => {
            let _ = windows_remove_root_from_current_user_store(&expected_thumbprint);
            Err("Windows did not retain the RDT root CA in the current user's trust store; the new entry was rolled back where possible.".into())
        }
        Err(error) => {
            let rollback = windows_remove_root_from_current_user_store(&expected_thumbprint);
            Err(format!("Could not verify the current-user root store after adding the RDT CA. Rollback {}. Detail: {error}", if rollback.is_ok() { "completed" } else { "also needs attention" }))
        }
    }
}

#[cfg(windows)]
fn windows_remove_root_from_current_user_store(thumbprint: &str) -> Result<(), String> {
    if thumbprint.len() != 40 || !thumbprint.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("A malformed certificate fingerprint was refused.".into());
    }
    let script = format!(
        r#"$ErrorActionPreference = 'Stop'
$thumb = {}
$store = [System.Security.Cryptography.X509Certificates.X509Store]::new([System.Security.Cryptography.X509Certificates.StoreName]::Root, [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
try {{
  $matches = @($store.Certificates | Where-Object {{ $_.Thumbprint.ToUpperInvariant() -eq $thumb }})
  foreach ($item in $matches) {{ $store.Remove($item) }}
}} finally {{ $store.Close() }}
"#,
        powershell_quote(&thumbprint.to_ascii_uppercase())
    );
    run_powershell_script(&script, "remove the exact RDT-managed root from the current user's trust store")?;
    if windows_root_store_contains(thumbprint)? {
        return Err("Windows still reports the RDT-managed root in the current-user trust store.".into());
    }
    Ok(())
}

#[cfg(windows)]
fn windows_validate_generated_tls_files(config: &LocalTlsConfig, cert: &Path, key: &Path, ca: &Path) -> Result<(), String> {
    for (path, expected, label) in [
        (cert, config.windows_certificate_sha256.as_deref(), "server certificate"),
        (key, config.windows_private_key_sha256.as_deref(), "server private key"),
        (ca, config.windows_ca_sha256.as_deref(), "root CA certificate"),
    ] {
        let expected = expected.ok_or_else(|| format!("The generated Windows {label} has no integrity record. Run Set up trusted local HTTPS again."))?;
        let actual = sha256_file_hex(path)?;
        if !expected.eq_ignore_ascii_case(&actual) {
            return Err(format!("The generated Windows {label} changed after setup. Run Set up trusted local HTTPS again; HTTPS will not start with altered TLS material."));
        }
    }

    let mut script = powershell_read_certificate(cert)?;
    let ca_path = ca.to_str().ok_or_else(|| "The CA path is not valid Unicode on Windows.".to_string())?;
    // The server certificate/private-key match is checked against the CNG key
    // before export in windows_generate_tls_files. Pinning the exact private-key
    // bytes here detects post-setup changes without importing a new CNG key
    // container on every health/status poll.
    script.push_str(&format!("$ca = Read-RdtCertificate {}\n", powershell_quote(ca_path)));
    script.push_str(r#"
$now = [DateTime]::UtcNow
if ($cert.NotBefore.ToUniversalTime() -gt $now -or $cert.NotAfter.ToUniversalTime() -le $now) { throw 'server_certificate_expired_or_not_yet_valid' }
if ($ca.NotBefore.ToUniversalTime() -gt $now -or $ca.NotAfter.ToUniversalTime() -le $now) { throw 'root_ca_expired_or_not_yet_valid' }
$caExtensions = @($ca.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.19' })
if ($caExtensions.Count -lt 1 -or -not ([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]$caExtensions[0]).CertificateAuthority) { throw 'configured_root_is_not_a_ca' }
if ($ca.Subject -ne $ca.Issuer -or $cert.Issuer -ne $ca.Subject) { throw 'certificate_issuer_mismatch' }
$sanExtensions = @($cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' })
$sanText = if ($sanExtensions.Count -gt 0) { $sanExtensions[0].Format($true) } else { '' }
if ($sanText -notmatch '(?im)IP\s*Address\s*[=:]\s*127\.0\.0\.1') { throw 'server_certificate_missing_loopback_ip_san' }
$ekuExtensions = @($cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' })
if ($ekuExtensions.Count -lt 1 -or -not (($ekuExtensions[0].EnhancedKeyUsages | Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.1' }).Count -gt 0)) { throw 'server_certificate_missing_server_auth_eku' }
$chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
$chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
$chain.ChainPolicy.ExtraStore.Add($ca)
$chain.ChainPolicy.VerificationFlags = [System.Security.Cryptography.X509Certificates.X509VerificationFlags]::AllowUnknownCertificateAuthority
if (-not $chain.Build($cert)) { throw 'server_certificate_chain_validation_failed' }
$chainRoot = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.Thumbprint.ToUpperInvariant()
if ($chainRoot -ne $ca.Thumbprint.ToUpperInvariant()) { throw 'server_certificate_does_not_chain_to_configured_root' }
[Console]::Out.Write('OK')
"#);
    run_powershell_script(&script, "validate the generated Windows TLS certificate, private-key match, SAN, expiry, chain and pinned files")?;
    Ok(())
}

#[cfg(windows)]
fn windows_generate_tls_files(cert_path: &Path, key_path: &Path, ca_path: &Path) -> Result<(), String> {
    let cert_path = cert_path.to_str().ok_or_else(|| "The certificate path is not valid Unicode on Windows.".to_string())?;
    let key_path = key_path.to_str().ok_or_else(|| "The private-key path is not valid Unicode on Windows.".to_string())?;
    let ca_path = ca_path.to_str().ok_or_else(|| "The CA path is not valid Unicode on Windows.".to_string())?;
    let mut script = String::from(r#"
$ErrorActionPreference = 'Stop'
$ca = $null
$server = $null
function Write-RdtPem([string]$path, [string]$label, [byte[]]$bytes) {
  $encoded = [Convert]::ToBase64String($bytes)
  $lines = New-Object System.Collections.Generic.List[string]
  for ($offset = 0; $offset -lt $encoded.Length; $offset += 64) {
    $length = [Math]::Min(64, $encoded.Length - $offset)
    $lines.Add($encoded.Substring($offset, $length))
  }
  $pem = "-----BEGIN $label-----`r`n" + ($lines -join "`r`n") + "`r`n-----END $label-----`r`n"
  [System.IO.File]::WriteAllText($path, $pem, [System.Text.Encoding]::ASCII)
}
function Remove-RdtGeneratedCertificate($item) {
  if ($null -eq $item) { return }
  $thumb = $item.Thumbprint
  $uniqueName = $null
  try {
    $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($item)
    if ($rsa -is [System.Security.Cryptography.RSACng]) { $uniqueName = $rsa.Key.UniqueName }
    if ($rsa) { $rsa.Dispose() }
  } catch { }
  try { Remove-Item -LiteralPath ("Cert:\CurrentUser\My\" + $thumb) -Force -ErrorAction SilentlyContinue } catch { }
  if ($uniqueName) {
    try {
      $storedKey = [System.Security.Cryptography.CngKey]::Open($uniqueName, [System.Security.Cryptography.CngProvider]::MicrosoftSoftwareKeyStorageProvider)
      $storedKey.Delete()
      $storedKey.Dispose()
    } catch { }
  }
  try { $item.Dispose() } catch { }
}
try {
  $ca = New-SelfSignedCertificate -Type Custom -Subject 'CN=RDT Connect Local HTTPS CA' -CertStoreLocation 'Cert:\CurrentUser\My' -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(5) -TextExtension @('2.5.29.19={critical}{text}ca=TRUE&pathlength=0','2.5.29.15={critical}{text}keyCertSign,cRLSign')
  if ($null -eq $ca -or -not $ca.HasPrivateKey) { throw 'root_ca_generation_failed' }
  $server = New-SelfSignedCertificate -Type Custom -Subject 'CN=127.0.0.1' -Signer $ca -CertStoreLocation 'Cert:\CurrentUser\My' -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddDays(365) -TextExtension @('2.5.29.19={critical}{text}ca=FALSE','2.5.29.15={critical}{text}digitalSignature,keyEncipherment','2.5.29.37={text}1.3.6.1.5.5.7.3.1','2.5.29.17={text}DNS=localhost&IPAddress=127.0.0.1')
  if ($null -eq $server -or -not $server.HasPrivateKey) { throw 'server_certificate_generation_failed' }
  $publicRsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPublicKey($server)
  $privateRsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($server)
  if ($null -eq $publicRsa -or $privateRsa -isnot [System.Security.Cryptography.RSACng]) { throw 'server_private_key_provider_not_supported' }
  try {
    $pub = $publicRsa.ExportParameters($false)
    $priv = $privateRsa.ExportParameters($false)
    if ([Convert]::ToBase64String($pub.Modulus) -ne [Convert]::ToBase64String($priv.Modulus) -or [Convert]::ToBase64String($pub.Exponent) -ne [Convert]::ToBase64String($priv.Exponent)) { throw 'server_key_pair_mismatch' }
    $privateDer = $privateRsa.Key.Export([System.Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
  } finally {
    if ($publicRsa) { $publicRsa.Dispose() }
    if ($privateRsa) { $privateRsa.Dispose() }
  }
  Write-RdtPem @CERT@ 'CERTIFICATE' ([byte[]]$server.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
  Write-RdtPem @CA@ 'CERTIFICATE' ([byte[]]$ca.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
  Write-RdtPem @KEY@ 'PRIVATE KEY' ([byte[]]$privateDer)
  [Console]::Out.Write('OK')
} finally {
  Remove-RdtGeneratedCertificate $server
  Remove-RdtGeneratedCertificate $ca
}
"#);
    script = script
        .replace("@CERT@", &powershell_quote(cert_path))
        .replace("@KEY@", &powershell_quote(key_path))
        .replace("@CA@", &powershell_quote(ca_path));
    let output = run_powershell_script(&script, "generate a loopback-only local HTTPS certificate and private key")?;
    if String::from_utf8_lossy(&output.stdout).trim() != "OK" {
        return Err("Windows certificate generation did not return an explicit success result.".into());
    }
    Ok(())
}

fn openssl_output(args: &[&str], what: &str) -> Result<std::process::Output, String> {
    if !cfg!(target_os = "macos") {
        return Err("Local HTTPS certificate setup is not qualified on this platform yet. Windows/Linux development builds use HTTP on loopback only; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
    let output = Command::new("/usr/bin/openssl")
        .args(args)
        .output()
        .map_err(|e| format!("RDT Connect could not run macOS OpenSSL to {what}: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(format!(
            "Could not {what}. {}",
            if detail.is_empty() {
                "Check the selected certificate and key files.".into()
            } else {
                detail
            }
        ));
    }
    Ok(output)
}

#[cfg(target_os = "macos")]
fn run_security(args: &[&str], what: &str) -> Result<(), String> {
    let output = Command::new("/usr/bin/security")
        .args(args)
        .output()
        .map_err(|e| format!("macOS Keychain operation could not run to {what}: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(format!(
            "Could not {what}. {}",
            if detail.is_empty() {
                String::from_utf8_lossy(&output.stdout).trim().to_string()
            } else {
                detail
            }
        ));
    }
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn run_security(_args: &[&str], _what: &str) -> Result<(), String> {
    Err("macOS Keychain trust operations are not available on this platform.".into())
}

#[cfg(target_os = "macos")]
fn login_keychain() -> Result<PathBuf, String> {
    Ok(home_dir()?
        .join("Library")
        .join("Keychains")
        .join("login.keychain-db"))
}

#[cfg(not(target_os = "macos"))]
fn login_keychain() -> Result<PathBuf, String> {
    Err("macOS login Keychain trust operations are not available on this platform.".into())
}

fn canonical_file(raw: &str, label: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(raw.trim())
        .canonicalize()
        .map_err(|e| format!("Could not resolve the selected {label}: {e}"))?;
    if !path.is_file() {
        return Err(format!("The selected {label} is not a regular file."));
    }
    Ok(path)
}

fn sha1_fingerprint(cert: &Path) -> Result<String, String> {
    if !cfg!(target_os = "macos") {
        return Err("macOS Keychain fingerprinting is unavailable on Windows; the Windows path uses SHA-256 fingerprints and the current-user certificate store. No macOS command was run.".into());
    }
    let output = Command::new("/usr/bin/openssl")
        .args(["x509", "-in"])
        .arg(cert)
        .args(["-noout", "-fingerprint", "-sha1"])
        .output()
        .map_err(|e| format!("Could not calculate certificate fingerprint: {e}"))?;
    if !output.status.success() {
        return Err("Could not calculate the selected CA certificate's fingerprint.".into());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let value = text
        .split('=')
        .nth(1)
        .unwrap_or("")
        .trim()
        .replace(':', "")
        .to_ascii_uppercase();
    if value.len() != 40 || !value.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("OpenSSL returned an invalid certificate fingerprint.".into());
    }
    Ok(value)
}

#[cfg(windows)]
fn validate_windows_tls_material(config: &LocalTlsConfig) -> Result<(), String> {
    if !config.generated {
        return Err("Windows trusted local HTTPS currently accepts only RDT-generated certificates; use Set up trusted local HTTPS so certificate identity, private-key matching, and current-user trust can be verified safely.".into());
    }
    let cert_raw = config.certificate_path.as_deref().ok_or_else(|| "HTTPS is the default, but no server certificate is configured. Select Set up trusted local HTTPS; RDT Connect will not silently fall back to HTTP.".to_string())?;
    let key_raw = config.private_key_path.as_deref().ok_or_else(|| "HTTPS is the default, but no private key is configured. Select Set up trusted local HTTPS; RDT Connect will not silently fall back to HTTP.".to_string())?;
    let ca_raw = config.ca_certificate_path.as_deref().ok_or_else(|| "The generated Windows HTTPS setup has no recorded root CA. Run Set up trusted local HTTPS again.".to_string())?;
    let cert = canonical_file(cert_raw, "server certificate")?;
    let key = canonical_file(key_raw, "private key")?;
    let ca = canonical_file(ca_raw, "root CA certificate")?;
    windows_validate_generated_tls_files(config, &cert, &key, &ca)
}

fn validate_tls_material(config: &LocalTlsConfig) -> Result<(), String> {
    #[cfg(windows)]
    {
        return validate_windows_tls_material(config);
    }
    #[cfg(all(not(target_os = "macos"), not(windows)))]
    {
        let _ = config;
        return Err("Trusted local HTTPS is not implemented on this platform. Keep development HTTP loopback-only; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
    let cert_raw = config.certificate_path.as_deref().ok_or_else(|| "HTTPS is the default, but no server certificate is linked yet. Open Settings → Local HTTPS and set up a trusted certificate; RDT Connect will not silently fall back to HTTP.".to_string())?;
    let key_raw = config.private_key_path.as_deref().ok_or_else(|| "HTTPS is the default, but no private key is linked yet. Open Settings → Local HTTPS and set up a trusted certificate; RDT Connect will not silently fall back to HTTP.".to_string())?;
    let cert = canonical_file(cert_raw, "server certificate")?;
    let key = canonical_file(key_raw, "private key")?;
    if !Command::new("/usr/bin/openssl")
        .args(["x509", "-in"])
        .arg(&cert)
        .args(["-noout", "-checkend", "0"])
        .status()
        .map_err(|e| format!("Could not validate the certificate expiry: {e}"))?
        .success()
    {
        return Err("The linked HTTPS certificate is expired or not yet valid. Link a current certificate and matching private key.".into());
    }
    let details = Command::new("/usr/bin/openssl")
        .args(["x509", "-in"])
        .arg(&cert)
        .args(["-noout", "-text"])
        .output()
        .map_err(|e| format!("Could not inspect the HTTPS certificate: {e}"))?;
    if !details.status.success() {
        return Err("The linked server certificate could not be parsed.".into());
    }
    let details = String::from_utf8_lossy(&details.stdout);
    let san_start = details.find("X509v3 Subject Alternative Name").unwrap_or(0);
    let san = &details[san_start..];
    if !san.contains("IP Address:127.0.0.1") {
        return Err("This certificate is not valid for RDT Connect's local address. Its Subject Alternative Name must include IP Address:127.0.0.1. A certificate for another hostname alone is not enough.".into());
    }
    let cert_pub = openssl_output(
        &[
            "x509",
            "-in",
            cert.to_str().ok_or("Invalid certificate path")?,
            "-pubkey",
            "-noout",
        ],
        "read the certificate public key",
    )?;
    let key_pub = Command::new("/usr/bin/openssl")
        .args(["pkey", "-in"])
        .arg(&key)
        .args(["-pubout", "-passin", "pass:"])
        .output()
        .map_err(|e| format!("Could not inspect the private key: {e}"))?;
    if !key_pub.status.success() {
        return Err("The private key cannot be read without a passphrase. Link an unencrypted key file protected by macOS file permissions; RDT Connect never copies the key.".into());
    }
    if cert_pub.stdout != key_pub.stdout {
        return Err("The selected certificate and private key do not match.".into());
    }
    if let Some(ca_raw) = config.ca_certificate_path.as_deref() {
        let ca = canonical_file(ca_raw, "root CA certificate")?;
        let ca_text = Command::new("/usr/bin/openssl")
            .args(["x509", "-in"])
            .arg(&ca)
            .args(["-noout", "-text"])
            .output()
            .map_err(|e| format!("Could not inspect the issuer CA: {e}"))?;
        if !ca_text.status.success()
            || !String::from_utf8_lossy(&ca_text.stdout).contains("CA:TRUE")
        {
            return Err("The selected CA file is not a CA certificate (CA:TRUE). Choose a CA certificate, not another leaf/server certificate.".into());
        }
        let self_verify = Command::new("/usr/bin/openssl")
            .args(["verify", "-CAfile"])
            .arg(&ca)
            .arg(&ca)
            .output()
            .map_err(|e| format!("Could not verify whether the selected CA is self-signed: {e}"))?;
        if !self_verify.status.success() {
            return Err("The selected CA is not a self-signed root. Choose the root CA certificate; intermediate CA files are not accepted as trust anchors.".into());
        }
        let verified = Command::new("/usr/bin/openssl")
            .args(["verify", "-CAfile"])
            .arg(&ca)
            .arg(&cert)
            .output()
            .map_err(|e| format!("Could not verify the selected certificate chain: {e}"))?;
        if !verified.status.success() {
            return Err(format!(
                "The selected CA does not validate the server certificate: {}",
                String::from_utf8_lossy(&verified.stderr).trim()
            ));
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn certificate_trusted(cert: &Path) -> bool {
    let path = match cert.to_str() {
        Some(v) => v,
        None => return false,
    };
    Command::new("/usr/bin/security")
        .args(["verify-cert", "-c", path, "-p", "ssl", "-s", "127.0.0.1"])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[cfg(windows)]
fn windows_certificate_trusted(config: &LocalTlsConfig) -> bool {
    let (Some(cert_raw), Some(ca_raw)) = (config.certificate_path.as_deref(), config.ca_certificate_path.as_deref()) else { return false; };
    let cert_path = Path::new(cert_raw);
    let ca_path = Path::new(ca_raw);
    let Some(ca_path_text) = ca_path.to_str() else { return false; };
    let Ok(mut script) = powershell_read_certificate(cert_path) else { return false; };
    script.push_str(&format!("$ca = Read-RdtCertificate {}\n", powershell_quote(ca_path_text)));
    script.push_str(r#"
$now = [DateTime]::UtcNow
if ($cert.NotBefore.ToUniversalTime() -gt $now -or $cert.NotAfter.ToUniversalTime() -le $now) { [Console]::Out.Write('false'); exit 0 }
if ($ca.NotBefore.ToUniversalTime() -gt $now -or $ca.NotAfter.ToUniversalTime() -le $now) { [Console]::Out.Write('false'); exit 0 }
if ($ca.Subject -ne $ca.Issuer -or $cert.Issuer -ne $ca.Subject) { [Console]::Out.Write('false'); exit 0 }
$san = @($cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' })
$sanText = if ($san.Count -gt 0) { $san[0].Format($true) } else { '' }
if ($sanText -notmatch '(?im)IP\s*Address\s*[=:]\s*127\.0\.0\.1') { [Console]::Out.Write('false'); exit 0 }
$chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
$chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
if (-not $chain.Build($cert)) { [Console]::Out.Write('false'); exit 0 }
$root = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.Thumbprint.ToUpperInvariant()
if ($root -ne $ca.Thumbprint.ToUpperInvariant()) { [Console]::Out.Write('false'); exit 0 }
$store = [System.Security.Cryptography.X509Certificates.X509Store]::new([System.Security.Cryptography.X509Certificates.StoreName]::Root, [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
try { $present = @($store.Certificates | Where-Object { $_.Thumbprint.ToUpperInvariant() -eq $root }).Count -gt 0 } finally { $store.Close() }
if ($present) { [Console]::Out.Write('true') } else { [Console]::Out.Write('false') }
"#);
    match run_powershell_script(&script, "verify Windows current-user TLS trust").map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string()) {
        Ok(value) => value == "true",
        Err(_) => false,
    }
}

#[cfg(all(not(target_os = "macos"), not(windows)))]
fn certificate_trusted(_cert: &Path) -> bool { false }

fn configured_certificate_trusted(config: &LocalTlsConfig) -> bool {
    #[cfg(windows)] { return windows_certificate_trusted(config); }
    #[cfg(not(windows))] {
        config.certificate_path.as_deref().map(Path::new).map(certificate_trusted).unwrap_or(false)
    }
}

#[cfg(target_os = "macos")]
fn trust_ca_file(
    state: &BackendState,
    config: &mut LocalTlsConfig,
    ca_path: &Path,
) -> Result<(), String> {
    let ca = canonical_file(
        ca_path.to_str().ok_or("Invalid CA path")?,
        "root CA certificate",
    )?;
    let details = Command::new("/usr/bin/openssl")
        .args(["x509", "-in"])
        .arg(&ca)
        .args(["-noout", "-text"])
        .output()
        .map_err(|e| format!("Could not inspect the CA certificate: {e}"))?;
    if !details.status.success() || !String::from_utf8_lossy(&details.stdout).contains("CA:TRUE") {
        return Err("The selected certificate is not a CA certificate; it will not be added to Keychain trust.".into());
    }
    // Do not elevate an intermediate issuer to a trust root. Only a self-signed CA
    // that verifies against itself can be explicitly added as trustRoot here.
    let self_verify = Command::new("/usr/bin/openssl")
        .args(["verify", "-CAfile"])
        .arg(&ca)
        .arg(&ca)
        .output()
        .map_err(|e| format!("Could not verify whether the selected CA is self-signed: {e}"))?;
    if !self_verify.status.success() {
        return Err("The selected CA is not a self-signed trust root. RDT Connect will not promote an intermediate CA to a root. Select your root CA, or rely on an existing trusted system root.".into());
    }
    let keychain = login_keychain()?;
    if !keychain.is_file() {
        return Err(
            "The macOS login Keychain was not found. Unlock/check Keychain Access, then retry."
                .into(),
        );
    }
    let fingerprint = sha1_fingerprint(&ca)?;
    let already_tracked = config.managed_trust_fingerprints.contains(&fingerprint);
    let keychain_inventory = Command::new("/usr/bin/security")
        .args(["find-certificate", "-a", "-Z"])
        .arg(&keychain)
        .output()
        .map_err(|e| format!("Could not inspect the login Keychain before changing trust: {e}"))?;
    if !keychain_inventory.status.success() {
        return Err("RDT Connect could not inspect the login Keychain, so it refused to change certificate trust.".into());
    }
    let fingerprint_present = String::from_utf8_lossy(&keychain_inventory.stdout)
        .to_ascii_uppercase()
        .contains(&fingerprint);
    if fingerprint_present && !already_tracked {
        if config
            .certificate_path
            .as_deref()
            .map(Path::new)
            .map(certificate_trusted)
            .unwrap_or(false)
        {
            // The certificate is already trusted; do not adopt or track an existing CA entry.
            save_local_tls_config(state, config)?;
            return Ok(());
        }
        return Err("This root CA already exists in your login Keychain but is not tracked as added by RDT Connect. To avoid claiming or later removing a certificate installed by you or another app, RDT Connect left it unchanged. Review it in Keychain Access.".into());
    }
    run_security(
        &[
            "add-trusted-cert",
            "-r",
            "trustRoot",
            "-k",
            keychain.to_str().ok_or("Invalid login Keychain path")?,
            ca.to_str().ok_or("Invalid CA path")?,
        ],
        "add the selected CA to this Mac's login Keychain",
    )?;
    if !already_tracked {
        config.managed_trust_fingerprints.push(fingerprint);
    }
    save_local_tls_config(state, config)?;
    Ok(())
}

#[cfg(windows)]
fn trust_ca_file(
    state: &BackendState,
    config: &mut LocalTlsConfig,
    ca_path: &Path,
) -> Result<(), String> {
    if !config.generated {
        return Err("Windows will not install a user-selected CA as RDT-managed trust. Use Set up trusted local HTTPS to generate and verify the app-owned certificate.".into());
    }
    validate_tls_material(config)?;
    let expected_ca = config.ca_certificate_path.as_deref().ok_or_else(|| "The Windows HTTPS config has no root CA path.".to_string())?;
    let ca = canonical_file(ca_path.to_str().ok_or("Invalid CA path")?, "root CA certificate")?;
    let configured_ca = canonical_file(expected_ca, "configured root CA certificate")?;
    if ca != configured_ca {
        return Err("The selected CA is not the root CA recorded for the generated Windows server certificate.".into());
    }
    let fingerprint = windows_certificate_thumbprint(&ca)?;
    let already_tracked = config.managed_trust_fingerprints.iter().any(|item| item.eq_ignore_ascii_case(&fingerprint));
    if windows_root_store_contains(&fingerprint)? {
        if already_tracked || configured_certificate_trusted(config) {
            return Ok(());
        }
        return Err("This CA already exists in the current user's trusted-root store, but RDT Connect did not record installing it and the configured HTTPS chain does not verify. It was left unchanged.".into());
    }

    let installed_fingerprint = windows_add_root_to_current_user_store(&ca)?;
    if !already_tracked {
        config.managed_trust_fingerprints.push(installed_fingerprint.clone());
    }
    if let Err(error) = save_local_tls_config(state, config) {
        let _ = windows_remove_root_from_current_user_store(&installed_fingerprint);
        if !already_tracked {
            config.managed_trust_fingerprints.retain(|item| !item.eq_ignore_ascii_case(&installed_fingerprint));
        }
        return Err(format!("The Windows trust entry was installed but its ownership record could not be saved; the newly installed entry was rolled back. Detail: {error}"));
    }
    if !configured_certificate_trusted(config) {
        let _ = windows_remove_root_from_current_user_store(&installed_fingerprint);
        config.managed_trust_fingerprints.retain(|item| !item.eq_ignore_ascii_case(&installed_fingerprint));
        save_local_tls_config(state, config)?;
        return Err("The root was added to the current user's trust store, but Windows did not verify the full certificate chain for 127.0.0.1. The new trust entry was removed; no HTTP fallback was attempted.".into());
    }
    Ok(())
}

#[cfg(all(not(target_os = "macos"), not(windows)))]
fn trust_ca_file(
    _state: &BackendState,
    _config: &mut LocalTlsConfig,
    _ca: &Path,
) -> Result<(), String> {
    Err("Native local certificate trust is not implemented on this platform.".into())
}

fn local_https_status_from_config(config: &LocalTlsConfig) -> LocalHttpsStatus {
    let configured = validate_tls_material(config).is_ok();
    let trusted = configured && configured_certificate_trusted(config);
    let message = if config.mode.eq_ignore_ascii_case("http") {
        "HTTP is selected for development only. HTTPS is the normal default; no automatic HTTP fallback is used.".to_string()
    } else if !configured {
        validate_tls_material(config).err().unwrap_or_else(|| {
            "A trusted HTTPS certificate and matching private key are required.".into()
        })
    } else if !trusted {
        if cfg!(target_os = "windows") {
            "The certificate is configured, but Windows has not verified it against the current user's trusted-root store. Trust the generated RDT root CA, then recheck.".to_string()
        } else {
            "The certificate and key match, but the operating system does not currently trust this certificate for 127.0.0.1. Trust the selected root CA, then recheck.".to_string()
        }
    } else {
        if cfg!(target_os = "windows") {
            "HTTPS is configured: the certificate is valid for 127.0.0.1 and Windows current-user trust verification passed.".to_string()
        } else {
            "HTTPS is configured: the certificate is valid for 127.0.0.1 and macOS trust verification passed.".to_string()
        }
    };
    LocalHttpsStatus {
        mode: config.mode.clone(),
        configured,
        trusted,
        generated: config.generated,
        certificate_path: config.certificate_path.clone(),
        private_key_path: config.private_key_path.clone(),
        ca_certificate_path: config.ca_certificate_path.clone(),
        rdt_added_trust_count: config.managed_trust_fingerprints.len(),
        message,
    }
}

fn local_tls_client(config: &LocalTlsConfig, timeout: Duration) -> Result<Client, String> {
    let mut builder = Client::builder().timeout(timeout);
    if config.mode.eq_ignore_ascii_case("https") {
        // rustls-tls-native-roots loads the Mac's trusted certificates. If the user
        // linked a private/self-signed root, add that explicit root as well.
        if let Some(trust_path) = config.ca_certificate_path.as_deref() {
            let pem = fs::read(trust_path)
                .map_err(|e| format!("Could not read the configured root CA certificate: {e}"))?;
            let root = reqwest::Certificate::from_pem(&pem)
                .map_err(|e| format!("Could not load the configured root CA certificate: {e}"))?;
            builder = builder.add_root_certificate(root);
        }
    }
    builder
        .build()
        .map_err(|e| format!("Could not create the verified local HTTP client: {e}"))
}

fn healthy(port: u16, config: &LocalTlsConfig) -> bool {
    let scheme = if config.mode.eq_ignore_ascii_case("https") {
        "https"
    } else {
        "http"
    };
    let url = format!("{scheme}://127.0.0.1:{port}/health");
    let client = match local_tls_client(config, Duration::from_millis(1200)) {
        Ok(c) => c,
        Err(_) => return false,
    };
    match client.get(url).send() {
        Ok(response) if response.status().as_u16() == 200 => response
            .text()
            .map(|body| body.contains("\"status\":\"ok\"") || body.contains("\"status\": \"ok\""))
            .unwrap_or(false),
        _ => false,
    }
}

#[tauri::command]
fn get_local_https_status(state: State<'_, BackendState>) -> Result<LocalHttpsStatus, String> {
    let config = load_local_tls_config(&state)?;
    Ok(local_https_status_from_config(&config))
}

#[tauri::command]
async fn pick_tls_file(
    app: AppHandle,
    state: State<'_, BackendState>,
    kind: String,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let locale = read_app_language(&state);
    let (title, label, extensions): (String, String, &[&str]) = match kind.as_str() {
        "certificate" => (
            tray_text(
                &locale,
                "msg.choose_rdt_voice_server_certificate.047c3ba8",
                "Choose RDT Voice server certificate",
                &[],
            ),
            tray_text(
                &locale,
                "msg.certificate_files.169b49a8",
                "Certificate files",
                &[],
            ),
            &["pem", "crt", "cer"],
        ),
        "private-key" => (
            tray_text(
                &locale,
                "msg.choose_matching_private_key.ec49aa09",
                "Choose matching private key",
                &[],
            ),
            tray_text(
                &locale,
                "msg.private_key_files.611887c1",
                "Private-key files",
                &[],
            ),
            &["pem", "key"],
        ),
        "ca" => (
            tray_text(
                &locale,
                "msg.choose_self_signed_root_ca_certificate.95a15bb1",
                "Choose self-signed root CA certificate",
                &[],
            ),
            tray_text(
                &locale,
                "msg.ca_certificate_files.0154de22",
                "CA certificate files",
                &[],
            ),
            &["pem", "crt", "cer"],
        ),
        _ => return Err("Unknown certificate file type.".into()),
    };
    let picked = app
        .dialog()
        .file()
        .set_title(title)
        .add_filter(label, extensions)
        .blocking_pick_file();
    match picked {
        Some(file) => Ok(Some(
            file.into_path()
                .map_err(|e| format!("The selected file path is unavailable: {e}"))?
                .to_string_lossy()
                .into_owned(),
        )),
        None => Ok(None),
    }
}

#[cfg(windows)]
fn setup_local_https_certificate_windows(state: &BackendState) -> Result<LocalHttpsStatus, String> {
    if state.status().running {
        return Err("Stop the server before changing TLS certificates.".into());
    }
    let existing_config = load_local_tls_config(state)?;
    let existing_status = local_https_status_from_config(&existing_config);
    if existing_status.configured && existing_status.trusted {
        let mut config = existing_config;
        config.mode = "https".into();
        save_local_tls_config(state, &config)?;
        let status = local_https_status_from_config(&config);
        if status.configured && status.trusted && status.mode.eq_ignore_ascii_case("https") { return Ok(status); }
        return Err(format!("Windows no longer confirms the existing trusted HTTPS configuration: {}", status.message));
    }
    // If file integrity and certificate checks still pass, retry only the
    // current-user trust operation instead of issuing a new CA on each click.
    if existing_status.configured && existing_config.generated {
        if let Some(ca_path) = existing_config.ca_certificate_path.clone() {
            let mut config = existing_config;
            config.mode = "https".into();
            trust_ca_file(state, &mut config, Path::new(&ca_path))?;
            let status = local_https_status_from_config(&config);
            if status.configured && status.trusted { return Ok(status); }
            return Err(format!("Windows certificate trust could not be confirmed after retry: {}", status.message));
        }
    }

    let seconds = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let dir = state.https_dir.join(format!("generated-windows-{seconds}-{}", std::process::id()));
    fs::create_dir_all(&dir).map_err(|error| format!("Could not create the Windows HTTPS certificate folder: {error}"))?;
    set_private_permissions(&dir, 0o700).map_err(|error| format!("Could not restrict access to the Windows HTTPS certificate folder: {error}"))?;
    let cert = dir.join("rdt-local-server.pem");
    let key = dir.join("rdt-local-server-key.pem");
    let ca = dir.join("rdt-local-ca.pem");
    windows_generate_tls_files(&cert, &key, &ca)?;
    for path in [&cert, &key, &ca] {
        if !path.is_file() { return Err("Windows certificate generation did not create every required TLS file.".into()); }
    }
    for path in [&cert, &key, &ca] {
        set_private_permissions(path, 0o600).map_err(|error| format!("Could not apply current-user-only permissions to generated HTTPS material: {error}"))?;
    }
    let mut config = load_local_tls_config(state)?;
    config.mode = "https".into();
    config.certificate_path = Some(cert.display().to_string());
    config.private_key_path = Some(key.display().to_string());
    config.ca_certificate_path = Some(ca.display().to_string());
    config.generated = true;
    config.windows_certificate_sha256 = Some(sha256_file_hex(&cert)?);
    config.windows_private_key_sha256 = Some(sha256_file_hex(&key)?);
    config.windows_ca_sha256 = Some(sha256_file_hex(&ca)?);
    save_local_tls_config(state, &config)?;
    validate_tls_material(&config)?;
    if let Err(error) = trust_ca_file(state, &mut config, &ca) {
        return Err(format!("Windows generated and validated the local certificate files, but current-user trust setup did not complete. The files were retained safely so setup can be retried. Detail: {error}"));
    }
    let status = local_https_status_from_config(&config);
    if !status.configured || !status.trusted || !status.mode.eq_ignore_ascii_case("https") {
        return Err(format!("Windows local HTTPS setup did not pass its final verification: {}. No HTTP fallback was attempted.", status.message));
    }
    Ok(status)
}

#[tauri::command]
fn setup_local_https_certificate(
    state: State<'_, BackendState>,
) -> Result<LocalHttpsStatus, String> {
    #[cfg(windows)]
    { return setup_local_https_certificate_windows(&state); }
    #[cfg(all(not(target_os = "macos"), not(windows)))]
    { return Err("Trusted local HTTPS is not implemented on this platform. Use loopback-only HTTP for development; remote access must remain behind verified Tailscale HTTPS/pairing.".into()); }
    if state.status().running {
        return Err("Stop the server before changing TLS certificates.".into());
    }

    // Reuse valid material when setup is repeated. This avoids generating and
    // trusting a new local CA on every click, including after a successful setup.
    let existing_config = load_local_tls_config(&state)?;
    let existing_status = local_https_status_from_config(&existing_config);
    if existing_status.configured && existing_status.trusted {
        let mut reusable_config = existing_config;
        reusable_config.mode = "https".into();
        save_local_tls_config(&state, &reusable_config)?;
        let status = local_https_status_from_config(&reusable_config);
        if status.mode.eq_ignore_ascii_case("https") && status.configured && status.trusted {
            return Ok(status);
        }
        return Err(format!(
            "The existing local certificate could not be confirmed after selecting HTTPS: {}",
            status.message
        ));
    }

    // If a prior generated certificate is valid but Keychain trust did not
    // complete, retry trust against that same CA instead of issuing another one.
    if existing_status.configured && existing_config.generated {
        if let Some(ca_path) = existing_config.ca_certificate_path.clone() {
            let mut retry_config = existing_config;
            retry_config.mode = "https".into();
            trust_ca_file(&state, &mut retry_config, Path::new(&ca_path)).map_err(|error| {
                format!(
                    "RDT Connect kept the existing local certificate files but could not finish Keychain trust. Resolve the Keychain issue and retry setup. Detail: {error}"
                )
            })?;
            let status = local_https_status_from_config(&retry_config);
            if status.mode.eq_ignore_ascii_case("https") && status.configured && status.trusted {
                return Ok(status);
            }
            return Err(format!(
                "The existing local certificate and trust status could not be verified: {}",
                status.message
            ));
        }
    }

    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let dir = state
        .https_dir
        .join(format!("generated-{seconds}-{}", std::process::id()));
    fs::create_dir_all(&dir)
        .map_err(|e| format!("Could not create the certificate folder: {e}"))?;
    set_private_permissions(&dir, 0o700)
        .map_err(|e| format!("Could not secure the certificate folder: {e}"))?;
    let ca_key = dir.join("rdt-local-ca-key.pem");
    let ca = dir.join("rdt-local-ca.pem");
    let server_key = dir.join("rdt-local-server-key.pem");
    let server_csr = dir.join("rdt-local-server.csr");
    let cert = dir.join("rdt-local-server.pem");
    let ca_cfg = dir.join("ca.cnf");
    let server_ext = dir.join("server.ext");
    let ca_config = "[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = v3_ca\n[dn]\nCN = RDT Connect Local HTTPS CA\n[v3_ca]\nbasicConstraints = critical,CA:TRUE,pathlen:0\nkeyUsage = critical,keyCertSign,cRLSign\nsubjectKeyIdentifier = hash\nnameConstraints = critical,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255\n";
    let server_extensions = "basicConstraints = critical,CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth\nsubjectAltName = DNS:localhost,IP:127.0.0.1\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid,issuer\n";
    fs::write(&ca_cfg, ca_config).map_err(|e| format!("Could not write local CA settings: {e}"))?;
    fs::write(&server_ext, server_extensions)
        .map_err(|e| format!("Could not write server certificate extensions: {e}"))?;
    for file in [&ca_cfg, &server_ext] {
        let _ = set_private_permissions(file, 0o600);
    }
    let run = |args: Vec<String>, what: &str| -> Result<(), String> {
        let output = Command::new("/usr/bin/openssl")
            .args(args)
            .output()
            .map_err(|e| format!("Could not run macOS OpenSSL to {what}: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "Could not {what}: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(())
    };
    run(
        vec![
            "req".into(),
            "-new".into(),
            "-x509".into(),
            "-newkey".into(),
            "rsa:2048".into(),
            "-nodes".into(),
            "-days".into(),
            "3650".into(),
            "-keyout".into(),
            ca_key.display().to_string(),
            "-out".into(),
            ca.display().to_string(),
            "-subj".into(),
            "/CN=RDT Connect Local HTTPS CA".into(),
            "-config".into(),
            ca_cfg.display().to_string(),
            "-extensions".into(),
            "v3_ca".into(),
        ],
        "generate the loopback-only local certificate authority",
    )?;
    run(
        vec![
            "req".into(),
            "-new".into(),
            "-newkey".into(),
            "rsa:2048".into(),
            "-nodes".into(),
            "-keyout".into(),
            server_key.display().to_string(),
            "-out".into(),
            server_csr.display().to_string(),
            "-subj".into(),
            "/CN=127.0.0.1".into(),
        ],
        "generate the local HTTPS server key and request",
    )?;
    run(
        vec![
            "x509".into(),
            "-req".into(),
            "-in".into(),
            server_csr.display().to_string(),
            "-CA".into(),
            ca.display().to_string(),
            "-CAkey".into(),
            ca_key.display().to_string(),
            "-CAcreateserial".into(),
            "-out".into(),
            cert.display().to_string(),
            "-days".into(),
            "397".into(),
            "-sha256".into(),
            "-extfile".into(),
            server_ext.display().to_string(),
        ],
        "sign the local HTTPS server certificate",
    )?;
    for file in [&ca_key, &server_key] {
        set_private_permissions(file, 0o600)
            .map_err(|e| format!("Could not secure a generated private key: {e}"))?;
    }
    let mut config = load_local_tls_config(&state)?;
    config.mode = "https".into();
    config.certificate_path = Some(cert.display().to_string());
    config.private_key_path = Some(server_key.display().to_string());
    config.ca_certificate_path = Some(ca.display().to_string());
    config.generated = true;
    save_local_tls_config(&state, &config)?;
    if let Err(error) = trust_ca_file(&state, &mut config, &ca) {
        return Err(format!("The local certificate files were created securely, but macOS could not add the local CA to your login Keychain. In Settings, choose ‘Trust selected issuer CA’ to retry. Detail: {error}"));
    }
    let status = local_https_status_from_config(&config);
    if !status.configured || !status.trusted {
        return Err(format!(
            "Certificate setup finished but verification did not pass: {}",
            status.message
        ));
    }
    Ok(status)
}

#[tauri::command]
fn link_local_https_certificate(
    state: State<'_, BackendState>,
    certificate_path: String,
    private_key_path: String,
    ca_certificate_path: Option<String>,
) -> Result<LocalHttpsStatus, String> {
    if cfg!(windows) {
        return Err("Windows currently supports RDT-generated trusted local HTTPS only. Select Set up trusted local HTTPS; external certificate linking is intentionally disabled until Windows custom-certificate verification is implemented.".into());
    }
    if cfg!(all(not(target_os = "macos"), not(windows))) {
        return Err("Trusted local HTTPS is not implemented on this platform. Keep development HTTP loopback-only; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
    if state.status().running {
        return Err("Stop the server before changing TLS certificates.".into());
    }
    let cert = canonical_file(&certificate_path, "server certificate")?;
    let key = canonical_file(&private_key_path, "private key")?;
    let ca = match ca_certificate_path
        .as_deref()
        .filter(|s| !s.trim().is_empty())
    {
        Some(s) => Some(canonical_file(s, "root CA certificate")?),
        None => None,
    };
    let mut config = load_local_tls_config(&state)?;
    config.mode = "https".into();
    config.certificate_path = Some(cert.display().to_string());
    config.private_key_path = Some(key.display().to_string());
    config.ca_certificate_path = ca.as_ref().map(|p| p.display().to_string());
    config.generated = false;
    validate_tls_material(&config)?;
    save_local_tls_config(&state, &config)?;
    Ok(local_https_status_from_config(&config))
}

#[tauri::command]
fn trust_local_https_ca(
    state: State<'_, BackendState>,
    ca_certificate_path: Option<String>,
) -> Result<LocalHttpsStatus, String> {
    if cfg!(all(not(target_os = "macos"), not(windows))) {
        return Err("Native local CA trust management is not implemented on this platform. No system trust store was modified.".into());
    }
    if state.status().running {
        return Err("Stop the server before changing TLS trust.".into());
    }
    let mut config = load_local_tls_config(&state)?;
    if let Some(path) = ca_certificate_path
        .as_deref()
        .filter(|s| !s.trim().is_empty())
    {
        config.ca_certificate_path = Some(
            canonical_file(path, "root CA certificate")?
                .display()
                .to_string(),
        );
    }
    let ca = config
        .ca_certificate_path
        .clone()
        .ok_or_else(|| "Choose a self-signed root CA file first.".to_string())?;
    validate_tls_material(&config)?;
    trust_ca_file(&state, &mut config, Path::new(&ca))?;
    let status = local_https_status_from_config(&config);
    if !status.trusted {
        return Err(format!(
            "The CA operation completed, but operating-system trust verification still failed: {}",
            status.message
        ));
    }
    Ok(status)
}

#[tauri::command]
fn remove_rdt_https_trust(state: State<'_, BackendState>) -> Result<LocalHttpsStatus, String> {
    if cfg!(all(not(target_os = "macos"), not(windows))) {
        return Err("Removing RDT-managed CA trust is not implemented on this platform. No system trust store was modified.".into());
    }
    if state.status().running {
        return Err("Stop the server before changing TLS trust.".into());
    }
    #[cfg(windows)]
    {
        let mut config = load_local_tls_config(&state)?;
        if config.managed_trust_fingerprints.is_empty() {
            return Err("RDT Connect has no tracked CA trust entry to remove. It will not remove certificates installed independently by you or another app.".into());
        }
        let mut remaining = Vec::new();
        let mut errors = Vec::new();
        for fingerprint in config.managed_trust_fingerprints.drain(..) {
            match windows_remove_root_from_current_user_store(&fingerprint) {
                Ok(()) => {},
                Err(error) => { errors.push(format!("fingerprint {fingerprint}: {error}")); remaining.push(fingerprint); }
            }
        }
        config.managed_trust_fingerprints = remaining;
        save_local_tls_config(&state, &config)?;
        if !errors.is_empty() {
            return Err(format!("Some tracked RDT root certificates could not be removed from the current-user trust store. Failed entries remain tracked. {}", errors.join("; ")));
        }
        return Ok(local_https_status_from_config(&config));
    }
    let mut config = load_local_tls_config(&state)?;
    if config.managed_trust_fingerprints.is_empty() {
        return Err("RDT Connect has no tracked CA trust entry to remove. It will not remove certificates installed independently by you or another app.".into());
    }
    let keychain = login_keychain()?;
    let mut remaining = Vec::new();
    let mut errors = Vec::new();
    for fingerprint in config.managed_trust_fingerprints.drain(..) {
        match run_security(
            &[
                "delete-certificate",
                "-Z",
                &fingerprint,
                "-t",
                keychain.to_str().ok_or("Invalid login Keychain path")?,
            ],
            "remove an exact RDT Connect-added CA trust entry",
        ) {
            Ok(()) => {}
            Err(e) => {
                errors.push(format!("fingerprint {fingerprint}: {e}"));
                remaining.push(fingerprint);
            }
        }
    }
    config.managed_trust_fingerprints = remaining;
    save_local_tls_config(&state, &config)?;
    if !errors.is_empty() {
        return Err(format!("Some tracked trust entries could not be removed. No certificate files were deleted. {}", errors.join("; ")));
    }
    Ok(local_https_status_from_config(&config))
}

#[tauri::command]
fn set_local_transport(
    state: State<'_, BackendState>,
    mode: String,
) -> Result<LocalHttpsStatus, String> {
    if state.status().running {
        return Err("Stop the local server before changing its transport.".into());
    }
    let normalized = mode.trim().to_ascii_lowercase();
    if normalized == "https" && cfg!(all(not(target_os = "macos"), not(windows))) {
        return Err("Trusted local HTTPS is not implemented on this platform. Use HTTP only on 127.0.0.1 for development; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
    if normalized != "https" && normalized != "http" {
        return Err("Choose HTTPS or HTTP development mode.".into());
    }
    let mut config = load_local_tls_config(&state)?;
    if normalized == "https" {
        validate_tls_material(&config)?;
        if !configured_certificate_trusted(&config) {
            return Err("HTTPS is not yet trusted for 127.0.0.1. Set up trusted HTTPS or trust the selected root CA in Settings.".into());
        }
    }
    config.mode = normalized;
    save_local_tls_config(&state, &config)?;
    Ok(local_https_status_from_config(&config))
}

const RDT_MODEL_LOCK_JSON: &str = include_str!("../../../backend/rdt_model_lock.json");

fn model_lock_entry(model: &str) -> Option<Value> {
    static LOCK: OnceLock<Value> = OnceLock::new();
    let lock = LOCK.get_or_init(|| {
        serde_json::from_str(RDT_MODEL_LOCK_JSON)
            .expect("RDT model lock JSON is validated at build time")
    });
    lock.get("models")?.get(model).cloned()
}

fn model_lock_string(model: &str, key: &str) -> Option<String> {
    model_lock_entry(model)?
        .get(key)?
        .as_str()
        .map(str::to_string)
}

fn model_lock_sha256(model: &str) -> Option<String> {
    let value = model_lock_string(model, "sha256")?;
    if value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        Some(value)
    } else {
        None
    }
}

fn model_lock_revision(model: &str) -> Option<String> {
    let value = model_lock_string(model, "revision")?;
    if value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        Some(value)
    } else {
        None
    }
}

fn validate_model(model: &str) -> Result<(), String> {
    if cfg!(any(target_os = "windows", target_os = "linux")) {
        if native_ggml_spec(model).is_some() {
            return Ok(());
        }
        return Err("Choose one of the approved GGML Whisper models from the list.".into());
    }
    if model_lock_entry(model).is_some()
        && model_lock_revision(model).is_some()
        && model_lock_sha256(model).is_some()
    {
        Ok(())
    } else {
        Err("Choose one of the pinned MLX Whisper models from the list.".into())
    }
}

#[tauri::command]
fn get_platform_model_catalog() -> Vec<PlatformModelOption> {
    if cfg!(any(target_os = "windows", target_os = "linux")) {
        return vec![
            PlatformModelOption { id: "tiny".into(), name: "Tiny".into(), description: "Fastest, smallest GGML model".into(), size: "~43.5 MB".into(), recommended: false },
            PlatformModelOption { id: "base".into(), name: "Base".into(), description: "Lightweight GGML choice".into(), size: "~81.8 MB".into(), recommended: false },
            PlatformModelOption { id: "small".into(), name: "Small".into(), description: "Balanced GGML model".into(), size: "~264 MB".into(), recommended: false },
            PlatformModelOption { id: "large-v3-turbo".into(), name: "Large v3 Turbo".into(), description: "Strong balance of speed and quality".into(), size: "~574 MB".into(), recommended: true },
            PlatformModelOption { id: "large-v3".into(), name: "Large v3".into(), description: "Largest GGML model in this list".into(), size: "~1.08 GB".into(), recommended: false },
        ];
    }
    vec![
        PlatformModelOption { id: "mlx-community/whisper-tiny-mlx".into(), name: "Tiny".into(), description: "Fastest · smallest download".into(), size: "~74 MB".into(), recommended: false },
        PlatformModelOption { id: "mlx-community/whisper-base-mlx".into(), name: "Base".into(), description: "Lightweight choice".into(), size: "~144 MB".into(), recommended: false },
        PlatformModelOption { id: "mlx-community/whisper-small-mlx".into(), name: "Small".into(), description: "Middle-size model".into(), size: "~481 MB".into(), recommended: false },
        PlatformModelOption { id: "mlx-community/whisper-large-v3-turbo".into(), name: "Large v3 Turbo".into(), description: "Strong balance of speed and quality".into(), size: "~1.61 GB".into(), recommended: true },
        PlatformModelOption { id: "mlx-community/whisper-large-v3-mlx".into(), name: "Large v3".into(), description: "Largest download in this list".into(), size: "~3.08 GB".into(), recommended: false },
    ]
}

const MODEL_STORAGE_MAX_LOCATIONS: usize = 20;

fn publish_model_download_status(
    app: &AppHandle,
    store: &Arc<Mutex<ModelDownloadStatus>>,
    phase: &str,
    message: &str,
    active: bool,
) {
    let snapshot = if let Ok(mut status) = store.lock() {
        status.phase = phase.to_string();
        status.message = message.to_string();
        status.active = active;
        if phase == "verifying" {
            status.progress_percent = status.progress_percent.max(99);
        } else if phase == "complete" {
            status.progress_percent = 100;
            if let Some(total) = status.total_bytes {
                status.downloaded_bytes = status.downloaded_bytes.max(total);
            }
        }
        status.clone()
    } else {
        return;
    };
    let _ = app.emit("rdt-model-download-status", snapshot);
}

fn emit_updated_model_download_status(
    app: &AppHandle,
    store: &Arc<Mutex<ModelDownloadStatus>>,
    update: impl FnOnce(&mut ModelDownloadStatus),
) {
    let snapshot = if let Ok(mut status) = store.lock() {
        update(&mut status);
        status.clone()
    } else {
        return;
    };
    let _ = app.emit("rdt-model-download-status", snapshot);
}

fn apply_model_download_output_line(
    line: &[u8],
    app: &AppHandle,
    store: &Arc<Mutex<ModelDownloadStatus>>,
    cancel_requested: &Arc<AtomicBool>,
) {
    if cancel_requested.load(Ordering::SeqCst) {
        return;
    }
    let text = String::from_utf8_lossy(line);
    let trimmed = text.trim();

    if let Some(meta) = trimmed.strip_prefix("RDT_MODEL_DOWNLOAD_META:") {
        let parts: Vec<&str> = meta.split(':').collect();
        if parts.len() == 3 {
            if let (Ok(total), Ok(files), Ok(estimated)) = (
                parts[0].parse::<u64>(),
                parts[1].parse::<usize>(),
                parts[2].parse::<u8>(),
            ) {
                emit_updated_model_download_status(app, store, |status| {
                    status.total_bytes = if total > 0 { Some(total) } else { Some(0) };
                    status.planned_files = Some(files);
                    status.size_is_estimate = estimated != 0;
                    status.downloaded_bytes = 0;
                    status.progress_percent = 0;
                    status.phase = "downloading".into();
                    status.message = if estimated != 0 {
                        "Downloading model files · approximate total size. Progress is based on transferred bytes.".into()
                    } else {
                        "Downloading model files. Progress is based on transferred bytes.".into()
                    };
                });
            }
        }
        return;
    }

    if let Some(progress) = trimmed.strip_prefix("RDT_MODEL_DOWNLOAD_PROGRESS:") {
        let parts: Vec<&str> = progress.split(':').collect();
        if parts.len() == 3 {
            if let (Ok(downloaded), Ok(reported_total), Ok(reported_percent)) = (
                parts[0].parse::<u64>(),
                parts[1].parse::<u64>(),
                parts[2].parse::<u8>(),
            ) {
                emit_updated_model_download_status(app, store, |status| {
                    if status.total_bytes.is_none() {
                        status.total_bytes = Some(reported_total);
                    }
                    let denominator = status.total_bytes.unwrap_or(reported_total);
                    let safe_downloaded = if denominator > 0 {
                        downloaded.min(denominator)
                    } else {
                        downloaded
                    };
                    status.downloaded_bytes = status.downloaded_bytes.max(safe_downloaded);
                    let computed = if denominator > 0 {
                        ((status.downloaded_bytes as u128 * 100) / denominator as u128).min(98)
                            as u8
                    } else {
                        reported_percent.min(98)
                    };
                    status.progress_percent = status.progress_percent.max(computed).min(98);
                    status.phase = "downloading".into();
                    status.message = if status.size_is_estimate {
                        "Downloading model files · approximate total size.".into()
                    } else {
                        "Downloading model files.".into()
                    };
                });
            }
        }
        return;
    }

    if let Some(error_kind) = trimmed.strip_prefix("RDT_MODEL_DOWNLOAD_ERROR:") {
        let family = if cfg!(any(target_os = "windows", target_os = "linux")) {
            "GGML Whisper"
        } else {
            "MLX Whisper"
        };
        let code = error_kind.chars().take(48).collect::<String>();
        let explanation = match code.as_str() {
            "http_401" | "http_403" =>
                "Hugging Face denied access to the model metadata or file",
            "http_404" | "pinned_model_artifact_missing" =>
                "the pinned model artifact was not found at the expected revision",
            "http_429" =>
                "Hugging Face rate-limited the request; wait briefly and retry",
            "tls_certificate_verification_failed" =>
                "Windows could not verify the HTTPS certificate; check system date, time and trust settings",
            "tls_connection_failed" =>
                "the HTTPS connection could not be established",
            "dns_resolution_failed" =>
                "the model host could not be resolved; check internet connectivity and DNS",
            "network_timeout" =>
                "the network request timed out; check connectivity and retry",
            "network_connection_failed" =>
                "the connection to the model host failed; check connectivity and any firewall or proxy",
            "pinned_metadata_sha256_does_not_match_approved_candidate"
            | "model_sha256_mismatch"
            | "model_size_mismatch"
            | "model_content_length_mismatch" =>
                "model integrity verification failed; the file was not accepted",
            "model_metadata_missing_size_or_sha256"
            | "invalid_model_metadata"
            | "model_metadata_too_large" =>
                "the model host returned unusable metadata; no model was accepted",
            "model_in_use" =>
                "the selected model is in use by a server or another operation",
            _ =>
                "the downloader reported an error; use the displayed code to diagnose it",
        };
        let message = format!(
            "{family} model download failed: {explanation} (code: {code}).              The model is not marked Downloaded; correct the issue and retry."
        );
        publish_model_download_status(app, store, "failed", &message, true);
        return;
    }

    let Some(stage) = trimmed.strip_prefix("RDT_MODEL_DOWNLOAD_STAGE:") else {
        return;
    };
    let (phase, message) = match stage {
        "checking" => ("checking", "Checking model availability and total size…"),
        "downloading" => (
            "downloading",
            "Downloading model files. Progress is based on transferred bytes.",
        ),
        "verifying" => ("verifying", "Verifying the downloaded model files…"),
        // Keep this below 100%; the supervisor independently rechecks cache files
        // and owns process cleanup before publishing terminal success.
        "complete" => (
            "verifying",
            "Model files were verified; finishing downloader cleanup…",
        ),
        _ => return,
    };
    publish_model_download_status(app, store, phase, message, true);
}

fn consume_model_download_log(
    reader: &mut File,
    pending_line: &mut Vec<u8>,
    app: &AppHandle,
    store: &Arc<Mutex<ModelDownloadStatus>>,
    cancel_requested: &Arc<AtomicBool>,
    finalize: bool,
) -> Result<bool, String> {
    let mut saw_complete = false;
    let mut chunk = [0u8; 8192];
    loop {
        let count = reader
            .read(&mut chunk)
            .map_err(|error| format!("Could not read model-download progress log: {error}"))?;
        if count == 0 {
            break; // Regular-file EOF is non-blocking; a child cannot hold this open.
        }
        for byte in &chunk[..count] {
            if *byte == b'\n' || *byte == b'\r' {
                if !pending_line.is_empty() {
                    if String::from_utf8_lossy(pending_line).trim()
                        == "RDT_MODEL_DOWNLOAD_STAGE:complete"
                    {
                        saw_complete = true;
                    }
                    apply_model_download_output_line(pending_line, app, store, cancel_requested);
                    pending_line.clear();
                }
            } else if pending_line.len() < 1024 {
                pending_line.push(*byte);
            }
        }
    }
    if finalize && !pending_line.is_empty() {
        if String::from_utf8_lossy(pending_line).trim() == "RDT_MODEL_DOWNLOAD_STAGE:complete" {
            saw_complete = true;
        }
        apply_model_download_output_line(pending_line, app, store, cancel_requested);
        pending_line.clear();
    }
    Ok(saw_complete)
}

fn create_model_download_log() -> Result<(PathBuf, File), String> {
    let temp = std::env::temp_dir();
    let stamp = unix_epoch_millis();
    for attempt in 0..16u8 {
        let path = temp.join(format!(
            "rdt-connect-model-download-{}-{stamp}-{attempt}.log",
            std::process::id()
        ));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(file) => return Ok((path, file)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "Could not create a private model-download progress log: {error}"
                ));
            }
        }
    }
    Err("Could not allocate a unique model-download progress log. Close any stale RDT Connect download attempt and retry.".into())
}

fn clear_model_download_log_path(store: &Arc<Mutex<Option<PathBuf>>>, expected_path: &Path) {
    if let Ok(mut current) = store.lock() {
        if current.as_deref() == Some(expected_path) {
            *current = None;
        }
    }
    // If a child still has the descriptor open, unlinking the name keeps the
    // ephemeral machine-readable log private and bounds named temporary files.
    let _ = fs::remove_file(expected_path);
}

fn capture_model_download_identity(
    pid: u32,
    expected_exe: &Path,
) -> Result<ProcessSnapshot, String> {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let snapshot = process_snapshot();
        if snapshot.is_empty() {
            return Err("The process table could not be inspected, so downloader ownership could not be established.".into());
        }
        if let Some(leader) = snapshot.iter().find(|item| item.pid == pid) {
            if leader.pgid != pid
                || leader.stat.starts_with('Z')
                || !command_is_expected_backend(&leader.command, expected_exe)
            {
                return Err("The spawned downloader's process group or executable did not match the packaged backend.".into());
            }
            return Ok(leader.clone());
        }
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(25));
    }
    Err("The downloader exited before its process identity could be recorded. No process-group signal was sent.".into())
}

fn approximate_directory_bytes(path: &Path) -> u64 {
    let metadata = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(_) => return 0,
    };
    if metadata.file_type().is_symlink() {
        return 0;
    }
    if metadata.is_file() {
        return metadata.len();
    }
    if !metadata.is_dir() {
        return 0;
    }
    let mut total = 0u64;
    let entries = match fs::read_dir(path) {
        Ok(v) => v,
        Err(_) => return 0,
    };
    for entry in entries.flatten() {
        total = total.saturating_add(approximate_directory_bytes(&entry.path()));
    }
    total
}

#[tauri::command]
fn get_model_download_status(state: State<'_, BackendState>) -> ModelDownloadStatus {
    state
        .model_download_status
        .lock()
        .map(|s| s.clone())
        .unwrap_or_default()
}

#[tauri::command]
fn start_model_download(
    app: AppHandle,
    state: State<'_, BackendState>,
    model: String,
) -> Result<ModelDownloadStatus, String> {
    validate_model(&model)?;
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let server_status = state.status();
    if server_status.running {
        let Some(active_model) = server_status.model.as_deref() else {
            return Err(
                "Cannot safely download a model while the running server's active model is unknown."
                    .into(),
            );
        };
        if active_model == model.as_str() {
            return Err(
                "Cannot download the active model cache while the local server is running.".into(),
            );
        }
    }
    {
        let current = state
            .model_download_status
            .lock()
            .map_err(|_| "Model-download state is unavailable.".to_string())?;
        if current.active {
            return Err("A model download is already in progress.".into());
        }
    }
    let cache_status = make_model_storage_status(&state, &model)?;
    if cache_status.model_cached {
        let result = ModelDownloadStatus {
            active: false,
            model: Some(model),
            destination_root: Some(cache_status.active_root),
            phase: "complete".into(),
            message: if cfg!(any(target_os = "windows", target_os = "linux")) {
                "A verified local GGML Whisper model is already available; no download is needed.".to_string()
            } else {
                "A verified local MLX model copy is already available; no download is needed.".to_string()
            },
            progress_percent: 100,
            downloaded_bytes: 0,
            total_bytes: Some(0),
            planned_files: Some(0),
            size_is_estimate: false,
        };
        *state
            .model_download_status
            .lock()
            .map_err(|_| "Model-download state is unavailable.".to_string())? = result.clone();
        let _ = app.emit("rdt-model-download-status", result.clone());
        return Ok(result);
    }
    let (root, _) = resolve_model_storage_root(&state, &model)?;
    let hub_cache = root.join("hub");
    let xet_cache = root.join("xet");
    let exe = backend_executable_path(&state.resource_dir);
    if !exe.is_file() {
        return Err(format!("Bundled model downloader was not found at {}. Run the platform-specific resource preparation script first.", exe.display()));
    }
    let (log_path, log_writer) = create_model_download_log()?;
    state
        .model_download_cancel_requested
        .store(false, Ordering::SeqCst);
    let initial = ModelDownloadStatus {
        active: true,
        model: Some(model.clone()),
        destination_root: Some(root.display().to_string()),
        phase: "checking".into(),
        message: "Checking model availability and total size…".into(),
        progress_percent: 0,
        downloaded_bytes: 0,
        total_bytes: None,
        planned_files: None,
        size_is_estimate: false,
    };
    *state
        .model_download_status
        .lock()
        .map_err(|_| "Model-download state is unavailable.".to_string())? = initial.clone();
    let _ = app.emit("rdt-model-download-status", initial.clone());

    let mut command = Command::new(&exe);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    command
        .arg("--download-model")
        .arg(&model)
        .env_remove("HF_TOKEN")
        .env_remove("HF_READ_TOKEN")
        .env_remove("HF_WRITE_TOKEN")
        .env_remove("HUGGING_FACE_HUB_TOKEN")
        .env_remove("WRITE_TOKEN")
        .env_remove("PYTHONPATH")
        // Model downloads must use the official Hugging Face origin. Do not inherit
        // a shell-defined HF_ENDPOINT that silently redirects every model request.
        .env("HF_ENDPOINT", "https://huggingface.co")
        // Hugging Face reads these before import. Bound idle network stalls while
        // allowing slower connections to large MLX weight files to make progress.
        .env("HF_HUB_DOWNLOAD_TIMEOUT", "60")
        .env("HF_HUB_ETAG_TIMEOUT", "20")
        .env("HF_HOME", &root)
        .env("HF_HUB_CACHE", &hub_cache)
        .env("HUGGINGFACE_HUB_CACHE", &hub_cache)
        .env("TRANSFORMERS_CACHE", &hub_cache)
        .env("HF_XET_CACHE", &xet_cache)
        .env("HF_HUB_DISABLE_XET", "1")
        .env("HF_HUB_DISABLE_PROGRESS_BARS", "0")
        .env("PYTHONUNBUFFERED", "1")
        .current_dir(exe.parent().unwrap_or(Path::new(".")))
        // A regular file cannot stay open awaiting EOF from a multiprocessing helper.
        // The supervisor tails it non-blockingly and owns all child cleanup.
        .stdout(Stdio::from(log_writer))
        .stderr(Stdio::null());
    #[cfg(any(windows, target_os = "linux"))]
    command.env("RDT_GGML_MODEL_DIR", root.join("ggml"));
    #[cfg(unix)]
    command.process_group(0);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = fs::remove_file(&log_path);
            publish_model_download_status(
                &app,
                &state.model_download_status,
                "failed",
                &format!("Could not start the model downloader: {error}"),
                false,
            );
            return Err(format!("Could not start the model downloader: {error}"));
        }
    };
    let pid = child.id();
    let identity = match capture_model_download_identity(pid, &exe) {
        Ok(identity) => identity,
        Err(error) => {
            // Child::kill targets this exact spawned process handle, not a port or a
            // process name. If identity could not be established, never signal a PGID.
            let _ = child.kill();
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let _ = fs::remove_file(&log_path);
            publish_model_download_status(
                &app,
                &state.model_download_status,
                "failed",
                &format!("Could not safely register the model downloader: {error}"),
                false,
            );
            return Err(format!(
                "Could not safely register the model downloader: {error}"
            ));
        }
    };
    if let Ok(mut download_pid) = state.model_download_pid.lock() {
        *download_pid = Some(identity.clone());
    }
    if let Ok(mut stored_log) = state.model_download_log_path.lock() {
        *stored_log = Some(log_path.clone());
    }

    let app_thread = app.clone();
    let status_store = Arc::clone(&state.model_download_status);
    let pid_store = Arc::clone(&state.model_download_pid);
    let log_path_store = Arc::clone(&state.model_download_log_path);
    let cancel_requested = Arc::clone(&state.model_download_cancel_requested);
    let expected_exe = exe.clone();
    let model_for_worker = model.clone();
    let root_for_worker = root.clone();
    thread::spawn(move || {
        let mut log_reader = match File::open(&log_path) {
            Ok(file) => file,
            Err(error) => {
                // A read failure is terminal only after the exact spawned group is
                // stopped; otherwise keep active state so Cancel can retry safely.
                if let Err(stop_error) =
                    finish_stopping_model_download_group(&identity, &expected_exe)
                {
                    let message = format!("The download progress log could not be opened ({error}); cleanup is unconfirmed ({stop_error}). Select Try cancel again.");
                    publish_model_download_status(
                        &app_thread,
                        &status_store,
                        "failed",
                        &message,
                        true,
                    );
                    clear_model_download_log_path(&log_path_store, &log_path);
                    return;
                }
                if let Ok(mut current) = pid_store.lock() {
                    *current = None;
                }
                publish_model_download_status(
                    &app_thread,
                    &status_store,
                    "failed",
                    "The model downloader's progress log could not be opened. Retry the download.",
                    false,
                );
                clear_model_download_log_path(&log_path_store, &log_path);
                return;
            }
        };
        let mut pending_line = Vec::with_capacity(512);
        let mut complete_seen_at: Option<Instant> = None;
        let mut exit_status = None;
        let mut supervisor_error: Option<String> = None;
        let mut recovered_after_complete = false;

        loop {
            match consume_model_download_log(
                &mut log_reader,
                &mut pending_line,
                &app_thread,
                &status_store,
                &cancel_requested,
                false,
            ) {
                Ok(saw_complete) => {
                    if saw_complete && complete_seen_at.is_none() {
                        complete_seen_at = Some(Instant::now());
                    }
                }
                Err(error) => {
                    supervisor_error = Some(error);
                    break;
                }
            }

            match child.try_wait() {
                Ok(Some(status)) => {
                    exit_status = Some(status);
                    // Drain the final log data so the completion status is applied.
                    // The child has already exited, so the grace-period timestamp is
                    // not consulted again on this loop path.
                    let _ = consume_model_download_log(
                        &mut log_reader,
                        &mut pending_line,
                        &app_thread,
                        &status_store,
                        &cancel_requested,
                        true,
                    );
                    break;
                }
                Ok(None) => {}
                Err(error) => {
                    supervisor_error =
                        Some(format!("Could not monitor the model downloader: {error}"));
                    break;
                }
            }

            // COMPLETE is emitted only after weight SHA-256 validation and the verified
            // cache marker are durable. Give normal interpreter teardown 1.5s; if still
            // alive, independently verify the pinned cache and terminate only this exact
            // isolated process group. No unbounded pipe EOF wait or arbitrary 8s stall.
            if !cancel_requested.load(Ordering::SeqCst)
                && complete_seen_at.is_some_and(|at| at.elapsed() >= Duration::from_millis(1500))
            {
                let cache_valid = model_cache_has_weights(&root_for_worker, &model_for_worker);
                let cache_valid = cache_valid && model_cache_fully_verified(&root_for_worker, &model_for_worker);
                match finish_stopping_model_download_group(&identity, &expected_exe) {
                    Ok(()) => {
                        let exit_deadline = Instant::now() + Duration::from_secs(2);
                        loop {
                            match child.try_wait() {
                                Ok(Some(status)) => {
                                    exit_status = Some(status);
                                    recovered_after_complete = true;
                                    break;
                                }
                                Ok(None) if Instant::now() < exit_deadline => {
                                    thread::sleep(Duration::from_millis(50));
                                }
                                Ok(None) => {
                                    supervisor_error = Some("The validated model is cached, but the downloader did not exit after bounded process cleanup. Select Try cancel again; RDT Connect has not claimed the process stopped.".into());
                                    break;
                                }
                                Err(error) => {
                                    supervisor_error = Some(format!("The validated model is cached, but downloader exit could not be confirmed: {error}. Select Try cancel again."));
                                    break;
                                }
                            }
                            if supervisor_error.is_some() || exit_status.is_some() {
                                break;
                            }
                        }
                        if exit_status.is_some() {
                            // Do not trust the progress marker alone. Final cache
                            // validation below decides whether 100% is justified.
                            recovered_after_complete = cache_valid;
                        }
                    }
                    Err(error) => {
                        supervisor_error = Some(format!("The downloader announced completion but cleanup could not be confirmed: {error}. Select Try cancel again."));
                    }
                }
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }

        if let Some(error) = supervisor_error {
            // Keep active=true and the process identity while cleanup is unconfirmed;
            // the UI offers Try cancel again rather than enabling another download.
            publish_model_download_status(&app_thread, &status_store, "failed", &error, true);
            clear_model_download_log_path(&log_path_store, &log_path);
            return;
        }

        // The downloader's parent can exit while a multiprocessing resource tracker
        // still owns the process-group membership. Close out that exact group before
        // releasing the UI or allowing a second model download.
        if let Err(error) = finish_stopping_model_download_group(&identity, &expected_exe) {
            let message = format!("The downloader exited, but cleanup of its verified process group is not confirmed: {error}. Select Try cancel again.");
            publish_model_download_status(&app_thread, &status_store, "failed", &message, true);
            clear_model_download_log_path(&log_path_store, &log_path);
            return;
        }

        let canceled = cancel_requested.swap(false, Ordering::SeqCst);
        let cache_valid = model_cache_has_weights(&root_for_worker, &model_for_worker);
                let cache_valid = cache_valid && model_cache_fully_verified(&root_for_worker, &model_for_worker);
        if let Ok(mut current) = pid_store.lock() {
            if current.as_ref().is_some_and(|recorded| {
                recorded.pid == identity.pid
                    && recorded.pgid == identity.pgid
                    && recorded.started == identity.started
                    && recorded.command == identity.command
            }) {
                *current = None;
            }
        }

        if cache_valid {
            let family = if cfg!(any(target_os = "windows", target_os = "linux")) { "GGML Whisper" } else { "MLX Whisper" };
            let message = if recovered_after_complete {
                format!("The {family} model files were fully downloaded and verified. RDT Connect recovered the downloader's stalled final shutdown. The model is ready to use.")
            } else if canceled {
                format!("The {family} model files finished downloading and passed verification before cancellation completed. The model is ready to use.")
            } else if exit_status.is_some_and(|status| status.success()) {
                format!("Download complete. The verified {family} model is ready to use.")
            } else {
                format!("The downloader ended unexpectedly, but the {family} model files are complete and passed verification. The model is ready to use.")
            };
            publish_model_download_status(&app_thread, &status_store, "complete", &message, false);
        } else if canceled {
            publish_model_download_status(
                &app_thread,
                &status_store,
                "cancelled",
                "Download canceled. Only complete, verified model files will be shown as Downloaded; valid completed files remain reusable for retry.",
                false,
            );
        } else {
            let family = if cfg!(any(target_os = "windows", target_os = "linux")) {
                "GGML Whisper"
            } else {
                "MLX Whisper"
            };
            let previous_message = status_store
                .lock()
                .map(|status| status.message.clone())
                .unwrap_or_default();

            // The line parser records a safe, actionable downloader error.
            // Do not hide that error when the child process exits.
            let message = if previous_message.contains(" model download failed:") {
                previous_message
            } else if recovered_after_complete {
                format!(
                    "The downloader stalled after announcing completion, but independent pinned {family} cache verification failed. The model is not marked Downloaded; retry the download."
                )
            } else if exit_status.as_ref().is_some_and(|status| status.success()) {
                format!(
                    "The downloader exited successfully, but the {family} cache is incomplete or invalid. The model is not marked Downloaded; retry to finish missing files."
                )
            } else {
                let exit_code = exit_status
                    .as_ref()
                    .and_then(|status| status.code())
                    .map(|code| code.to_string())
                    .unwrap_or_else(|| "unknown".into());
                format!(
                    "Model download did not finish with a complete, verified {family} cache (downloader exit code: {exit_code}). The model is not marked Downloaded; check connectivity and retry."
                )
            };
            publish_model_download_status(
                &app_thread, &status_store, "failed", &message, false
            );
        }
        clear_model_download_log_path(&log_path_store, &log_path);
    });
    Ok(initial)
}

// Model downloads use their own process group (CommandExt::process_group(0)). The
// captured leader identity includes start time, PGID and full command. Helper processes
// such as Python's resource_tracker are permitted only within that isolated group.
fn model_download_group_members_are_owned(
    identity: &ProcessSnapshot,
    expected_exe: &Path,
) -> Result<bool, String> {
    if identity.pid == 0 || identity.pgid != identity.pid {
        return Err("The stored downloader identity is not an isolated process group.".into());
    }
    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return Err(
            "Could not inspect the process table; no further cancellation signal was sent.".into(),
        );
    }
    let members: Vec<&ProcessSnapshot> = snapshot
        .iter()
        .filter(|item| item.pgid == identity.pgid && !item.stat.starts_with('Z'))
        .collect();
    if members.is_empty() {
        #[cfg(windows)]
        if !windows_descendant_pids(identity.pid, &snapshot).is_empty() {
            return Err("The Windows downloader leader exited while descendant processes remain. RDT Connect cannot safely identify or terminate those helpers yet; no cancellation success was reported.".into());
        }
        // No captured group or descendant is visible. Do not target a reused PID.
        return Ok(false);
    }
    if let Some(leader) = snapshot.iter().find(|item| item.pid == identity.pid) {
        if leader.pgid != identity.pgid
            || leader.started != identity.started
            || leader.command != identity.command
        {
            return Err("The downloader PID now identifies a different process; no process-group signal was sent.".into());
        }
    }
    // The group was created exclusively for this child with process_group(0).
    // Legitimate helpers such as Python's multiprocessing.resource_tracker have a
    // different command line but inherit that isolated PGID. Validate the original
    // leader while it exists; a still-populated PGID cannot be reused after the
    // leader exits until every member is gone. Never signal by port/name alone.
    if let Some(leader) = snapshot.iter().find(|item| item.pid == identity.pid) {
        if !command_is_expected_backend(&leader.command, expected_exe) {
            return Err("The original downloader leader no longer matches the packaged backend; no process-group signal was sent.".into());
        }
    }
    Ok(true)
}

// Send TERM to only the verified download process group, allow graceful shutdown,
// then revalidate identity before escalating to KILL. Every wait is bounded.
fn finish_stopping_model_download_group(
    identity: &ProcessSnapshot,
    expected_exe: &Path,
) -> Result<(), String> {
    if !model_download_group_members_are_owned(identity, expected_exe)? {
        return Ok(());
    }
    let group_id = identity.pgid;
    if let Err(error) = send_signal_checked(-(group_id as i32), SIGNAL_TERM) {
        if !process_group_alive(group_id) { return Ok(()); }
        return Err(format!("Could not request graceful cleanup of the verified downloader group: {error}"));
    }

    let term_deadline = Instant::now() + Duration::from_millis(700);
    loop {
        if !model_download_group_members_are_owned(identity, expected_exe)? {
            return Ok(());
        }
        if Instant::now() >= term_deadline {
            break;
        }
        thread::sleep(Duration::from_millis(75));
    }

    // Ownership is deliberately checked immediately before SIGKILL.
    if !model_download_group_members_are_owned(identity, expected_exe)? {
        return Ok(());
    }
    if let Err(error) = send_signal_checked(-(group_id as i32), SIGNAL_KILL) {
        if !process_group_alive(group_id) { return Ok(()); }
        return Err(format!("Could not force-stop the verified downloader group: {error}"));
    }

    let final_deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if !model_download_group_members_are_owned(identity, expected_exe)? {
            return Ok(());
        }
        if Instant::now() >= final_deadline {
            break;
        }
        thread::sleep(Duration::from_millis(75));
    }
    Err("The verified download process group is still active after bounded termination.".into())
}

#[tauri::command]
fn cancel_model_download(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<ModelDownloadStatus, String> {
    // Serialize against the downloader's initial spawn so Cancel cannot race the
    // small window where active=true but the isolated child PID is not yet recorded.
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let mut snapshot = state
        .model_download_status
        .lock()
        .map_err(|_| "Model-download state is unavailable.".to_string())?
        .clone();
    if !snapshot.active {
        return Ok(snapshot);
    }
    state
        .model_download_cancel_requested
        .store(true, Ordering::SeqCst);
    snapshot.phase = "canceling".into();
    snapshot.message = "Stopping the download and its helper workers…".into();

    let identity = state
        .model_download_pid
        .lock()
        .map_err(|_| "Model-download process state is unavailable.".to_string())?
        .clone();
    if let Some(identity) = identity {
        let expected_exe = backend_executable_path(&state.resource_dir);
        match model_download_group_members_are_owned(&identity, &expected_exe) {
            Ok(true) => {
                // The group was created specifically for this download. Its live
                // members must all match our bundled executable before signaling.
                if let Err(error) = send_signal_checked(-(identity.pgid as i32), SIGNAL_TERM) {
                    if process_group_alive(identity.pgid) {
                        snapshot.phase = "failed".into();
                        snapshot.message = format!("Could not request cancellation: {error}. Select Try cancel again to retry.");
                        if let Ok(mut status) = state.model_download_status.lock() { *status = snapshot.clone(); }
                        let _ = app.emit("rdt-model-download-status", snapshot.clone());
                        return Ok(snapshot);
                    }
                }
            }
            Ok(false) => {
                // The downloader already exited. The waiting thread will publish its
                // terminal status; don't signal a potentially reused process-group ID.
            }
            Err(error) => {
                snapshot.phase = "failed".into();
                snapshot.message = format!("Cancellation could not verify the download process safely: {error} Select Try cancel again after checking Developer Options → Diagnostics.");
                if let Ok(mut status) = state.model_download_status.lock() {
                    *status = snapshot.clone();
                }
                let _ = app.emit("rdt-model-download-status", snapshot.clone());
                return Ok(snapshot);
            }
        }

        if let Ok(mut status) = state.model_download_status.lock() {
            *status = snapshot.clone();
        }
        let _ = app.emit("rdt-model-download-status", snapshot.clone());

        let app_for_escalation = app.clone();
        let status_store = Arc::clone(&state.model_download_status);
        let pid_store = Arc::clone(&state.model_download_pid);
        let log_path_store = Arc::clone(&state.model_download_log_path);
        let cancel_requested = Arc::clone(&state.model_download_cancel_requested);
        let identity_for_escalation = identity.clone();
        let model = snapshot.model.clone();
        let destination_root = snapshot.destination_root.clone();
        thread::spawn(move || {
            // The foreground command already requested TERM. Allow bounded grace,
            // then revalidate the exact stored identity before escalation.
            thread::sleep(Duration::from_millis(900));
            let still_this_attempt = cancel_requested.load(Ordering::SeqCst)
                && pid_store
                    .lock()
                    .map(|current| {
                        current.as_ref().is_some_and(|captured| {
                            captured.pid == identity_for_escalation.pid
                                && captured.pgid == identity_for_escalation.pgid
                                && captured.started == identity_for_escalation.started
                                && captured.command == identity_for_escalation.command
                        })
                    })
                    .unwrap_or(false)
                && status_store
                    .lock()
                    .map(|status| status.active && status.phase == "canceling")
                    .unwrap_or(false);
            if !still_this_attempt {
                return;
            }

            if let Err(error) =
                finish_stopping_model_download_group(&identity_for_escalation, &expected_exe)
            {
                let updated = if let Ok(mut status) = status_store.lock() {
                    if !status.active || status.phase != "canceling" {
                        return;
                    }
                    // Keep active=true so Start/Delete cannot race a possibly-live process.
                    status.phase = "failed".into();
                    status.message = format!("Cancellation could not be confirmed: {error} Select Try cancel again; no unverified process was signaled.");
                    Some(status.clone())
                } else {
                    None
                };
                if let Some(updated) = updated {
                    let _ = app_for_escalation.emit("rdt-model-download-status", updated);
                }
                return;
            }

            // If the worker already published a terminal result, do not overwrite it.
            let still_this_attempt = cancel_requested.load(Ordering::SeqCst)
                && pid_store
                    .lock()
                    .map(|current| {
                        current.as_ref().is_some_and(|captured| {
                            captured.pid == identity_for_escalation.pid
                                && captured.pgid == identity_for_escalation.pgid
                                && captured.started == identity_for_escalation.started
                                && captured.command == identity_for_escalation.command
                        })
                    })
                    .unwrap_or(false)
                && status_store
                    .lock()
                    .map(|status| status.active && status.phase == "canceling")
                    .unwrap_or(false);
            if !still_this_attempt {
                return;
            }

            let cache_valid = model
                .as_deref()
                .zip(destination_root.as_deref())
                .is_some_and(|(model, root)| model_cache_fully_verified(Path::new(root), model));
            cancel_requested.store(false, Ordering::SeqCst);
            if let Ok(mut current) = pid_store.lock() {
                if current.as_ref().is_some_and(|captured| {
                    captured.pid == identity_for_escalation.pid
                        && captured.started == identity_for_escalation.started
                }) {
                    *current = None;
                }
            }
            if cache_valid {
                publish_model_download_status(
                    &app_for_escalation,
                    &status_store,
                    "complete",
                    "The MLX model files are complete and verified. The downloader has been safely stopped and the model is ready to use.",
                    false,
                );
            } else {
                publish_model_download_status(
                    &app_for_escalation,
                    &status_store,
                    "cancelled",
                    "Download canceled. Only complete, verified MLX model files will be shown as Downloaded; completed files can be reused on retry.",
                    false,
                );
            }
            let remaining_log = log_path_store.lock().ok().and_then(|mut path| path.take());
            if let Some(path) = remaining_log {
                let _ = fs::remove_file(path);
            }
        });
    } else {
        // With the lifecycle lock held, no PID means the download never established
        // a process or it finished between status and PID reads. Let the caller retry
        // rather than claiming success or leaving the button disabled forever.
        snapshot.phase = "failed".into();
        snapshot.message = "No active downloader process was recorded. Refresh status, then retry Cancel or retry the download.".into();
        if let Ok(mut status) = state.model_download_status.lock() {
            *status = snapshot.clone();
        }
        let _ = app.emit("rdt-model-download-status", snapshot.clone());
        return Ok(snapshot);
    }
    Ok(snapshot)
}

#[tauri::command]
fn delete_rdt_model_caches(
    state: State<'_, BackendState>,
    models: Vec<String>,
) -> Result<ModelCacheDeleteResult, String> {
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let server_status = state.status();
    if server_status.running && server_status.model.as_deref().is_none() {
        return Err(
            "Cannot safely delete model caches while the running server's active model is unknown."
                .into(),
        );
    }
    if server_status.running
        && models
            .iter()
            .any(|model| server_status.model.as_deref() == Some(model.as_str()))
    {
        return Err(
            "Cannot delete the active model cache while the local server is running.".into(),
        );
    }
    if state
        .model_download_status
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .active
    {
        return Err(
            "Finish or cancel the active model download before deleting cached models.".into(),
        );
    }
    if state
        .model_download_status
        .lock()
        .map(|s| s.active)
        .unwrap_or(true)
    {
        return Err(
            "Wait for the model download to finish or cancel it before deleting cached models."
                .into(),
        );
    }
    if models.is_empty() {
        return Err("Select at least one downloaded RDT Connect model to delete.".into());
    }
    let model_ids = platform_model_ids();
    if models.iter().any(|model| !model_ids.contains(&model.as_str())) {
        return Err("The deletion request included an unsupported model ID.".into());
    }
    let selected_models: Vec<&str> = models.iter().map(String::as_str).collect();
    let roots = load_model_storage_locations(&state)?;
    if cfg!(any(target_os = "windows", target_os = "linux")) {
        let mut deleted = 0usize;
        let mut bytes = 0u64;
        let mut checked = 0usize;
        let mut failures = Vec::new();
        for root_text in &roots {
            let root = Path::new(root_text);
            if !root.is_dir() { continue; }
            checked += 1;
            let ggml = root.join("ggml");
            let canonical_root = match fs::canonicalize(&ggml) { Ok(p) => p, Err(_) => continue };
            for model in selected_models.iter().copied().filter(|id| native_ggml_spec(id).is_some()) {
                let (filename, _, _) = native_ggml_spec(model).expect("filtered spec");
                let artifact = ggml.join(filename);
                let sidecar = artifact.with_file_name(format!("{filename}.rdt-verified.json"));
                let meta = match fs::symlink_metadata(&artifact) { Ok(m) => m, Err(_) => continue };
                if meta.file_type().is_symlink() || !meta.is_file() { failures.push(format!("Skipped unsafe GGML entry under {}", root.display())); continue; }
                let canonical = match fs::canonicalize(&artifact) { Ok(p) => p, Err(_) => continue };
                if canonical.parent() != Some(canonical_root.as_path()) { failures.push(format!("Skipped out-of-root GGML entry under {}", root.display())); continue; }
                match fs::remove_file(&artifact) {
                    Ok(()) => { let _ = fs::remove_file(&sidecar); deleted += 1; bytes = bytes.saturating_add(meta.len()); }
                    Err(e) => failures.push(format!("Could not remove cached GGML model under {}: {e}", root.display())),
                }
                let _ = fs::remove_file(artifact.with_file_name(format!("{filename}.part")));
            }
        }
        let freed = if bytes >= 1_000_000_000 { format!("about {:.2} GB", bytes as f64 / 1_000_000_000.0) } else if bytes >= 1_000_000 { format!("about {:.0} MB", bytes as f64 / 1_000_000.0) } else { format!("about {} KB", bytes / 1_000) };
        let message = if deleted == 0 && failures.is_empty() { "No verified RDT Connect GGML model files were found in configured storage roots.".into() } else if failures.is_empty() { format!("Removed {deleted} RDT Connect GGML model file(s) ({freed}); unrelated files were left unchanged.") } else { format!("Removed {deleted} GGML model file(s) ({freed}) with {} safety/removal warning(s).", failures.len()) };
        return Ok(ModelCacheDeleteResult { deleted_repositories: deleted, locations_checked: checked, approximate_bytes_freed: bytes, message });
    }
    const MODEL_IDS: [&str; 5] = [
        "mlx-community/whisper-large-v3-turbo",
        "mlx-community/whisper-large-v3-mlx",
        "mlx-community/whisper-small-mlx",
        "mlx-community/whisper-base-mlx",
        "mlx-community/whisper-tiny-mlx",
    ];
    let mut deleted = 0usize;
    let mut bytes = 0u64;
    let mut checked = 0usize;
    let mut failures: Vec<String> = Vec::new();
    for root_text in &roots {
        let root = Path::new(root_text);
        if !root.is_dir() {
            continue;
        }
        checked += 1;
        let hub = root.join("hub");
        if !hub.is_dir() {
            continue;
        }
        for model in MODEL_IDS
            .into_iter()
            .filter(|model| selected_models.contains(model))
        {
            let key = format!("models--{}", model.replace('/', "--"));
            let repo = hub.join(key);
            let metadata = match fs::symlink_metadata(&repo) {
                Ok(m) => m,
                Err(_) => continue,
            };
            // Skip a user-created repository symlink; never follow it or remove its target.
            if metadata.file_type().is_symlink() {
                failures.push(format!(
                    "Skipped a symlinked cache entry in {}",
                    root.display()
                ));
                continue;
            }
            if !metadata.is_dir() {
                continue;
            }
            let size = approximate_directory_bytes(&repo);
            match fs::remove_dir_all(&repo) {
                Ok(()) => {
                    deleted += 1;
                    bytes = bytes.saturating_add(size);
                }
                Err(error) => failures.push(format!(
                    "Could not remove a Whisper cache entry from {}: {}",
                    root.display(),
                    error
                )),
            }
        }
    }
    let freed = if bytes >= 1_000_000_000 {
        format!("about {:.2} GB", bytes as f64 / 1_000_000_000.0)
    } else if bytes >= 1_000_000 {
        format!("about {:.0} MB", bytes as f64 / 1_000_000.0)
    } else {
        format!("about {} KB", bytes / 1_000)
    };
    let message = if deleted == 0 && failures.is_empty() {
        "No cached RDT Connect MLX Whisper model entries were found in available configured storage roots.".into()
    } else if failures.is_empty() {
        if deleted == 1 {
            format!("Removed {deleted} RDT Connect MLX Whisper cache entry ({freed}). Other model repositories and browser caches were left unchanged.")
        } else {
            format!("Removed {deleted} RDT Connect MLX Whisper cache entries ({freed}). Other model repositories and browser caches were left unchanged.")
        }
    } else {
        let failure_count = failures.len();
        if deleted == 1 {
            format!("Removed {deleted} cache entry ({freed}). Removal failures: {failure_count}. Review storage paths and permissions.")
        } else {
            format!("Removed {deleted} cache entries ({freed}). Removal failures: {failure_count}. Review storage paths and permissions.")
        }
    };
    Ok(ModelCacheDeleteResult {
        deleted_repositories: deleted,
        locations_checked: checked,
        approximate_bytes_freed: bytes,
        message,
    })
}

#[tauri::command]
fn open_easywhisper_model_manager(browser_id: Option<String>) -> Result<(), String> {
    let url = "https://rolandoristech.github.io/rdt-voice/?rdt_manage_easywhisper_models=1";
    #[cfg(target_os = "macos")]
    {
        let selected_browser = browser_id.as_deref().unwrap_or("default");
        let mut command = Command::new("/usr/bin/open");
        if selected_browser != "default" {
            let bundle_id = browser_bundle_id(selected_browser).ok_or_else(|| "Choose a supported browser from the Local Server browser list.".to_string())?;
            if !browser_app_is_installed(selected_browser) {
                return Err("The selected browser is no longer installed. Choose another browser and retry.".into());
            }
            command.arg("-b").arg(bundle_id);
        }
        command.arg(url).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            .map(|_| ()).map_err(|e| format!("Could not open the model manager: {e}"))
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(url, browser_id.as_deref())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = browser_id; // Linux uses the desktop's registered default HTTP(S) handler.
        Command::new("xdg-open").arg(url).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            .map(|_| ()).map_err(|e| format!("Could not open the model manager in the default browser: {e}"))
    }
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ModelStorageConfig {
    locations: Vec<String>,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ModelStorageStatus {
    model: String,
    locations: Vec<String>,
    locations_availability: Vec<String>,
    active_root: String,
    model_cached: bool,
    server_running: bool,
}

fn default_model_storage_locations() -> Result<Vec<String>, String> {
    #[cfg(target_os = "macos")]
    let root = home_dir()?.join("models").join("huggingface");
    #[cfg(target_os = "windows")]
    let root = application_support_dir("RDT Connect")?.join("Models");
    #[cfg(all(unix, not(target_os = "macos")))]
    let root = application_support_dir("RDT Connect")?.join("Models");
    Ok(vec![root.display().to_string()])
}
fn load_model_storage_locations(state: &BackendState) -> Result<Vec<String>, String> {
    if !state.model_storage_config_path.is_file() {
        return default_model_storage_locations();
    }
    let raw = fs::read(&state.model_storage_config_path)
        .map_err(|e| format!("Could not read model-storage settings: {e}"))?;
    let config: ModelStorageConfig = serde_json::from_slice(&raw)
        .map_err(|e| format!("Model-storage settings are not valid JSON: {e}"))?;
    if config.locations.is_empty() || config.locations.len() > MODEL_STORAGE_MAX_LOCATIONS {
        return Err(format!("Model-storage settings must contain between 1 and {MODEL_STORAGE_MAX_LOCATIONS} locations. Open Settings and correct the list."));
    }
    if config
        .locations
        .iter()
        .any(|p| !Path::new(p.as_str()).is_absolute())
    {
        return Err("Model-storage settings contain a non-absolute path. Open Settings and select the folder again.".into());
    }
    Ok(config.locations)
}

fn model_snapshot_file_is_complete(snapshot: &Path, repo_root: &Path, name: &str) -> bool {
    // Snapshot filenames must be simple child names; do not allow an index file
    // to make cache validation escape the selected model repository.
    if name.is_empty()
        || Path::new(name).file_name().and_then(|value| value.to_str()) != Some(name)
        || name == "."
        || name == ".."
    {
        return false;
    }
    let path = snapshot.join(name);
    let Ok(resolved) = fs::canonicalize(path) else {
        return false;
    };
    if !resolved.starts_with(repo_root) {
        return false;
    }
    let Ok(metadata) = fs::metadata(resolved) else {
        return false;
    };
    metadata.is_file() && metadata.len() > 0
}

fn model_snapshot_is_complete(root: &Path, model: &str, snapshot_path: &Path) -> bool {
    let Some(revision) = model_lock_revision(model) else {
        return false;
    };
    let Some(expected_sha) = model_lock_sha256(model) else {
        return false;
    };
    let Some(weight_name) = model_lock_string(model, "weight_file") else {
        return false;
    };
    if weight_name != "weights.npz" {
        return false;
    }

    let Ok(home_root) = fs::canonicalize(root) else {
        return false;
    };
    let hub_cache = home_root.join("hub");
    let Ok(cache_root) = fs::canonicalize(&hub_cache) else {
        return false;
    };
    let repo_key = format!("models--{}", model.replace('/', "--"));
    let Ok(repo_root) = fs::canonicalize(cache_root.join(&repo_key)) else {
        return false;
    };
    if !repo_root.starts_with(&cache_root) {
        return false;
    }
    let Ok(snapshots_root) = fs::canonicalize(repo_root.join("snapshots")) else {
        return false;
    };
    if !snapshots_root.starts_with(&repo_root) {
        return false;
    }
    let Ok(snapshot) = fs::canonicalize(snapshot_path) else {
        return false;
    };
    if !snapshot.is_dir()
        || !snapshot.starts_with(&snapshots_root)
        || snapshot.file_name().and_then(|name| name.to_str()) != Some(revision.as_str())
    {
        return false;
    }

    if !model_snapshot_file_is_complete(&snapshot, &repo_root, "config.json")
        || !model_snapshot_file_is_complete(&snapshot, &repo_root, &weight_name)
    {
        return false;
    }
    let Ok(config_bytes) = fs::read(snapshot.join("config.json")) else {
        return false;
    };
    let Ok(config) = serde_json::from_slice::<Value>(&config_bytes) else {
        return false;
    };
    if config.get("model_type").and_then(Value::as_str) != Some("whisper") {
        return false;
    }

    let Ok(weight_path) = fs::canonicalize(snapshot.join(&weight_name)) else {
        return false;
    };
    let Ok(meta) = fs::metadata(&weight_path) else {
        return false;
    };
    if !weight_path.starts_with(&repo_root) || !meta.is_file() || meta.len() == 0 {
        return false;
    }
    let marker_dir = home_root.join("rdt-model-verification");
    let marker_path = marker_dir.join(format!("{repo_key}.json"));
    let Ok(marker_dir_meta) = fs::symlink_metadata(&marker_dir) else {
        return false;
    };
    let Ok(marker_meta) = fs::symlink_metadata(&marker_path) else {
        return false;
    };
    if marker_dir_meta.file_type().is_symlink()
        || !marker_dir_meta.is_dir()
        || marker_meta.file_type().is_symlink()
        || !marker_meta.is_file()
    {
        return false;
    }
    let Ok(marker_bytes) = fs::read(&marker_path) else {
        return false;
    };
    let Ok(marker) = serde_json::from_slice::<Value>(&marker_bytes) else {
        return false;
    };
    marker.get("schema_version").and_then(Value::as_u64) == Some(1)
        && marker.get("repo_id").and_then(Value::as_str) == Some(model)
        && marker.get("revision").and_then(Value::as_str) == Some(revision.as_str())
        && marker.get("weight_file").and_then(Value::as_str) == Some(weight_name.as_str())
        && marker.get("sha256").and_then(Value::as_str) == Some(expected_sha.as_str())
        && marker.get("size_bytes").and_then(Value::as_u64) == Some(meta.len())
        && model_cache_metadata_matches(&meta, &marker)
}

#[cfg(unix)]
fn model_cache_metadata_matches(meta: &fs::Metadata, marker: &Value) -> bool {
    marker.get("mtime_ns").and_then(Value::as_i64)
        == Some(meta.mtime() * 1_000_000_000 + meta.mtime_nsec())
        && marker.get("inode").and_then(Value::as_u64) == Some(meta.ino())
}

#[cfg(windows)]
fn model_cache_metadata_matches(meta: &fs::Metadata, marker: &Value) -> bool {
    let modified_ns = meta.modified().ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .and_then(|duration| i64::try_from(duration.as_nanos()).ok());
    modified_ns.is_some() && marker.get("mtime_ns").and_then(Value::as_i64) == modified_ns
}

fn model_cache_has_weights(root: &Path, model: &str) -> bool {
    if cfg!(any(target_os = "windows", target_os = "linux")) {
        let Some((filename, expected_sha, revision)) = native_ggml_spec(model) else { return false; };
        let ggml = root.join("ggml");
        let artifact = ggml.join(filename);
        let sidecar = artifact.with_file_name(format!("{filename}.rdt-verified.json"));
        let Ok(canonical_root) = fs::canonicalize(&ggml) else { return false; };
        let Ok(meta) = fs::symlink_metadata(&artifact) else { return false; };
        if meta.file_type().is_symlink() || !meta.is_file() || meta.len() == 0 { return false; }
        let Ok(canonical) = fs::canonicalize(&artifact) else { return false; };
        if canonical.parent() != Some(canonical_root.as_path()) { return false; }
        let Ok(side_meta) = fs::symlink_metadata(&sidecar) else { return false; };
        if side_meta.file_type().is_symlink() || !side_meta.is_file() { return false; }
        let Ok(bytes) = fs::read(&sidecar) else { return false; };
        let Ok(json) = serde_json::from_slice::<Value>(&bytes) else { return false; };
        return json.get("model_id").and_then(Value::as_str) == Some(model)
            && json.get("filename").and_then(Value::as_str) == Some(filename)
            && json.get("repo").and_then(Value::as_str) == Some("ggerganov/whisper.cpp")
            && json.get("revision").and_then(Value::as_str) == Some(revision)
            && json.get("sha256").and_then(Value::as_str) == Some(expected_sha)
            && json.get("size_bytes").and_then(Value::as_u64) == Some(meta.len());
    }
    let repo_key = format!("models--{}", model.replace('/', "--"));
    let snapshots = root.join("hub").join(repo_key).join("snapshots");
    let entries = match fs::read_dir(snapshots) {
        Ok(entries) => entries,
        Err(_) => return false,
    };
    entries
        .filter_map(Result::ok)
        .any(|entry| model_snapshot_is_complete(root, model, &entry.path()))
}

fn model_cache_fully_verified(root: &Path, model: &str) -> bool {
    if !model_cache_has_weights(root, model) { return false; }
    if !cfg!(any(target_os = "windows", target_os = "linux")) { return true; }
    let Some((filename, expected_sha, _revision)) = native_ggml_spec(model) else { return false; };
    let artifact = root.join("ggml").join(filename);
    let Ok(mut file) = File::open(&artifact) else { return false; };
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        match file.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => hasher.update(&buffer[..count]),
            Err(_) => return false,
        }
    }
    let digest = format!("{:x}", hasher.finalize());
    digest == expected_sha
}

fn make_model_storage_status(
    state: &BackendState,
    model: &str,
) -> Result<ModelStorageStatus, String> {
    validate_model(model)?;
    let locations = load_model_storage_locations(state)?;
    let cached_index = locations
        .iter()
        .position(|root| model_cache_has_weights(Path::new(root.as_str()), model));
    let locations_availability = locations
        .iter()
        .enumerate()
        .map(|(index, root)| {
            let path = Path::new(root.as_str());
            // Once an earlier root contains the chosen model, don't probe later removable
            // volumes just to decorate Settings; this avoids unsolicited volume-access prompts.
            if cached_index
                .map(|chosen| index > chosen && path.starts_with("/Volumes"))
                .unwrap_or(false)
            {
                "not-checked".to_string()
            } else if path.is_dir() {
                "available".to_string()
            } else {
                "unavailable".to_string()
            }
        })
        .collect::<Vec<_>>();
    let cached_root = cached_index.and_then(|index| locations.get(index));
    let model_cached = cached_root.is_some();
    let first_available = locations
        .iter()
        .find(|root| Path::new(root.as_str()).is_dir());
    let home = home_dir()?;
    let first_internal = locations
        .iter()
        .find(|root| Path::new(root.as_str()).starts_with(&home));
    // Materialize this selection as an owned String before moving `locations`
    // into the response. Keeping a borrowed `&String` here causes E0505.
    let active_root = cached_root
        .or(first_available)
        .or(first_internal)
        .or_else(|| locations.first())
        .map(|root| root.to_string())
        .ok_or_else(|| "No model-storage location is configured. Open Settings.".to_string())?;
    Ok(ModelStorageStatus {
        model: model.to_string(),
        locations,
        locations_availability,
        active_root,
        model_cached,
        server_running: state.status().running,
    })
}

fn resolve_model_storage_root(
    state: &BackendState,
    model: &str,
) -> Result<(PathBuf, bool), String> {
    validate_model(model)?;
    let locations = load_model_storage_locations(state)?;
    // Prefer the first configured root containing a verified snapshot for the selected model.
    // When the model exists in multiple locations, the Settings order determines the copy.
    if let Some(root) = locations
        .iter()
        .find(|root| model_cache_has_weights(Path::new(root.as_str()), model))
    {
        return Ok((PathBuf::from(root.as_str()), true));
    }
    // For a model not yet cached, write to the first mounted/available configured folder.
    if let Some(root) = locations
        .iter()
        .find(|root| Path::new(root.as_str()).is_dir())
    {
        return Ok((PathBuf::from(root.as_str()), false));
    }
    // First-run convenience: create only an internal-home destination. Never create a
    // missing /Volumes path, because that could silently write to the Mac when a drive is out.
    let home = home_dir()?;
    if let Some(root) = locations
        .iter()
        .map(|root| PathBuf::from(root.as_str()))
        .find(|p| p.starts_with(&home))
    {
        fs::create_dir_all(&root).map_err(|e| {
            format!(
                "Could not create the internal model-storage folder {}: {e}",
                root.display()
            )
        })?;
        return Ok((root, false));
    }
    Err("None of the configured model-storage folders is currently available. Reconnect the drive or choose an available folder in Settings. No model files were changed.".into())
}

fn normalize_model_storage_locations(locations: Vec<String>) -> Result<Vec<String>, String> {
    if locations.is_empty() || locations.len() > MODEL_STORAGE_MAX_LOCATIONS {
        return Err(format!(
            "Choose between 1 and {MODEL_STORAGE_MAX_LOCATIONS} model-storage folders."
        ));
    }
    let mut normalized: Vec<String> = Vec::new();
    for raw in locations {
        let raw = raw.trim();
        if raw.is_empty() {
            continue;
        }
        let path = PathBuf::from(raw);
        if !path.is_absolute() {
            return Err(format!("This model-storage path is not absolute: {raw}"));
        }
        if !path.is_dir() {
            // Keep an already configured removable-drive path in the ordered list even
            // while that volume is disconnected. The app can show it as unavailable,
            // fall back to another mounted root, and resume using it when reconnected.
            // This branch never creates a missing volume or any directory beneath it.
            #[cfg(target_os = "macos")]
            let is_unmounted_volume = raw.starts_with("/Volumes/")
                && path.components().all(|component| !matches!(component, std::path::Component::ParentDir))
                && path != Path::new("/Volumes");
            #[cfg(not(target_os = "macos"))]
            let is_unmounted_volume = false;
            if is_unmounted_volume {
                let value = path.to_string_lossy().into_owned();
                if !normalized.contains(&value) {
                    normalized.push(value);
                }
                continue;
            }
            return Err(format!("This model-storage folder is not currently available: {raw}. Reconnect the drive or remove the location before saving."));
        }
        let canonical = path
            .canonicalize()
            .map_err(|e| format!("Could not resolve model-storage folder {raw}: {e}"))?;
        // People often select the cache's `hub` child by mistake. Store its HF_HOME parent.
        let canonical = if canonical.file_name().and_then(|x| x.to_str()) == Some("hub") {
            if let Some(parent) = canonical.parent() {
                parent.to_path_buf()
            } else {
                canonical.clone()
            }
        } else {
            canonical
        };
        if canonical.parent().is_none() {
            return Err(
                "Choose a model-storage folder below the filesystem root, not the root itself."
                    .into(),
            );
        }
        let value = canonical.display().to_string();
        if !normalized.contains(&value) {
            normalized.push(value);
        }
    }
    if normalized.is_empty() {
        return Err("Add at least one valid model-storage folder.".into());
    }
    Ok(normalized)
}

fn save_model_storage_locations(
    state: &BackendState,
    locations: Vec<String>,
) -> Result<Vec<String>, String> {
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    if state.status().running {
        return Err("Stop the local server before changing model-storage locations.".into());
    }
    if state
        .model_download_status
        .lock()
        .map(|s| s.active)
        .unwrap_or(true)
    {
        return Err(
            "Wait for the model download to finish or cancel it before changing storage locations."
                .into(),
        );
    }
    let locations = normalize_model_storage_locations(locations)?;
    let payload = serde_json::to_vec_pretty(&ModelStorageConfig {
        locations: locations.clone(),
    })
    .map_err(|e| format!("Could not encode model-storage settings: {e}"))?;
    let temporary = state
        .model_storage_config_path
        .with_extension(format!("json.tmp-{}", std::process::id()));
    fs::write(&temporary, payload)
        .map_err(|e| format!("Could not write model-storage settings: {e}"))?;
    let _ = set_private_permissions(&temporary, 0o600);
    if let Err(e) = fs::rename(&temporary, &state.model_storage_config_path) {
        let _ = fs::remove_file(&temporary);
        return Err(format!("Could not save model-storage settings: {e}"));
    }
    let _ = set_private_permissions(&state.model_storage_config_path, 0o600);
    Ok(locations)
}


#[tauri::command]
async fn get_model_storage_status(
    app: AppHandle,
    model: String,
) -> Result<ModelStorageStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<BackendState>();
        make_model_storage_status(&state, &model)
    })
    .await
    .map_err(|error| format!("Model-storage status worker failed: {error}"))?
}

#[tauri::command]
fn set_model_storage_locations(
    state: State<'_, BackendState>,
    model: String,
    locations: Vec<String>,
) -> Result<ModelStorageStatus, String> {
    validate_model(&model)?;
    save_model_storage_locations(&state, locations)?;
    make_model_storage_status(&state, &model)
}

#[tauri::command]
async fn pick_model_storage_directory(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let locale = read_app_language(&state);
    let title = tray_text(
        &locale,
        "msg.choose_a_hugging_face_model_cache_home.f18816af",
        "Choose a Hugging Face model-cache home",
        &[],
    );
    let picked = app.dialog().file().set_title(title).blocking_pick_folder();
    match picked {
        Some(folder) => {
            let path = folder
                .into_path()
                .map_err(|e| format!("The selected folder path is unavailable: {e}"))?;
            Ok(Some(path.to_string_lossy().into_owned()))
        }
        None => Ok(None),
    }
}

#[tauri::command]
fn get_app_build_info() -> AppBuildInfo {
    AppBuildInfo {
        version: app_version_label(),
        build_id: BUILD_ID.to_string(),
    }
}

#[tauri::command]
fn get_backend_status(state: State<'_, BackendState>) -> BackendStatus {
    state.status()
}

#[tauri::command]
fn open_backend_log(state: State<'_, BackendState>) -> Result<(), String> {
    OpenOptions::new().create(true).append(true).open(&state.log_path)
        .map_err(|e| format!("Could not create or open the backend log: {e}"))?;
    #[cfg(target_os = "macos")]
    let mut command = { let mut c = Command::new("/usr/bin/open"); c.args(["-a", "TextEdit"]); c };
    #[cfg(target_os = "windows")]
    let mut command = Command::new("notepad.exe");
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = Command::new("xdg-open");
    command.arg(&state.log_path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
        .map(|_| ()).map_err(|e| format!("Could not open the backend log: {e}"))
}

fn redact_log_value_after(line: &str, marker: &str) -> String {
    let lower = line.to_ascii_lowercase();
    let Some(marker_at) = lower.find(marker) else {
        return line.to_string();
    };
    let mut value_at = marker_at + marker.len();
    while value_at < line.len() {
        let c = line[value_at..].chars().next().unwrap_or(' ');
        if c.is_whitespace() || c == '\"' || c == '\'' {
            value_at += c.len_utf8();
        } else {
            break;
        }
    }
    let end = line[value_at..]
        .char_indices()
        .find(|(_, c)| c.is_whitespace() || ['&', ',', ';', '}', '\"', '\''].contains(c))
        .map(|(offset, _)| value_at + offset)
        .unwrap_or(line.len());
    if end <= value_at {
        return line.to_string();
    }
    let mut result = line.to_string();
    result.replace_range(value_at..end, "[REDACTED]");
    result
}

fn sanitize_log_line(line: &str, home: &str) -> String {
    let lower = line.to_ascii_lowercase();
    // Logs are diagnostics, not a data-export channel. Drop any line that appears to
    // contain audio/transcript text or a private Tailscale hostname/URL.
    if lower.contains(".ts.net") || lower.contains(".tailscale.net") {
        return "[REDACTED: private remote-access hostname/URL]".into();
    }
    if [
        "transcript=",
        "transcript:",
        "transcript\":",
        "\"text\":",
        "text=",
        "\"audio\":",
        "audio=",
        "payload=",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
    {
        return "[REDACTED: possible audio/transcript payload]".into();
    }
    let mut safe = if !home.is_empty() {
        line.replace(home, "~")
    } else {
        line.to_string()
    };
    for marker in [
        "authorization: bearer ",
        "bearer ",
        "access_token=",
        "access_token\":",
        "token=",
        "token:",
        "\"token\":",
        "api_key=",
        "api_key:",
        "secret=",
        "secret:",
        "password=",
        "password:",
        "server-token=",
    ] {
        safe = redact_log_value_after(&safe, marker);
    }
    safe
}

fn safe_tail_log(path: &Path, max_lines: usize) -> String {
    let raw = tail_log(path, max_lines);
    if raw.is_empty() {
        return String::new();
    }
    let home = home_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    raw.lines()
        .map(|line| sanitize_log_line(line, &home))
        .collect::<Vec<_>>()
        .join("\n")
}

#[tauri::command]
fn get_recent_backend_log(state: State<'_, BackendState>) -> String {
    safe_tail_log(&state.log_path, 100)
}

fn diagnostic_file_metadata(path: &Path) -> String {
    match fs::metadata(path) {
        Ok(metadata) => {
            let modified = metadata
                .modified()
                .ok()
                .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
                .map(|value| value.as_secs().to_string())
                .unwrap_or_else(|| "unknown".into());
            format!(
                "present; bytes={}; modified_unix_seconds={modified}",
                metadata.len()
            )
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => "missing".into(),
        Err(error) => format!("metadata error: {}", error.kind()),
    }
}

fn diagnostic_html_build_marker(path: &Path) -> String {
    let Ok(contents) = fs::read_to_string(path) else {
        return "unavailable".into();
    };
    for line in contents.lines() {
        if let Some((_, value)) = line.split_once("HTML build:") {
            let marker = value
                .split(|c: char| c == '"' || c == '\'' || c == '<')
                .next()
                .unwrap_or("")
                .trim();
            if !marker.is_empty() {
                return marker.chars().take(120).collect();
            }
        }
    }
    "marker not found".into()
}

#[tauri::command]
fn get_app_diagnostics(state: State<'_, BackendState>, preferred_port: u16) -> String {
    let status = state.status();
    let executable = backend_executable_path(&state.resource_dir);
    let html = state.resource_dir.join("rdt_voice_web").join("index.html");
    let token_ready = fs::read_to_string(&state.token_path)
        .map(|value| value.trim().len() >= 32)
        .unwrap_or(false);
    let mut lines = vec![
        format!("RDT Connect · {} · Build {BUILD_ID}", app_version_label()),
        single_instance_diagnostics(),
        format!(
            "Platform: {} / {}",
            std::env::consts::OS,
            std::env::consts::ARCH
        ),
        format!(
            "Backend process: {}",
            if status.running { "RUNNING" } else { "STOPPED" }
        ),
        format!(
            "Backend PID: {}",
            status
                .pid
                .map(|pid| pid.to_string())
                .unwrap_or_else(|| "—".into())
        ),
        format!(
            "Active endpoint: {}",
            status
                .local_url
                .clone()
                .unwrap_or_else(|| "— (server stopped)".into())
        ),
        {
            let tls =
                local_https_status_from_config(&load_local_tls_config(&state).unwrap_or_default());
            format!("Local transport: {}; certificate configured={}; macOS trust verified={}; RDT-added CA trust entries={}; {}", tls.mode.to_ascii_uppercase(), tls.configured, tls.trusted, tls.rdt_added_trust_count, tls.message)
        },
        format!(
            "Selected/running model: {}",
            status
                .model
                .clone()
                .unwrap_or_else(|| "No model currently running".into())
        ),
        format!(
            "Bundled backend executable: {}",
            if executable.is_file() {
                "PASS"
            } else {
                "FAIL — missing"
            }
        ),
        format!(
            "Bundled RDT Voice HTML: {}",
            if html.is_file() {
                "PASS"
            } else {
                "FAIL — missing"
            }
        ),
        format!(
            "Bundled backend resource metadata: {}",
            diagnostic_file_metadata(&executable)
        ),
        format!(
            "Bundled HTML resource metadata: {}",
            diagnostic_file_metadata(&html)
        ),
        format!(
            "Bundled HTML build marker: {}",
            diagnostic_html_build_marker(&html)
        ),
        format!(
            "Backend token file: {} (secret value never displayed)",
            if token_ready {
                "present and valid-length"
            } else {
                "missing or invalid"
            }
        ),
        format!(
            "Log file: {}",
            if state.log_path.is_file() {
                "available"
            } else {
                "not created yet"
            }
        ),
        format!(
            "macOS Metal framework: {}",
            if cfg!(target_os = "macos")
                && Path::new("/System/Library/Frameworks/Metal.framework").exists()
            {
                "present (framework only; MLX inference not run)"
            } else if cfg!(target_os = "macos") {
                "not found"
            } else {
                "not applicable on this platform"
            }
        ),
    ];
    if let Some(port) = status.port {
        lines.push(format!(
            "Local /health check: {}",
            if healthy(port, &load_local_tls_config(&state).unwrap_or_default()) { format!("PASS — {} 200 and status ok", if status.local_url.as_deref().unwrap_or("").starts_with("https://") { "HTTPS" } else { "HTTP" }) } else { "FAIL — backend did not return a healthy response (check TLS trust, port, and backend log)".into() }
        ));
        let auth_check = match state.api_credentials() {
            Ok((auth_port, token)) => {
                let tls_config = load_local_tls_config(&state).unwrap_or_default();
                let expected_scheme = if tls_config.mode.eq_ignore_ascii_case("http") {
                    "http"
                } else {
                    "https"
                };
                let origin = status
                    .local_url
                    .clone()
                    .unwrap_or_else(|| format!("{expected_scheme}://127.0.0.1:{auth_port}"));
                match local_tls_client(&tls_config, Duration::from_secs(3)) {
                    Ok(client) => match client
                        .get(format!("{origin}/auth/tailscale/status"))
                        .header("Authorization", format!("Bearer {token}"))
                        .header("Origin", origin.as_str())
                        .send()
                    {
                        Ok(response) if response.status().is_success() => {
                            "PASS — authenticated API accepted token".to_string()
                        }
                        Ok(response)
                            if response.status().as_u16() == 401
                                || response.status().as_u16() == 403 =>
                        {
                            format!(
                                "FAIL — HTTP {} (authentication/authorization)",
                                response.status().as_u16()
                            )
                        }
                        Ok(response) => format!(
                            "CHECK — authenticated API returned HTTP {}",
                            response.status().as_u16()
                        ),
                        Err(_) => {
                            "FAIL — authenticated API request could not complete within 3 seconds"
                                .into()
                        }
                    },
                    Err(_) => "FAIL — could not create a local diagnostic HTTP client".into(),
                }
            }
            Err(_) => "FAIL — token file missing or invalid".into(),
        };
        lines.push(format!("Authenticated API preflight: {auth_check}"));
        lines.push(
            "Transcription upload: NOT TESTED — diagnostics sends no audio or transcript text."
                .into(),
        );
        if let Some(model) = status.model.as_deref() {
            let cache_name = format!("models--{}", model.replace('/', "--"));
            let cache_found = home_dir()
                .ok()
                .map(|home| {
                    home.join(".cache")
                        .join("huggingface")
                        .join("hub")
                        .join(cache_name)
                        .is_dir()
                })
                .unwrap_or(false);
            lines.push(format!("Default Hugging Face cache: {} (targeted path check only; completeness not verified)", if cache_found { "model directory found" } else { "model directory not found" }));
        }
    } else {
        lines.push("Local /health check: NOT RUN — backend is stopped.".into());
        lines.push("Authenticated API preflight: NOT RUN — backend is stopped.".into());
        lines.push("Model cache: NOT checked — no model running.".into());
        if (1024..=65535).contains(&preferred_port) {
            match check_preferred_port(preferred_port) {
                Ok(_) => lines.push(format!("Preferred port 127.0.0.1:{preferred_port}: appears free (brief bind test only).")),
                Err(message) => lines.push(format!("Preferred port check: {message}")),
            }
        } else {
            lines.push("Preferred port check: invalid port value; choose 1024–65535.".into());
        }
    }
    lines.push(
        "Browser CORS/TLS/mixed-content behavior: NOT TESTED from this native app report.".into(),
    );
    lines.push("Safety: diagnostics did not start/stop a server, run inference, or read audio/transcript content.".into());
    lines.push(String::new());
    lines.push("STOP PREVIOUS SERVER — LATEST ATTEMPT TRACE (read-only; dedicated trace persists across app restarts):".into());
    if let Some(trace_path) = stop_trace_log_path() {
        let mut trace = safe_tail_log(&trace_path, 160);
        if trace.trim().is_empty() {
            trace = stop_trace_snapshot();
        }
        lines.push(if trace.trim().is_empty() {
            "(No recorded Stop Previous Server attempt yet in this app-support log.)".into()
        } else {
            trace
        });
    } else {
        let trace = stop_trace_snapshot();
        lines.push(if trace.trim().is_empty() {
            "(No recorded Stop Previous Server attempt yet in this app session.)".into()
        } else {
            trace
        });
    }
    lines.push(String::new());
    lines.push("CURRENT PROCESS / PORT SNAPSHOT (read-only; listener identity must be verified before any targeted cleanup; unknown processes are never stopped by port alone):".into());
    let mut diagnostic_ports = vec![8888u16, 8889u16];
    if let Some(port) = status.port {
        diagnostic_ports.push(port);
    }
    if (1024..=65535).contains(&preferred_port) {
        diagnostic_ports.push(preferred_port);
    }
    lines.extend(stop_diagnostic_snapshot(
        &state,
        status.pid,
        &diagnostic_ports,
        "CURRENT",
    ));
    let recent = safe_tail_log(&state.log_path, 20);
    lines.push(String::new());
    lines.push("Recent sanitized backend log (up to 20 lines):".into());
    lines.push(if recent.is_empty() {
        "(no log entries available)".into()
    } else {
        recent
    });
    lines.join("\n")
}

#[tauri::command]
fn get_backend_connection_status(state: State<'_, BackendState>) -> BackendConnectionCheck {
    let status = state.status();
    let (Some(port), true) = (status.port, status.running) else {
        return BackendConnectionCheck {
            running: false, pid: status.pid, port: status.port, local_url: status.local_url,
            health_ok: false, auth_ok: None, auth_http_status: None,
            message: "No managed backend is running in this RDT Connect instance. A separate browser connection is not established.".into(),
        };
    };
    let tls_config = load_local_tls_config(&state).unwrap_or_default();
    let health_ok = healthy(port, &tls_config);
    let (auth_ok, auth_http_status, auth_note) = match state.api_credentials() {
        Ok((auth_port, token)) => {
            let expected_scheme = if tls_config.mode.eq_ignore_ascii_case("http") {
                "http"
            } else {
                "https"
            };
            let origin = status
                .local_url
                .clone()
                .unwrap_or_else(|| format!("{expected_scheme}://127.0.0.1:{auth_port}"));
            match local_tls_client(&tls_config, Duration::from_secs(3)) {
                Ok(client) => match client.get(format!("{origin}/auth/tailscale/status"))
                    .header("Authorization", format!("Bearer {token}"))
                    .header("Origin", origin.as_str()).send() {
                        Ok(response) if response.status().is_success() => (Some(true), Some(response.status().as_u16()), "The backend accepted RDT Connect's local authentication token.".to_string()),
                        Ok(response) => (Some(false), Some(response.status().as_u16()), format!("The backend is reachable, but RDT Connect's authenticated API check returned HTTP {}.", response.status().as_u16())),
                        Err(_) => (Some(false), None, "The authenticated local API check did not complete within 3 seconds.".to_string()),
                    },
                Err(_) => (Some(false), None, "RDT Connect could not create the local HTTP client for its authenticated API check.".to_string()),
            }
        }
        Err(error) => (
            Some(false),
            None,
            format!("The backend token is not available to RDT Connect: {error}"),
        ),
    };
    let message = if !health_ok {
        format!("The managed process is present, but /health did not return a healthy response. {auth_note} This is not by itself evidence of a TLS failure.")
    } else if auth_ok == Some(true) {
        format!("Local /health returned HTTP 200 and RDT Connect's token was accepted. This does not prove that a separate browser session is authenticated; a browser request can still receive HTTP 401.")
    } else {
        format!("Local /health returned HTTP 200, so the server is reachable. Authentication needs attention: {auth_note}")
    };
    BackendConnectionCheck {
        running: true,
        pid: status.pid,
        port: Some(port),
        local_url: status.local_url,
        health_ok,
        auth_ok,
        auth_http_status,
        message,
    }
}

#[tauri::command]
fn get_orphaned_backends(state: State<'_, BackendState>) -> OrphanScan {
    scan_orphaned_backends(&state)
}

fn manager_parent_for_backend(state: &BackendState, pid: u32) -> Option<(u32, String)> {
    let expected_exe = backend_executable_path(&state.resource_dir);
    let snapshot = process_snapshot();
    let backend = snapshot.iter().find(|item| item.pid == pid)?;
    if !command_is_expected_backend(&backend.command, &expected_exe)
        || backend.ppid < 2
        || backend.ppid == std::process::id()
    {
        return None;
    }
    let manager = snapshot.iter().find(|item| item.pid == backend.ppid)?;
    let is_rdt_manager = is_rdt_manager_command(&manager.command);
    if is_rdt_manager {
        Some((manager.pid, manager.command.clone()))
    } else {
        None
    }
}

/// Return a PID's live process state without treating a zombie awaiting reaping as alive.
/// `None` is a confirmed absence; failures to inspect the process table are errors.
#[cfg(unix)]
fn inspect_manager_pid(pid: u32) -> Result<Option<(String, String)>, String> {
    let output = Command::new("/bin/ps")
        .args(["-ww", "-axo", "pid=,stat=,command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|e| format!("Could not verify old manager process state: {e}"))?;
    if !output.status.success() {
        return Err(
            "Could not verify old manager process state; no additional signal was sent.".into(),
        );
    }
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let mut fields = line.split_whitespace();
        let Some(pid_text) = fields.next() else {
            continue;
        };
        if pid_text.parse::<u32>().ok() != Some(pid) {
            continue;
        }
        let stat = fields.next().unwrap_or_default().to_string();
        let command = fields.collect::<Vec<_>>().join(" ");
        if stat.starts_with('Z') {
            return Ok(None);
        }
        return Ok(Some((stat, command)));
    }
    Ok(None)
}

#[cfg(windows)]
fn inspect_manager_pid(pid: u32) -> Result<Option<(String, String)>, String> {
    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return Err("Could not verify the Windows process table; no manager process was signaled.".into());
    }
    Ok(snapshot
        .into_iter()
        .find(|item| item.pid == pid && !item.stat.starts_with('Z'))
        .map(|item| (item.stat, item.command)))
}

/// A legacy manager cannot receive the stop request from a newer app. After the
/// exact backend it directly owns is stopped, close only that verified manager
/// so its stale flock is released and the replacement app can start cleanly.
fn close_legacy_manager_after_stop(pid: u32, expected_command: &str) -> Result<(), String> {
    if pid < 2 || pid == std::process::id() {
        return Ok(());
    }
    let Some((_, current)) = inspect_manager_pid(pid)? else {
        return Ok(());
    };
    let is_rdt_manager = is_rdt_manager_command(&current);
    if current != expected_command || !is_rdt_manager {
        return Err("The old manager changed during cleanup, so RDT Connect left it untouched. Refresh diagnostics before retrying.".into());
    }
    append_stop_trace(format!(
        "LEGACY MANAGER CLEANUP: targeting verified manager PID {pid}; command={}",
        sanitize_log_line(
            &current,
            &home_dir()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default()
        )
    ));
    if let Err(error) = send_signal_checked(pid as i32, SIGNAL_TERM) {
        append_stop_trace(format!(
            "LEGACY MANAGER SIGTERM ERROR (continuing existing bounded wait): {error}"
        ));
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        match inspect_manager_pid(pid)? {
            None => return Ok(()),
            Some((_, command)) if command == expected_command => {}
            Some(_) => {
                return Err(
                    "The previous manager changed during cleanup; no further signal was sent."
                        .into(),
                )
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
    match inspect_manager_pid(pid)? {
        None => return Ok(()),
        Some((_, command)) if command == expected_command => {
            if let Err(error) = send_signal_checked(pid as i32, SIGNAL_KILL) {
                append_stop_trace(format!(
                    "LEGACY MANAGER SIGKILL ERROR (continuing existing bounded wait): {error}"
                ));
            }
        }
        Some(_) => {
            return Err(
                "The previous manager changed during cleanup; no further signal was sent.".into(),
            )
        }
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        match inspect_manager_pid(pid)? {
            None => return Ok(()),
            Some((_, command)) if command == expected_command => {}
            Some(_) => {
                return Err(
                    "The previous manager changed during cleanup; no further signal was sent."
                        .into(),
                )
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err(format!("The previous RDT Connect manager PID {pid} is still present. The backend was stopped, but manager ownership could not be released."))
}

#[tauri::command]
fn stop_orphaned_backend(
    app: AppHandle,
    state: State<'_, BackendState>,
    pid: u32,
) -> Result<OrphanScan, String> {
    let started = Instant::now();
    reset_stop_trace(&format!("STOP PREVIOUS SERVER ATTEMPT STARTED; build={BUILD_ID}; manager_pid={}; requested_backend_pid={pid}.", std::process::id()));
    append_stop_trace(format!(
        "RESOURCE DIRECTORY: {}",
        sanitize_log_line(
            &state.resource_dir.display().to_string(),
            &home_dir()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default()
        )
    ));
    let command_and_group = process_command_and_group(pid);
    let expected_exe = backend_executable_path(&state.resource_dir);
    // Inspect a PID's listening sockets only after its command matches this project's
    // packaged backend. The socket query is read-only; any later signal still requires
    // a fresh executable, PID and process-group identity check.
    let (port, port_lookup_error) = match command_and_group.as_ref() {
        Some((command, _)) if command_is_expected_backend(command, &expected_exe) => {
            match infer_verified_process_port(pid, command) {
                Ok(port) => (port, None),
                Err(error) => (None, Some(error)),
            }
        }
        Some(_) | None => (None, None),
    };
    let home = home_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    match command_and_group {
        Some((command, pgid)) => append_stop_trace(format!("INITIAL TARGET IDENTITY: PID={pid}; PGID={pgid}; expected_executable_match={}; verified_listening_port={:?}; command={}", command_is_expected_backend(&command, &expected_exe), port, sanitize_log_line(&command, &home))),
        None => append_stop_trace(format!("INITIAL TARGET IDENTITY: PID {pid} is not present in /bin/ps command/PGID check.")),
    }
    let mut diagnostic_ports = vec![8888u16, 8889u16];
    if let Some(port) = port {
        diagnostic_ports.push(port);
    }
    for line in stop_diagnostic_snapshot(&state, Some(pid), &diagnostic_ports, "BEFORE") {
        append_stop_trace(line);
    }
    append_stop_trace("STOP COMMAND PHASE: entering existing verified stop logic.");
    let result: Result<OrphanScan, String> = (|| {
        // Snapshot port and direct manager identity before stopping. The command is not
        // considered successful until the backend tree is gone AND lsof confirms the
        // listener port stayed free after any stale manager was cleaned up.
        let (command, _) = process_command_and_group(pid).ok_or_else(|| {
            format!("Previous backend PID {pid} has already exited. Refresh server status.")
        })?;
        if !command_is_expected_backend(&command, &expected_exe) {
            return Err("Safety check refused to stop this PID because it is not the expected packaged RDT Connect backend. No process was stopped.".into());
        }
        let port = port.ok_or_else(|| port_lookup_error.clone().unwrap_or_else(|| format!("Could not identify a listening port from verified packaged PID {pid}. No process was stopped.")))?;
        let owner = manager_parent_for_backend(&state, pid);
        stop_orphaned_backend_safely(&state, pid)?;
        if let Some((owner_pid, owner_command)) = owner {
            close_legacy_manager_after_stop(owner_pid, &owner_command)?;
        }
        verify_port_released(port, pid)?;
        let scan = scan_orphaned_backends(&state);
        if scan.instances.iter().any(|item| item.pid == pid) {
            return Err(format!("Backend PID {pid} still appears in the process scan after Stop. Port {port} was checked, but cleanup is not confirmed."));
        }
        refresh_tray_menu(&app);
        Ok(scan)
    })();
    append_stop_trace("STOP COMMAND PHASE: existing stop logic returned; collecting final process and port state.");
    for line in stop_diagnostic_snapshot(&state, Some(pid), &diagnostic_ports, "AFTER") {
        append_stop_trace(line);
    }
    match &result {
        Ok(_) => append_stop_trace(format!("FINAL RESULT: SUCCESS; Rust command verified backend tree stopped and target port released; elapsed_ms={}.", started.elapsed().as_millis())),
        Err(error) => append_stop_trace(format!("FINAL RESULT: FAILURE; elapsed_ms={}; returned_error={}", started.elapsed().as_millis(), error)),
    }
    result
}

fn stop_diagnostic_snapshot(
    state: &BackendState,
    target_pid: Option<u32>,
    ports: &[u16],
    phase: &str,
) -> Vec<String> {
    let all = process_snapshot();
    let home = home_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    let target_label = target_pid
        .map(|pid| pid.to_string())
        .unwrap_or_else(|| "— (none selected)".into());
    let mut lines = vec![format!(
        "{phase} SNAPSHOT: manager PID={manager_pid}; requested backend PID={requested_backend_pid}.",
        manager_pid = std::process::id(),
        requested_backend_pid = target_label
    )];
    if let Some(manager) = all.iter().find(|item| item.pid == std::process::id()) {
        lines.push(format!(
            "{phase} MANAGER PROCESS: PID={manager_pid} PPID={parent_pid} PGID={process_group_id} STAT={process_state} COMMAND={command}",
            manager_pid = manager.pid,
            parent_pid = manager.ppid,
            process_group_id = manager.pgid,
            process_state = manager.stat,
            command = sanitize_log_line(&manager.command, &home)
        ));
    } else {
        lines.push(format!(
            "{phase} MANAGER PROCESS: PID {manager_pid} not found in /bin/ps snapshot.",
            manager_pid = std::process::id()
        ));
    }
    let expected_exe = backend_executable_path(&state.resource_dir);
    let candidates: Vec<_> = all
        .iter()
        .filter(|item| item.command.contains("rdt_voice_backend"))
        .collect();
    if candidates.is_empty() {
        lines.push(format!(
            "{phase} BACKEND PROCESS SCAN: no process command containing rdt_voice_backend."
        ));
    } else {
        for item in candidates.iter().take(12) {
            let verified = command_is_expected_backend(&item.command, &expected_exe);
            lines.push(format!("{phase} BACKEND CANDIDATE: PID={pid} PPID={parent_pid} PGID={process_group_id} STAT={process_state} VERIFIED_EXPECTED_EXECUTABLE={verified_expected_executable} COMMAND={command}", pid = item.pid, parent_pid = item.ppid, process_group_id = item.pgid, process_state = item.stat, verified_expected_executable = verified, command = sanitize_log_line(&item.command, &home)));
        }
        if candidates.len() > 12 {
            lines.push(format!("{phase} BACKEND CANDIDATE: omitted additional candidate records={candidate_count} (bounded report limit reached).", candidate_count = candidates.len() - 12));
        }
    }
    let mut seen = Vec::new();
    for port in ports.iter().copied() {
        if port < 1 || seen.contains(&port) {
            continue;
        }
        seen.push(port);
        let safety = if port == 8888 {
            "OBSERVATION ONLY — identify executable and ownership before any targeted cleanup"
        } else {
            "read-only listener check"
        };
        match listening_pids_on_port(port) {
            Ok(pids) if pids.is_empty() => {
                lines.push(format!("{phase} PORT {port}: FREE ({safety})."))
            }
            Ok(pids) => {
                let descriptions = pids
                    .iter()
                    .map(|pid| {
                        if let Some(item) = all.iter().find(|item| item.pid == *pid) {
                            format!(
                                "PID {} PPID {} PGID {} STAT {} COMMAND={}",
                                item.pid,
                                item.ppid,
                                item.pgid,
                                item.stat,
                                sanitize_log_line(&item.command, &home)
                            )
                        } else {
                            format!("PID {pid} (not found in /bin/ps snapshot)")
                        }
                    })
                    .collect::<Vec<_>>();
                lines.push(format!(
                    "{phase} PORT {port}: LISTENERS [{listener_descriptions}] ({safety}).",
                    listener_descriptions = descriptions.join("; ")
                ));
            }
            Err(error) => lines.push(format!(
                "{phase} PORT {port}: CHECK FAILED ({safety}): {error}"
            )),
        }
    }
    lines
}

#[tauri::command]
fn get_keep_in_menu_bar(state: State<'_, BackendState>) -> bool {
    state.keep_in_menu_bar()
}

#[tauri::command]
fn set_keep_in_menu_bar(
    app: AppHandle,
    state: State<'_, BackendState>,
    enabled: bool,
) -> Result<(), String> {
    let previous = state.keep_in_menu_bar();
    state.set_keep_in_menu_bar(enabled)?;
    let Some(tray) = app.tray_by_id("rdt-connect-menu-bar") else {
        let _ = state.set_keep_in_menu_bar(previous);
        return Err("The menu-bar icon is not available in this session; the previous preference was restored.".into());
    };
    if let Err(error) = tray.set_visible(enabled) {
        let _ = state.set_keep_in_menu_bar(previous);
        return Err(format!("Could not update the menu-bar icon visibility: {error}. The previous preference was restored."));
    }
    Ok(())
}

#[tauri::command]
async fn start_backend(
    app: AppHandle,
    model: String,
    preferred_port: u16,
) -> Result<BackendStatus, String> {
    let state_app = app.clone();
    let worker_app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = state_app.state::<BackendState>();
        start_backend_blocking(worker_app, state, model, preferred_port)
    })
    .await
    .map_err(|error| format!("Server startup worker failed: {error}"))?
}

fn start_backend_blocking(
    app: AppHandle,
    state: State<'_, BackendState>,
    model: String,
    preferred_port: u16,
) -> Result<BackendStatus, String> {
    // Keep this lock from the initial running check until the backend is registered or startup fails.
    // A second Start waits, then sees the first server instead of spawning another process.
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    validate_model(&model)?;
    if state
        .model_download_status
        .lock()
        .map(|s| s.active)
        .unwrap_or(true)
    {
        return Err("Wait for the selected model download to finish or cancel it before starting the server.".into());
    }
    if state.status().running {
        return Err(
            "The RDT Voice backend is already running. Stop it before starting another instance."
                .into(),
        );
    }
    let tls_config = load_local_tls_config(&state)?;
    let use_https = !tls_config.mode.eq_ignore_ascii_case("http");
    if use_https && cfg!(all(not(target_os = "macos"), not(windows))) {
        return Err("Trusted local HTTPS is not implemented on this platform. Change Local transport to HTTP for loopback-only development; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
    if use_https {
        validate_tls_material(&tls_config)?;
        if !configured_certificate_trusted(&tls_config) {
            return Err("HTTPS is selected, but the operating system has not verified the configured local certificate for 127.0.0.1. Open Settings → Local HTTPS and complete trusted certificate setup. No HTTP fallback was attempted.".into());
        }
    }
    // If the managed backend died unexpectedly, remove only a Serve route that
    // this still-running manager knows it created before starting a replacement.
    let stale_serve_port = {
        let mut inner = state.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.managed_serve_port.take()
    };
    if let Some(port) = stale_serve_port {
        disable_serve_port(port);
    }

    // Prevent two independent RDT Connect app instances from starting servers concurrently.
    // The file lock is held until Stop/Exit has terminated the managed process group.
    let orphan_pids = find_existing_backend_pids();
    if !orphan_pids.is_empty() {
        return Err(format!("An existing rdt_voice_backend process was found (PID {}). No new backend was started. If diagnostics identify it as an orphan from this project, use Stop verified orphaned backend first.", orphan_pids.join(", ")));
    }
    let manager_lock = state.acquire_manager_lock().map_err(|error| {
        if state.manager_lock_is_held() {
            format!("{error} If the previous app closed but its matching backend remained, refresh diagnostics and use Stop verified orphaned backend; no new process was started.")
        } else { error }
    })?;

    let exe = backend_executable_path(&state.resource_dir);
    if !exe.is_file() {
        return Err(format!("Bundled backend executable not found at {}. Run the platform-specific resource preparation script first.", exe.display()));
    }
    let html = state.resource_dir.join("rdt_voice_web").join("index.html");
    if !html.is_file() {
        return Err(format!(
            "Bundled RDT Voice HTML not found at {}. Run the platform-specific resource preparation script first.",
            html.display()
        ));
    }

    let port = check_preferred_port(preferred_port)?;
    let bundle_dir = exe
        .parent()
        .ok_or_else(|| "Could not resolve the backend bundle folder.".to_string())?;
    let inherited_path = std::env::var_os("PATH").unwrap_or_default();
    let path = std::env::join_paths(
        std::iter::once(bundle_dir.to_path_buf()).chain(std::env::split_paths(&inherited_path)),
    )
    .map_err(|error| format!("Could not assemble the backend runtime PATH safely: {error}"))?;
    let (model_storage_root, model_was_cached) = resolve_model_storage_root(&state, &model)?;
    let hub_cache = model_storage_root.join("hub");
    let xet_cache = model_storage_root.join("xet");
    let mut log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&state.log_path)
        .map_err(|e| format!("Could not open backend log: {e}"))?;
    write_log_prefix(
        &mut log,
        &format!(
            "Start {model} on 127.0.0.1:{port}; model cache root: {}; cached snapshot: {}",
            model_storage_root.display(),
            model_was_cached
        ),
    );
    let stderr_log = log
        .try_clone()
        .map_err(|e| format!("Could not clone backend log handle: {e}"))?;

    let port_text = port.to_string();
    let data_dir = application_support_dir("RDT Voice Server")?;
    fs::create_dir_all(&data_dir)
        .map_err(|e| format!("Could not create the RDT Voice Desktop data folder: {e}"))?;
    set_private_permissions(&data_dir, 0o700)
        .map_err(|e| format!("Could not secure the RDT Voice Desktop data folder: {e}"))?;
    let auth_db = data_dir.join("auth.sqlite3");

    let mut command = Command::new(&exe);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    command.args(["--host", "127.0.0.1", "--port", port_text.as_str()]);
    if use_https {
        command
            .arg("--ssl-certfile")
            .arg(tls_config.certificate_path.as_deref().unwrap_or(""));
        command
            .arg("--ssl-keyfile")
            .arg(tls_config.private_key_path.as_deref().unwrap_or(""));
    }
    command
        // Keep the app-managed Whisper runtime independent of a launching Terminal's
        // global model-tool configuration. Cache paths below are chosen by RDT Connect;
        // global Hugging Face credentials and Python import overrides are not needed.
        .env_remove("HF_TOKEN")
        .env_remove("HF_READ_TOKEN")
        .env_remove("HF_WRITE_TOKEN")
        .env_remove("HUGGING_FACE_HUB_TOKEN")
        .env_remove("WRITE_TOKEN")
        .env_remove("PYTHONPATH")
        .env_remove("HF_HUB_ENABLE_HF_TRANSFER")
        .env_remove("HF_HUB_DISABLE_XET_SUBMISSION")
        .env_remove("HF_XET_HIGH_PERFORMANCE")
        .env("HF_HUB_DISABLE_XET", "1")
        .current_dir(bundle_dir)
        .env("WHISPER_MODEL_ID", &model)
        .env("HF_ENDPOINT", "https://huggingface.co")
        .env("RDT_CONNECT_MANAGED", "1")
        .env("HF_HOME", &model_storage_root)
        .env("HF_HUB_CACHE", &hub_cache)
        .env("HUGGINGFACE_HUB_CACHE", &hub_cache)
        .env("TRANSFORMERS_CACHE", &hub_cache)
        .env("HF_XET_CACHE", &xet_cache)
        .env("RDT_AUTH", "on")
        .env("RDT_VOICE_HTML", &html)
        .env("RDT_TOKEN_FILE", &state.token_path)
        .env("RDT_AUTH_DB", &auth_db)
        .env("RDT_DATA_ROOT", data_dir.join("recordings"))
        .env(
            "RDT_BACKEND_SCHEME",
            if use_https { "https" } else { "http" },
        )
        .env("RDT_WEB_PORTS_STR", port_text.as_str())
        .env(
            "RDT_ALLOWED_HOSTS",
            format!("127.0.0.1:{port},localhost:{port}"),
        )
        .env("PATH", path)
        .env("PYTHONUNBUFFERED", "1");
    #[cfg(not(windows))]
    {
        let parent_exe = std::env::current_exe()
            .map_err(|e| format!("Could not determine RDT Connect executable for backend supervision: {e}"))?;
        command
            .env("RDT_CONNECT_PARENT_PID", std::process::id().to_string())
            .env("RDT_CONNECT_PARENT_EXE", parent_exe);
    }
    #[cfg(any(windows, target_os = "linux"))]
    command.env("RDT_GGML_MODEL_DIR", model_storage_root.join("ggml"));
    command.stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr_log));
    #[cfg(unix)]
    command.process_group(0);

    let mut child = command
        .spawn()
        .map_err(|e| format!("Could not launch the packaged Whisper backend: {e}"))?;
    let process_group = child.id();
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut is_ready = false;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => {
                terminate_process_group(process_group, Some(&mut child));
                let details = safe_tail_log(&state.log_path, 18);
                return Err(format!(
                    "The backend exited during startup ({status}).\n{}",
                    if details.is_empty() {
                        "Open the backend log for details."
                    } else {
                        &details
                    }
                ));
            }
            Err(e) => {
                terminate_process_group(process_group, Some(&mut child));
                return Err(format!("Could not monitor the backend process: {e}"));
            }
            Ok(None) => {}
        }
        if healthy(port, &tls_config) {
            is_ready = true;
            break;
        }
        thread::sleep(Duration::from_millis(300));
    }
    if !is_ready {
        terminate_process_group(process_group, Some(&mut child));
        let details = safe_tail_log(&state.log_path, 24);
        return Err(format!(
            "The backend did not become healthy within 60 seconds.\n{}",
            if details.is_empty() {
                "Open the backend log for details."
            } else {
                &details
            }
        ));
    }

    let recent_choice = LastServerChoice {
        model: model.clone(),
        preferred_port: port,
    };
    if let Err(error) = save_last_server_choice(&state, &recent_choice) {
        eprintln!("[RDT Connect] Could not persist the recent server choice: {error}");
    }
    {
        let mut inner = state.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.child = Some(child);
        inner.process_group = Some(process_group);
        inner.port = Some(port);
        inner.model = Some(model);
        inner.uses_https = use_https;
        inner.managed_serve_port = None;
        inner.managed_lock = Some(manager_lock);
    }
    let status = state.status();
    refresh_tray_menu(&app);
    Ok(status)
}

#[tauri::command]
async fn stop_backend(app: AppHandle) -> Result<BackendStatus, String> {
    let state_app = app.clone();
    let worker_app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = state_app.state::<BackendState>();
        stop_backend_blocking(worker_app, state)
    })
    .await
    .map_err(|error| format!("Server stop worker failed: {error}"))?
}

fn stop_backend_blocking(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<BackendStatus, String> {
    let started = Instant::now();
    reset_stop_trace(&format!(
        "STOP PREVIOUS SERVER ATTEMPT STARTED; build={BUILD_ID}; manager_pid={}; route=stop_backend (same shared cleanup routine as menu-bar Stop).",
        std::process::id()
    ));
    append_stop_trace(
        "COMMAND ENTRY: in-app stop command reached Rust; capturing a read-only before snapshot.",
    );

    let before_status = state.status();
    let before_scan = scan_orphaned_backends(&state);
    let target_pid = before_status
        .pid
        .or_else(|| before_scan.instances.first().map(|instance| instance.pid));
    let target_label = target_pid
        .map(|pid| pid.to_string())
        .unwrap_or_else(|| "— (none selected)".into());
    append_stop_trace(format!("TARGET DISCOVERY: requested backend PID={target_label}; verified orphan candidate count={}; manager_locked_elsewhere={}.", before_scan.instances.len(), before_scan.manager_locked_elsewhere));
    let mut candidate_ports = vec![8888u16, 8889u16];
    if let Some(port) = before_status.port {
        candidate_ports.push(port);
    }
    for instance in &before_scan.instances {
        if let Some(port) = instance.port {
            candidate_ports.push(port);
        }
    }
    let mut diagnostic_ports = Vec::new();
    for port in candidate_ports.iter().copied() {
        if port >= 1 && !diagnostic_ports.contains(&port) {
            diagnostic_ports.push(port);
        }
    }
    for line in stop_diagnostic_snapshot(&state, target_pid, &diagnostic_ports, "BEFORE") {
        append_stop_trace(line);
    }
    append_stop_trace("STOP COMMAND PHASE: calling stop_owned_and_orphan_backends — the exact same native cleanup function used by menu-bar Stop. Port 8888 is observation-only.");

    let result = stop_owned_and_orphan_backends(&state);
    append_stop_trace("STOP COMMAND PHASE: shared native cleanup function returned; collecting final process and port state.");
    for line in stop_diagnostic_snapshot(&state, target_pid, &diagnostic_ports, "AFTER") {
        append_stop_trace(line);
    }
    match &result {
        Ok(()) => append_stop_trace(format!("FINAL RESULT: shared stop function returned Ok; verified RDT Connect backend scan completed; elapsed_ms={}. Port listener snapshots above are observational evidence.", started.elapsed().as_millis())),
        Err(error) => append_stop_trace(format!("FINAL RESULT: FAILURE; elapsed_ms={}; returned_error={}", started.elapsed().as_millis(), error)),
    }
    result?;
    refresh_tray_menu(&app);
    Ok(state.status())
}

#[derive(Serialize, Clone)]
struct BrowserOption {
    id: String,
    name: String,
}

#[cfg(target_os = "macos")]
fn browser_app_is_installed(browser_id: &str) -> bool {
    let app_name = match browser_id {
        "safari" => "Safari.app",
        "chrome" => "Google Chrome.app",
        "firefox" => "Firefox.app",
        "edge" => "Microsoft Edge.app",
        "brave" => "Brave Browser.app",
        "arc" => "Arc.app",
        "vivaldi" => "Vivaldi.app",
        "opera" => "Opera.app",
        "duckduckgo" => "DuckDuckGo.app",
        "mullvad" => "Mullvad Browser.app",
        _ => return false,
    };
    let mut roots = vec![
        PathBuf::from("/Applications"),
        PathBuf::from("/System/Applications"),
    ];
    if let Some(home) = std::env::var_os("HOME") {
        roots.push(PathBuf::from(home).join("Applications"));
    }
    roots.iter().any(|root| root.join(app_name).is_dir())
}

#[cfg(windows)]
fn windows_browser_executable(browser_id: &str) -> Option<PathBuf> {
    use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    use winreg::RegKey;

    let exe_names: &[&str] = match browser_id {
        "chrome" => &["chrome.exe"],
        "firefox" => &["firefox.exe"],
        "edge" => &["msedge.exe"],
        "brave" => &["brave.exe"],
        "vivaldi" => &["vivaldi.exe"],
        "opera" => &["launcher.exe", "opera.exe"],
        "duckduckgo" => &["DuckDuckGo.exe"],
        "mullvad" => &["mullvad-browser.exe", "mullvadbrowser.exe", "firefox.exe"],
        _ => return None,
    };

    // App Paths is the supported Windows registration mechanism and includes
    // user-level installations. Verify every registry result exists before use.
    let mut candidates: Vec<PathBuf> = Vec::new();
    for exe in exe_names {
        let app_path_keys = [
            (HKEY_CURRENT_USER, format!(r"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}")),
            (HKEY_LOCAL_MACHINE, format!(r"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}")),
            (HKEY_LOCAL_MACHINE, format!(r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe}")),
        ];
        for (hive, key_path) in app_path_keys {
            if let Ok(key) = RegKey::predef(hive).open_subkey(&key_path) {
                if let Ok(value) = key.get_value::<String, _>("") {
                    let clean = value.trim().trim_matches('"');
                    if !clean.is_empty() {
                        candidates.push(PathBuf::from(clean));
                    }
                }
            }
        }
    }

    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from);
    let program_files = std::env::var_os("PROGRAMFILES").map(PathBuf::from);
    let program_files_x86 = std::env::var_os("PROGRAMFILES(X86)").map(PathBuf::from);
    let mut add = |base: &Option<PathBuf>, relative: &str| {
        if let Some(base) = base {
            candidates.push(base.join(relative));
        }
    };
    match browser_id {
        "chrome" => {
            add(&local, r"Google\Chrome\Application\chrome.exe");
            add(&program_files, r"Google\Chrome\Application\chrome.exe");
            add(&program_files_x86, r"Google\Chrome\Application\chrome.exe");
        }
        "firefox" => {
            add(&local, r"Programs\Mozilla Firefox\firefox.exe");
            add(&program_files, r"Mozilla Firefox\firefox.exe");
            add(&program_files_x86, r"Mozilla Firefox\firefox.exe");
        }
        "edge" => {
            add(&local, r"Microsoft\Edge\Application\msedge.exe");
            add(&program_files, r"Microsoft\Edge\Application\msedge.exe");
            add(&program_files_x86, r"Microsoft\Edge\Application\msedge.exe");
        }
        "brave" => {
            add(&local, r"BraveSoftware\Brave-Browser\Application\brave.exe");
            add(&program_files, r"BraveSoftware\Brave-Browser\Application\brave.exe");
            add(&program_files_x86, r"BraveSoftware\Brave-Browser\Application\brave.exe");
        }
        "vivaldi" => {
            add(&local, r"Vivaldi\Application\vivaldi.exe");
            add(&program_files, r"Vivaldi\Application\vivaldi.exe");
            add(&program_files_x86, r"Vivaldi\Application\vivaldi.exe");
        }
        "opera" => {
            add(&local, r"Programs\Opera\launcher.exe");
            add(&program_files, r"Opera\launcher.exe");
            add(&program_files_x86, r"Opera\launcher.exe");
        }
        "duckduckgo" => {
            add(&local, r"Programs\DuckDuckGo\DuckDuckGo.exe");
            add(&program_files, r"DuckDuckGo\DuckDuckGo.exe");
        }
        "mullvad" => {
            for executable in ["mullvad-browser.exe", "mullvadbrowser.exe", "firefox.exe"] {
                add(&local, &format!(r"Programs\Mullvad Browser\Browser\{executable}"));
                add(&program_files, &format!(r"Mullvad Browser\Browser\{executable}"));
            }
        }
        _ => {}
    }
    candidates.into_iter().find(|candidate| candidate.is_file())
}

#[cfg(windows)]
fn browser_app_is_installed(browser_id: &str) -> bool {
    windows_browser_executable(browser_id).is_some()
}

#[cfg(all(unix, not(target_os = "macos")))]
fn browser_app_is_installed(browser_id: &str) -> bool {
    let executable = match browser_id {
        "chrome" => "google-chrome",
        "firefox" => "firefox",
        "edge" => "microsoft-edge",
        "brave" => "brave-browser",
        "vivaldi" => "vivaldi",
        "opera" => "opera",
        "duckduckgo" => "duckduckgo",
        "mullvad" => "mullvad-browser",
        _ => return false,
    };
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .any(|root| root.join(executable).is_file())
}

#[cfg(target_os = "macos")]
fn browser_bundle_id(browser_id: &str) -> Option<&'static str> {
    match browser_id {
        "safari" => Some("com.apple.Safari"),
        "chrome" => Some("com.google.Chrome"),
        "firefox" => Some("org.mozilla.firefox"),
        "edge" => Some("com.microsoft.edgemac"),
        "brave" => Some("com.brave.Browser"),
        "arc" => Some("company.thebrowser.Browser"),
        "vivaldi" => Some("com.vivaldi.Vivaldi"),
        "opera" => Some("com.operasoftware.Opera"),
        "duckduckgo" => Some("com.duckduckgo.macos.browser"),
        "mullvad" => Some("net.mullvad.mullvadbrowser"),
        _ => None,
    }
}

#[cfg(windows)]
fn open_url_windows(url: &str, browser_id: Option<&str>) -> Result<(), String> {
    use std::ffi::c_void;

    // Only open explicit, fixed product URLs and secure web/email targets.
    let parsed = url::Url::parse(url).map_err(|error| format!("The external URL is invalid: {error}"))?;
    let allowed = match parsed.scheme() {
        "https" => matches!(parsed.host_str(), Some("rolandoristech.github.io" | "github.com" | "youtube.com" | "www.youtube.com")),
        "mailto" => parsed.path() == "woknewsbusiness@gmail.com",
        _ => false,
    };
    if !allowed {
        return Err("RDT Connect refused to open an external URL outside its approved destinations.".into());
    }

    let selected = browser_id.unwrap_or("default");
    if selected != "default" {
        if let Some(executable) = windows_browser_executable(selected) {
            Command::new(&executable)
                .arg(url)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map(|_| ())
                .map_err(|error| format!("Could not open the URL in the selected browser: {error}"))?;
            return Ok(());
        }
        // A browser saved by an earlier installation may since have been removed.
        // Do not break Open RDT Voice: fall through to the registered system default.
    }

    #[link(name = "shell32")]
    extern "system" {
        fn ShellExecuteW(
            hwnd: *mut c_void,
            operation: *const u16,
            file: *const u16,
            parameters: *const u16,
            directory: *const u16,
            show_command: i32,
        ) -> isize;
    }
    let operation: Vec<u16> = "open".encode_utf16().chain(std::iter::once(0)).collect();
    let target: Vec<u16> = url.encode_utf16().chain(std::iter::once(0)).collect();
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(), operation.as_ptr(), target.as_ptr(),
            std::ptr::null(), std::ptr::null(), 1,
        )
    };
    if result <= 32 {
        return Err(format!("Windows could not open the URL with the registered default browser (ShellExecuteW={result})."));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn open_url_external(url: &str, browser_id: Option<&str>) -> Result<(), String> {
    let selected = browser_id.unwrap_or("default");
    let mut command = Command::new("/usr/bin/open");
    if selected != "default" {
        let bundle_id = browser_bundle_id(selected).ok_or_else(|| "Choose a supported browser from the Local Server browser list.".to_string())?;
        if !browser_app_is_installed(selected) {
            return Err("The selected browser is no longer installed. Choose another browser and retry.".into());
        }
        command.arg("-b").arg(bundle_id);
    }
    command.arg(url).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
        .map(|_| ()).map_err(|error| format!("Could not open the URL in the selected browser: {error}"))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn open_url_external(url: &str, _browser_id: Option<&str>) -> Result<(), String> {
    let parsed = url::Url::parse(url).map_err(|error| format!("The external URL is invalid: {error}"))?;
    if !matches!(parsed.scheme(), "https" | "mailto") {
        return Err("RDT Connect refused to open an unsupported external URL.".into());
    }
    Command::new("xdg-open").arg(url).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
        .map(|_| ()).map_err(|error| format!("Could not open the URL in the default browser: {error}"))
}

#[tauri::command]
fn list_available_browsers() -> Vec<BrowserOption> {
    let mut options = vec![BrowserOption {
        id: "default".into(),
        name: "System Default Browser".into(),
    }];
    for (id, name) in [
        ("safari", "Safari"),
        ("chrome", "Google Chrome"),
        ("firefox", "Firefox"),
        ("edge", "Microsoft Edge"),
        ("brave", "Brave"),
        ("duckduckgo", "DuckDuckGo"),
        ("mullvad", "Mullvad Browser"),
        ("arc", "Arc"),
        ("vivaldi", "Vivaldi"),
        ("opera", "Opera"),
    ] {
        if browser_app_is_installed(id) {
            options.push(BrowserOption {
                id: id.into(),
                name: name.into(),
            });
        }
    }
    options
}

#[tauri::command]
fn open_voice_window(
    app: AppHandle,
    state: State<'_, BackendState>,
    browser_id: Option<String>,
    locale: Option<String>,
) -> Result<(), String> {
    let status = state.status();
    let _port = status
        .port
        .ok_or_else(|| "Start the local backend first.".to_string())?;

    // Exchange the bearer token for a short-lived, single-use handoff code. The
    // real token never enters the URL, browser history, screenshots, or copied links.
    let handoff = api_request(&state, "POST", "/v1/browser-handoff", Some(json!({})))?;
    let code = handoff.get("code").and_then(Value::as_str)
        .filter(|value| value.len() >= 32)
        .ok_or_else(|| "The backend did not return a valid one-time browser handoff code. Update the RDT Voice backend, rebuild the bundled resources, and try again.".to_string())?;

    let backend_url = status
        .local_url
        .clone()
        .ok_or_else(|| "The local server has no active endpoint.".to_string())?;
    // The locale is an allowlisted, non-secret UI preference. Keep it in the
    // query so RDT Voice can read it before its first render. The one-time
    // handoff code and backend URL remain in the fragment; the real API token
    // never enters the URL.
    let active_locale = locale
        .filter(|candidate| supported_locale(candidate))
        .unwrap_or_else(|| read_app_language(&state));
    let fragment = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("rdt_connect_handoff", code)
        .append_pair("rdt_connect_backend", &backend_url)
        .finish();
    let mut launch_url = url::Url::parse("https://rolandoristech.github.io/rdt-voice/")
        .map_err(|e| format!("Could not build the official RDT Voice browser URL: {e}"))?;
    launch_url
        .query_pairs_mut()
        .append_pair("rdt_connect_lang", &active_locale);
    launch_url.set_fragment(Some(&fragment));

    if let Some(existing) = app.get_webview_window("voice") {
        let _ = existing.close();
    }
    #[cfg(target_os = "macos")]
    {
        let selected_browser = browser_id.as_deref().unwrap_or("default");
        let mut command = Command::new("/usr/bin/open");
        if selected_browser != "default" {
            let bundle_id = browser_bundle_id(selected_browser).ok_or_else(|| "The selected browser is not supported. Choose a browser from the available list.".to_string())?;
            if !browser_app_is_installed(selected_browser) {
                return Err("The selected browser is no longer installed in Applications. Choose another browser and retry.".to_string());
            }
            command.arg("-b").arg(bundle_id);
        }
        command.arg(launch_url.as_str()).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            .map_err(|e| format!("Could not open RDT Voice in the selected browser: {e}"))?;
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(launch_url.as_str(), browser_id.as_deref())?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = browser_id;
        Command::new("xdg-open").arg(launch_url.as_str()).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            .map_err(|e| format!("Could not open RDT Voice in the default browser: {e}"))?;
    }
    Ok(())
}

fn request_builder(
    client: &Client,
    method: &str,
    url: String,
    token: &str,
    origin: &str,
) -> RequestBuilder {
    let auth = format!("Bearer {token}");
    match method {
        "POST" => client
            .post(url)
            .header("Authorization", auth)
            .header("Origin", origin),
        "DELETE" => client
            .delete(url)
            .header("Authorization", auth)
            .header("Origin", origin),
        _ => client
            .get(url)
            .header("Authorization", auth)
            .header("Origin", origin),
    }
}

fn api_request(
    state: &BackendState,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> Result<Value, String> {
    let (_port, token) = state.api_credentials()?;
    let status = state.status();
    let origin = status
        .local_url
        .ok_or_else(|| "The local server has no active endpoint.".to_string())?;
    let url = format!("{origin}{path}");
    let tls_config = load_local_tls_config(state)?;
    let client = local_tls_client(&tls_config, Duration::from_secs(12))?;
    let mut req = request_builder(&client, method, url, &token, &origin);
    if let Some(value) = body {
        req = req.json(&value);
    }
    let response = req
        .send()
        .map_err(|e| format!("Could not contact the local backend: {e}"))?;
    let code = response.status();
    let value: Value = response.json().unwrap_or_else(|_| json!({}));
    if !code.is_success() {
        let detail = value
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or("The backend returned an error.");
        return Err(format!("{} ({})", detail, code.as_u16()));
    }
    Ok(value)
}

#[tauri::command]
fn get_tailscale_status(state: State<'_, BackendState>) -> Result<Value, String> {
    api_request(&state, "GET", "/auth/tailscale/status", None)
}

#[tauri::command]
fn get_remote_access_diagnostics(state: State<'_, BackendState>) -> Result<Value, String> {
    api_request(&state, "GET", "/auth/tailscale/diagnostics", None)
}

#[tauri::command]
fn enable_private_access(state: State<'_, BackendState>) -> Result<Value, String> {
    let response = api_request(&state, "POST", "/auth/tailscale/setup", Some(json!({})))?;
    let created = response.get("reused").and_then(Value::as_bool) == Some(false);
    let port = response
        .get("serve_port")
        .and_then(Value::as_u64)
        .or_else(|| {
            response
                .get("public_url")
                .and_then(Value::as_str)
                .and_then(|u| url::Url::parse(u).ok())
                .and_then(|u| u.port().map(|p| p as u64))
        })
        .map(|p| p as u16);
    if created {
        let mut inner = state.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.managed_serve_port = port;
    }
    Ok(response)
}

#[tauri::command]
fn generate_pairing_qr(state: State<'_, BackendState>) -> Result<PairingQr, String> {
    let ts = api_request(&state, "GET", "/auth/tailscale/status", None)?;
    if ts.get("ready").and_then(Value::as_bool) != Some(true) {
        return Err(ts
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("Enable private HTTPS access first.")
            .to_string());
    }
    let public_url = ts
        .get("public_url")
        .and_then(Value::as_str)
        .ok_or_else(|| "Tailscale did not return a private HTTPS URL.".to_string())?;
    let response = api_request(
        &state,
        "POST",
        "/auth/pair/start",
        Some(json!({ "public_origin": public_url })),
    )?;
    let svg = response
        .get("qr_svg")
        .and_then(Value::as_str)
        .ok_or_else(|| "The backend did not return a QR image.".to_string())?;
    let pairing_url = response
        .get("pairing_url")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let expires_at = response
        .get("expires_at")
        .and_then(Value::as_f64)
        .unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64()
                + 60.0
        });
    Ok(PairingQr {
        pairing_url,
        qr_data_url: format!(
            "data:image/svg+xml;base64,{}",
            BASE64.encode(svg.as_bytes())
        ),
        expires_at,
    })
}

#[tauri::command]
fn list_pair_requests(state: State<'_, BackendState>) -> Result<Value, String> {
    api_request(&state, "GET", "/auth/pair/requests", None)
}

#[tauri::command]
fn approve_pair_request(
    state: State<'_, BackendState>,
    request_id: String,
) -> Result<Value, String> {
    api_request(
        &state,
        "POST",
        &format!("/auth/pair/requests/{request_id}/approve"),
        None,
    )
}

#[tauri::command]
fn deny_pair_request(state: State<'_, BackendState>, request_id: String) -> Result<Value, String> {
    api_request(
        &state,
        "POST",
        &format!("/auth/pair/requests/{request_id}/deny"),
        None,
    )
}

#[tauri::command]
fn list_paired_devices(state: State<'_, BackendState>) -> Result<Value, String> {
    api_request(&state, "GET", "/auth/devices", None)
}

#[tauri::command]
fn revoke_paired_device(
    state: State<'_, BackendState>,
    session_id: String,
) -> Result<Value, String> {
    api_request(
        &state,
        "DELETE",
        &format!("/auth/devices/{session_id}"),
        None,
    )
}

fn tailscale_cli() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let candidates = [
        PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
        PathBuf::from("/opt/homebrew/bin/tailscale"),
        PathBuf::from("/usr/local/bin/tailscale"),
    ];
    for path in candidates {
        if path.is_file() {
            return Some(path);
        }
    }
    if let Some(home) = home {
        let p = home.join("Applications/Tailscale.app/Contents/MacOS/Tailscale");
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(path) = std::env::var("PATH") {
        for dir in std::env::split_paths(&std::ffi::OsString::from(path)) {
            let p = dir.join("tailscale");
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

fn disable_serve_port(port: u16) {
    if let Some(cli) = tailscale_cli() {
        let serve_arg = format!("--https={port}");
        let _ = Command::new(cli)
            .args(["serve", serve_arg.as_str(), "off"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct LastServerChoice {
    model: String,
    preferred_port: u16,
}

fn supported_locale(locale: &str) -> bool {
    matches!(
        locale,
        "en" | "zh-CN"
            | "zh-TW"
            | "fr"
            | "es"
            | "ko"
            | "ja"
            | "de"
            | "th"
            | "vi"
            | "tl"
            | "id"
            | "hi"
            | "ms"
            | "pt"
            | "ru"
            | "it"
            | "lo"
            | "ar"
            | "tr"
            | "pl"
            | "nl"
            | "uk"
            | "sv"
            | "cs"
            | "no"
    )
}

fn read_app_language(state: &BackendState) -> String {
    fs::read_to_string(&state.app_language_pref_path)
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|locale| supported_locale(locale))
        .unwrap_or_else(|| "en".to_string())
}

fn locale_catalog_source(locale: &str) -> &'static str {
    match locale {
        "ar" => include_str!("../../src/locales/ar.json"),
        "cs" => include_str!("../../src/locales/cs.json"),
        "de" => include_str!("../../src/locales/de.json"),
        "es" => include_str!("../../src/locales/es.json"),
        "fr" => include_str!("../../src/locales/fr.json"),
        "hi" => include_str!("../../src/locales/hi.json"),
        "id" => include_str!("../../src/locales/id.json"),
        "it" => include_str!("../../src/locales/it.json"),
        "ja" => include_str!("../../src/locales/ja.json"),
        "ko" => include_str!("../../src/locales/ko.json"),
        "lo" => include_str!("../../src/locales/lo.json"),
        "ms" => include_str!("../../src/locales/ms.json"),
        "nl" => include_str!("../../src/locales/nl.json"),
        "no" => include_str!("../../src/locales/no.json"),
        "pl" => include_str!("../../src/locales/pl.json"),
        "pt" => include_str!("../../src/locales/pt.json"),
        "ru" => include_str!("../../src/locales/ru.json"),
        "sv" => include_str!("../../src/locales/sv.json"),
        "th" => include_str!("../../src/locales/th.json"),
        "tl" => include_str!("../../src/locales/tl.json"),
        "tr" => include_str!("../../src/locales/tr.json"),
        "uk" => include_str!("../../src/locales/uk.json"),
        "vi" => include_str!("../../src/locales/vi.json"),
        "zh-CN" => include_str!("../../src/locales/zh-CN.json"),
        "zh-TW" => include_str!("../../src/locales/zh-TW.json"),
        _ => include_str!("../../src/locales/en.json"),
    }
}

fn tray_text(locale: &str, key: &str, fallback: &str, params: &[(&str, String)]) -> String {
    let catalog: Value = serde_json::from_str(locale_catalog_source(locale)).unwrap_or(Value::Null);
    let mut text = catalog
        .get("messages")
        .and_then(|messages| messages.get(key))
        .and_then(Value::as_str)
        .unwrap_or(fallback)
        .to_string();
    for (name, value) in params {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

fn write_private_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "Preference path has no parent folder.".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("Could not create preference folder: {error}"))?;
    let tmp = parent.join(format!(
        ".rdt-pref-{}-{}.tmp",
        std::process::id(),
        unix_epoch_millis()
    ));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    let mut file = options
        .open(&tmp)
        .map_err(|error| format!("Could not create temporary preference file: {error}"))?;
    let result = (|| {
        file.write_all(bytes)
            .map_err(|error| format!("Could not write preference file: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("Could not flush preference file: {error}"))?;
        set_private_permissions(&tmp, 0o600)
            .map_err(|error| format!("Could not protect preference file: {error}"))?;
        fs::rename(&tmp, path)
            .map_err(|error| format!("Could not save preference file: {error}"))?;
        set_private_permissions(path, 0o600)
            .map_err(|error| format!("Could not protect saved preference: {error}"))?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn save_last_server_choice(state: &BackendState, choice: &LastServerChoice) -> Result<(), String> {
    validate_model(&choice.model)?;
    if !(1024..=65535).contains(&choice.preferred_port) {
        return Err("The recent preferred port must be between 1024 and 65535.".into());
    }
    let bytes = serde_json::to_vec(choice)
        .map_err(|error| format!("Could not encode recent server choice: {error}"))?;
    write_private_atomic(&state.last_server_choice_path, &bytes)
}

fn read_last_server_choice(state: &BackendState) -> LastServerChoice {
    if let Ok(bytes) = fs::read(&state.last_server_choice_path) {
        if let Ok(choice) = serde_json::from_slice::<LastServerChoice>(&bytes) {
            if validate_model(&choice.model).is_ok()
                && (1024..=65535).contains(&choice.preferred_port)
            {
                return choice;
            }
        }
    }
    LastServerChoice {
        model: if cfg!(any(target_os = "windows", target_os = "linux")) {
            "large-v3-turbo".into()
        } else {
            "mlx-community/whisper-tiny-mlx".into()
        },
        preferred_port: 8888,
    }
}

#[tauri::command]
fn set_app_language(
    app: AppHandle,
    state: State<'_, BackendState>,
    locale: String,
) -> Result<(), String> {
    if !supported_locale(&locale) {
        return Err(format!("Unsupported interface language: {locale}"));
    }
    write_private_atomic(&state.app_language_pref_path, locale.as_bytes())?;
    refresh_tray_menu(&app);
    Ok(())
}

#[tauri::command]
fn remember_recent_server_choice(
    state: State<'_, BackendState>,
    model: String,
    preferred_port: u16,
) -> Result<(), String> {
    save_last_server_choice(
        &state,
        &LastServerChoice {
            model,
            preferred_port,
        },
    )
}

fn tray_voice_preflight_passes(state: &BackendState, status: &BackendStatus) -> bool {
    let (Some(port), true) = (status.port, status.running) else {
        return false;
    };
    let tls_config = load_local_tls_config(state).unwrap_or_default();
    if !healthy(port, &tls_config) {
        return false;
    }
    let Ok((auth_port, token)) = state.api_credentials() else {
        return false;
    };
    let expected_scheme = if tls_config.mode.eq_ignore_ascii_case("http") {
        "http"
    } else {
        "https"
    };
    let origin = status
        .local_url
        .clone()
        .unwrap_or_else(|| format!("{expected_scheme}://127.0.0.1:{auth_port}"));
    let Ok(client) = local_tls_client(&tls_config, Duration::from_millis(1200)) else {
        return false;
    };
    client
        .get(format!("{origin}/auth/tailscale/status"))
        .header("Authorization", format!("Bearer {token}"))
        .header("Origin", origin.as_str())
        .send()
        .map(|response| response.status().is_success())
        .unwrap_or(false)
}

fn show_update_window(app: &AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("updates") {
        window
            .show()
            .map_err(|error| format!("Could not show the update window: {error}"))?;
        window
            .set_focus()
            .map_err(|error| format!("Could not focus the update window: {error}"))?;
        let _ = app.emit("rdt-connect-update-check-request", ());
        return Ok(());
    }

    WebviewWindowBuilder::new(app, "updates", WebviewUrl::App("update.html".into()))
        .title("RDT Connect Updates")
        .inner_size(468.0, 270.0)
        .min_inner_size(420.0, 250.0)
        .resizable(false)
        .center()
        .build()
        .map(|_| ())
        .map_err(|error| format!("Could not open the update window: {error}"))
}

fn build_tray_menu(app: &AppHandle, state: &BackendState) -> tauri::Result<Menu<tauri::Wry>> {
    let status = state.status();
    let locale = read_app_language(state);
    let starting = state.menu_start_in_progress.load(Ordering::SeqCst);
    let orphan_scan = if status.running || starting {
        OrphanScan {
            manager_locked_elsewhere: false,
            instances: Vec::new(),
        }
    } else {
        scan_orphaned_backends(state)
    };
    let open = MenuItem::with_id(
        app,
        "open-window",
        tray_text(
            &locale,
            "msg.open_rdt_connect.4fbc22fd",
            "Open RDT Connect",
            &[],
        ),
        true,
        None::<&str>,
    )?;
    let status_text = if starting {
        format!(
            "🟡 {}",
            tray_text(
                &locale,
                "msg.server_starting_or_stopping.a034115e",
                "Server starting or stopping",
                &[]
            )
        )
    } else if status.running {
        let port = status.port.unwrap_or(0).to_string();
        format!(
            "🟢 {} — {}",
            tray_text(&locale, "msg.stop_server.a21bf605", "Stop server", &[]),
            tray_text(
                &locale,
                "msg.server_running_127_0_0_1_arg1.4bda3b7f",
                "Server running · 127.0.0.1:{arg1}",
                &[("arg1", port)]
            ),
        )
    } else if orphan_scan.manager_locked_elsewhere {
        format!(
            "🟡 {}",
            tray_text(
                &locale,
                "msg.managed_by_another_open_rdt_connect_window.32399247",
                "Managed by another open RDT Connect window",
                &[]
            )
        )
    } else if let Some(orphan) = orphan_scan.instances.first() {
        let port = orphan
            .port
            .map(|p| p.to_string())
            .unwrap_or_else(|| "unknown".into());
        format!(
            "🟡 {}",
            tray_text(
                &locale,
                "msg.previous_server_needs_attention_port_arg1.410422c4",
                "Previous server needs attention · port {arg1}",
                &[("arg1", port)]
            )
        )
    } else {
        format!(
            "🔴 {}",
            tray_text(
                &locale,
                "msg.server_stopped.4c906946",
                "Server stopped",
                &[]
            )
        )
    };
    // Small, understated native macOS status dot. The status item's text remains the accessible label.
    // While running, this green status row is the single tray-menu Stop action.
    // The main-window Stop Server button remains unchanged and uses the same cleanup routine.
    let status_item = MenuItem::with_id(
        app,
        "server-status",
        status_text,
        status.running && !starting,
        None::<&str>,
    )?;

    let voice_item = if tray_voice_preflight_passes(state, &status) {
        Some(MenuItem::with_id(
            app,
            "open-voice",
            tray_text(
                &locale,
                "msg.open_rdt_voice.755ee731",
                "Open RDT Voice",
                &[],
            ),
            true,
            None::<&str>,
        )?)
    } else {
        None
    };

    // Don't duplicate Stop in the tray when the green status row is already the Stop action.
    // Keep Stop available for orphan cleanup when the managed server is not running.
    let action = if status.running {
        None
    } else {
        let can_stop_orphan =
            !starting && !orphan_scan.manager_locked_elsewhere && !orphan_scan.instances.is_empty();
        let can_start =
            !starting && !orphan_scan.manager_locked_elsewhere && orphan_scan.instances.is_empty();
        let action_id = if can_stop_orphan {
            "stop-server"
        } else {
            "start-server"
        };
        let action_label = if can_stop_orphan {
            tray_text(&locale, "msg.stop_server.a21bf605", "Stop server", &[])
        } else {
            tray_text(&locale, "msg.start_server.f08a9813", "Start Server", &[])
        };
        let action_enabled = if can_stop_orphan { true } else { can_start };
        Some(MenuItem::with_id(
            app,
            action_id,
            action_label,
            action_enabled,
            None::<&str>,
        )?)
    };
    let quit = MenuItem::with_id(
        app,
        "quit-app",
        tray_text(
            &locale,
            "msg.quit_rdt_connect.9dcc650d",
            "Quit RDT Connect",
            &[],
        ),
        true,
        None::<&str>,
    )?;
    let mut items: Vec<&dyn IsMenuItem<tauri::Wry>> = vec![&open, &status_item];
    if let Some(item) = voice_item.as_ref() {
        items.push(item);
    }
    if let Some(item) = action.as_ref() {
        items.push(item);
    }
    items.push(&quit);
    Menu::with_items(app, &items)
}

fn refresh_tray_menu(app: &AppHandle) {
    let refresh_app = app.clone();

    if let Err(error) = app.run_on_main_thread(move || {
        let Some(state) = refresh_app.try_state::<BackendState>() else {
            eprintln!("[RDT Connect] Tray refresh skipped: backend state unavailable");
            return;
        };
        let Some(tray) = refresh_app.tray_by_id("rdt-connect-menu-bar") else {
            eprintln!("[RDT Connect] Tray refresh skipped: menu-bar tray not found");
            return;
        };

        match build_tray_menu(&refresh_app, &state) {
            Ok(menu) => {
                if let Err(error) = tray.set_menu(Some(menu)) {
                    eprintln!("[RDT Connect] Could not apply refreshed tray menu: {error}");
                }
            }
            Err(error) => {
                eprintln!("[RDT Connect] Could not rebuild tray menu: {error}");
            }
        }
    }) {
        eprintln!("[RDT Connect] Could not schedule tray refresh: {error}");
    }
}

#[cfg(target_os = "macos")]
fn restore_dock_icon() {
    use objc2::{AllocAnyThread, MainThreadMarker};
    use objc2_app_kit::{NSApplication, NSImage};
    use objc2_foundation::NSData;

    let Some(mtm) = MainThreadMarker::new() else {
        eprintln!("[RDT Connect] Dock icon restoration requires the main thread");
        return;
    };

    let data = NSData::with_bytes(include_bytes!("../icons/icon.icns"));
    let Some(image) = NSImage::initWithData(NSImage::alloc(), &data) else {
        eprintln!("[RDT Connect] Could not load Dock icon");
        return;
    };

    unsafe {
        NSApplication::sharedApplication(mtm).setApplicationIconImage(Some(&image));
    }
}

fn is_rdt_connect_open_url(url: &url::Url) -> bool {
    url.scheme() == "rdtconnect"
        && url.host_str() == Some("open")
        && matches!(url.path(), "" | "/")
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
}

fn show_main_window(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
        restore_dock_icon();
    }
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn hide_main_window_to_menu_bar(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    }
}

#[cfg(target_os = "macos")]
fn acquire_single_instance_lock() -> Result<Option<std::fs::File>, String> {
    use fs2::FileExt;
    use std::io::Write;
    use std::path::PathBuf;

    let home = std::env::var_os("HOME")
        .ok_or_else(|| "HOME is unavailable; cannot secure the single-instance lock".to_string())?;
    let support_dir = PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("RDT Connect");
    std::fs::create_dir_all(&support_dir)
        .map_err(|e| format!("cannot create the RDT Connect support directory: {e}"))?;
    let lock_path = support_dir.join("rdt-connect-single-instance.lock");
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|e| format!("cannot open the single-instance lock: {e}"))?;

    match file.try_lock_exclusive() {
        Ok(()) => {
            file.set_len(0)
                .map_err(|e| format!("cannot reset lock diagnostics: {e}"))?;
            writeln!(file, "pid={}", std::process::id())
                .map_err(|e| format!("cannot write lock diagnostics: {e}"))?;
            file.sync_all()
                .map_err(|e| format!("cannot flush lock diagnostics: {e}"))?;
            Ok(Some(file))
        }
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(None),
        Err(error) => Err(format!("cannot acquire the single-instance lock: {error}")),
    }
}

#[cfg(target_os = "macos")]
fn single_instance_diagnostics() -> String {
    let pid = std::process::id();
    let path = std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .map(|home| {
            home.join("Library")
                .join("Application Support")
                .join("RDT Connect")
                .join("rdt-connect-single-instance.lock")
        });
    let expected_pid = format!("pid={pid}");
    match path.and_then(|path| std::fs::read_to_string(path).ok()) {
        Some(contents) if contents.lines().any(|line| line.trim() == expected_pid.as_str()) =>
            format!("Single-instance lock: HELD by this manager (PID {pid}); duplicate launches should activate this window."),
        Some(_contents) =>
            format!("Single-instance lock: WARNING — lock metadata does not identify this process (PID {pid}); contents were not exposed."),
        None =>
            format!("Single-instance lock: WARNING — lock metadata unavailable for this manager (PID {pid})."),
    }
}

#[cfg(not(target_os = "macos"))]
fn single_instance_diagnostics() -> String {
    "Single-instance handling: delegated to the Tauri single-instance plugin on this platform."
        .into()
}

#[cfg(target_os = "macos")]
fn forward_activation_to_existing_instance(identifier: &str) -> Result<(), String> {
    use std::io::Write;
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, Instant};

    // Match the official Tauri plugin's version-independent macOS socket path.
    let socket_name = identifier.replace(['.', '-'].as_ref(), "_");
    let socket_path = PathBuf::from(format!("/tmp/{}_si.sock", socket_name));
    let cwd = std::env::current_dir()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let args = std::env::args_os()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("\0");
    let deadline = Instant::now() + Duration::from_secs(5);

    loop {
        match UnixStream::connect(&socket_path) {
            Ok(mut stream) => {
                stream
                    .write_all(cwd.as_bytes())
                    .and_then(|_| stream.write_all(b"\0\0"))
                    .and_then(|_| stream.write_all(args.as_bytes()))
                    .map_err(|e| {
                        format!("could not forward activation to the existing manager: {e}")
                    })?;
                drop(stream); // The Tauri plugin parses the request after EOF.
                return Ok(());
            }
            Err(error) => {
                if Instant::now() >= deadline {
                    return Err(format!("existing RDT Connect holds the singleton lock, but activation could not be forwarded: {error}"));
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

#[tauri::command]
fn open_about_link(kind: String, locale: Option<String>) -> Result<(), String> {
    const GITHUB: &str = "https://github.com/RolanDorisTech/rdt-voice";
    const WEBSITE: &str = "https://rolandoristech.github.io/rdt-voice/";
    const EMAIL: &str = "mailto:woknewsbusiness@gmail.com";
    let target = match kind.as_str() {
        "github" => GITHUB.to_string(),
        "website" => {
            let mut url = url::Url::parse(WEBSITE).map_err(|error| {
                format!("Could not build the official RDT Voice website URL: {error}")
            })?;
            let selected_locale = locale
                .as_deref()
                .filter(|value| supported_locale(value))
                .unwrap_or("en");
            url.query_pairs_mut()
                .append_pair("rdt_connect_lang", selected_locale);
            url.to_string()
        }
        "email" => EMAIL.to_string(),
        "rdt-voice-license" => "https://github.com/RolanDorisTech/rdt-voice/blob/main/LICENSE".to_string(),
        "commercial-license" => "https://github.com/RolanDorisTech/rdt-voice/blob/main/COMMERCIAL_LICENSE.md".to_string(),
        "license-history" => "https://github.com/RolanDorisTech/rdt-voice/blob/main/LICENSE_HISTORY.md".to_string(),
        "third-party-acknowledgements" => "https://github.com/RolanDorisTech/rdt-voice/blob/main/README.md#acknowledgements-and-third-party-licenses".to_string(),

        _ => return Err("Unknown About link; no external target was opened.".into()),
    };
    #[cfg(target_os = "macos")]
    {
        Command::new("/usr/bin/open")
            .arg(&target)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("Could not open the selected About link: {error}"))
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(&target, Some("default"))
            .map_err(|error| format!("Could not open the selected About link: {error}"))
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&target)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("Could not open the selected About link: {error}"))
    }
}

#[tauri::command]
fn open_youtube_channel() -> Result<(), String> {
    const URL: &str = "https://youtube.com/@RolanDorisTech";
    #[cfg(target_os = "macos")]
    {
        Command::new("/usr/bin/open")
            .arg(URL)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|error| {
                format!("Could not open the Rolan & Doris Tech YouTube channel: {error}")
            })
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(URL, Some("default")).map_err(|error| {
            format!("Could not open the Rolan & Doris Tech YouTube channel: {error}")
        })
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(URL)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|error| {
                format!("Could not open the Rolan & Doris Tech YouTube channel: {error}")
            })
    }
}

#[tauri::command]
fn get_server_token(state: State<'_, BackendState>) -> Result<String, String> {
    let token = fs::read_to_string(&state.token_path).map_err(|_| {
        "The access token has not been created yet. Start the server once, then try again."
            .to_string()
    })?;
    let token = token.trim();
    if token.len() < 32 {
        return Err(
            "The access token file is empty or invalid. Inspect it in local diagnostics.".into(),
        );
    }
    Ok(token.to_string())
}

fn main() {
    let context = tauri::generate_context!();

    // macOS: secure admission atomically before Tauri can create a window or start services.
    #[cfg(target_os = "macos")]
    let _instance_lock = match acquire_single_instance_lock() {
        Ok(Some(lock)) => lock,
        Ok(None) => match forward_activation_to_existing_instance(&context.config().identifier) {
            Ok(()) => {
                eprintln!("[RDT Connect] Duplicate launch forwarded to the existing instance; this process will not start.");
                return;
            }
            Err(first_error) => match acquire_single_instance_lock() {
                Ok(Some(lock)) => {
                    eprintln!("[RDT Connect] Previous manager exited during launch handoff; taking the released singleton lock.");
                    lock
                }
                Ok(None) => {
                    eprintln!("[RDT Connect] Refusing to start a second manager: {first_error}");
                    return;
                }
                Err(error) => {
                    eprintln!("[RDT Connect] Refusing to start because singleton state cannot be verified: {error}");
                    return;
                }
            },
        },
        Err(error) => {
            eprintln!("[RDT Connect] Refusing to start because the single-instance lock could not be secured: {error}");
            return;
        }
    };

    let app = tauri::Builder::default()
        // Keep single-instance first so duplicate URL launches are forwarded.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            eprintln!("[RDT Connect] Duplicate launch received; activating existing instance.");
            show_main_window(app);
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let resources = resolve_resource_dir(app.handle()).map_err(std::io::Error::other)?;
            let state = BackendState::new(resources).map_err(std::io::Error::other)?;
            app.manage(state);

            // Handle the custom URL on both cold launch and later clicks.
            let initial_deep_links = app
                .deep_link()
                .get_current()
                .map_err(std::io::Error::other)?;

            let deep_link_app = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                if event.urls().iter().any(is_rdt_connect_open_url) {
                    show_main_window(&deep_link_app);
                }
            });

            if initial_deep_links
                .as_ref()
                .is_some_and(|urls| urls.iter().any(is_rdt_connect_open_url))
            {
                show_main_window(app.handle());
            }

            let state = app.state::<BackendState>();
            #[cfg(target_os = "macos")]
            {
                let app_about = PredefinedMenuItem::about(app, Some("About RDT Connect"), None)?;
                let app_updates = MenuItem::with_id(
                    app,
                    "app-menu-check-updates",
                    "Check for Updates",
                    true,
                    None::<&str>,
                )?;
                let app_separator = PredefinedMenuItem::separator(app)?;
                let app_quit =
                    PredefinedMenuItem::quit(app, Some("Quit RDT Connect"))?;
                let app_submenu = Submenu::with_items(
                    app,
                    "RDT Connect",
                    true,
                    &[&app_about, &app_updates, &app_separator, &app_quit],
                )?;

                let file_close =
                    PredefinedMenuItem::close_window(app, Some("Close Window"))?;
                let file_submenu =
                    Submenu::with_items(app, "File", true, &[&file_close])?;

                let undo = PredefinedMenuItem::undo(app, None)?;
                let redo = PredefinedMenuItem::redo(app, None)?;
                let edit_separator = PredefinedMenuItem::separator(app)?;
                let cut = PredefinedMenuItem::cut(app, None)?;
                let copy = PredefinedMenuItem::copy(app, None)?;
                let paste = PredefinedMenuItem::paste(app, None)?;
                let select_all = PredefinedMenuItem::select_all(app, None)?;
                let edit_submenu = Submenu::with_items(
                    app,
                    "Edit",
                    true,
                    &[&undo, &redo, &edit_separator, &cut, &copy, &paste, &select_all],
                )?;

                let minimize = PredefinedMenuItem::minimize(app, None)?;
                let window_close =
                    PredefinedMenuItem::close_window(app, Some("Close Window"))?;
                let window_submenu =
                    Submenu::with_items(app, "Window", true, &[&minimize, &window_close])?;

                let app_menu = Menu::with_items(
                    app,
                    &[&app_submenu, &file_submenu, &edit_submenu, &window_submenu],
                )?;
                app.set_menu(app_menu)?;
            }

            app.on_menu_event(|app, event| {
                match event.id().as_ref() {
                    "app-menu-check-updates" => {
                        if let Err(error) = show_update_window(app) {
                            eprintln!("[RDT Connect] Could not open update status window: {error}");
                        }
                    }
                    _ => {}
                }
            });

            let menu = build_tray_menu(app.handle(), &state).map_err(std::io::Error::other)?;
            // Black-and-clear template mask adapts to macOS appearance; the Dock icon stays full color.
            TrayIconBuilder::with_id("rdt-connect-menu-bar")
                .icon(tauri::include_image!("icons/rdt-menu-bar-icon.png"))
                .icon_as_template(false)
                .menu(&menu)
                .tooltip("RDT Connect")
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open-window" => show_main_window(app),
                    "server-status" | "stop-server" => {
                        if let Some(state) = app.try_state::<BackendState>() {
                            if let Err(error) = stop_owned_and_orphan_backends(&state) {
                                eprintln!("[RDT Connect] Menu-bar Stop could not confirm cleanup: {error}");
                                show_main_window(app);
                                let _ = app.emit("rdt-connect-stop-failed", error);
                            } else {
                                let _ = app.emit("rdt-backend-state-changed", "stopped");
                            }
                        }
                        refresh_tray_menu(app);
                    }
                    "open-voice" => {
                        if let Some(state) = app.try_state::<BackendState>() {
                            if let Err(error) = open_voice_window(app.clone(), state, None, None) {
                                eprintln!("[RDT Connect] Menu-bar Open RDT Voice failed: {error}");
                                show_main_window(app);
                                let _ = app.emit("rdt-tray-open-voice-failed", error);
                            }
                        }
                        refresh_tray_menu(app);
                    }
                    "start-server" => {
                        let Some(state) = app.try_state::<BackendState>() else { return; };
                        if state.menu_start_in_progress.swap(true, Ordering::SeqCst) { return; }
                        refresh_tray_menu(app);
                        let start_app = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let read_app = start_app.clone();
                            let choice_and_cache = tauri::async_runtime::spawn_blocking(move || {
                                let state = read_app.state::<BackendState>();
                                let choice = read_last_server_choice(&state);
                                let cache = make_model_storage_status(&state, &choice.model);
                                (choice, cache)
                            }).await;
                            match choice_and_cache {
                                Ok((choice, Ok(cache))) if cache.model_cached => {
                                    if let Err(error) = start_backend(start_app.clone(), choice.model, choice.preferred_port).await {
                                        show_main_window(&start_app);
                                        let _ = start_app.emit("rdt-tray-start-failed", error);
                                    } else {
                                        let _ = start_app.emit("rdt-backend-state-changed", "running");
                                    }
                                }
                                Ok((choice, Ok(_))) => {
                                    show_main_window(&start_app);
                                    let _ = start_app.emit("rdt-tray-model-needs-download", choice.model);
                                }
                                Ok((_choice, Err(error))) => {
                                    show_main_window(&start_app);
                                    let _ = start_app.emit("rdt-tray-start-failed", error);
                                }
                                Err(error) => {
                                    show_main_window(&start_app);
                                    let _ = start_app.emit("rdt-tray-start-failed", format!("Could not check the selected model before starting: {error}"));
                                }
                            }
                            if let Some(state) = start_app.try_state::<BackendState>() {
                                state.menu_start_in_progress.store(false, Ordering::SeqCst);
                            }
                            // Refresh once more after clearing the busy guard so the menu shows the settled status.
                            refresh_tray_menu(&start_app);
                        });
                    }
                    "quit-app" => app.exit(0),
                    _ => {}
                })
                // Left-click presents the menu only. Opening the window is an explicit
                // menu action, so a click cannot immediately dismiss the menu by raising
                // the Dock window underneath it.
                .build(app)?;
            // Respect the saved visibility preference on every launch. When disabled,
            // the application remains a normal Dock/window app but has no menu-bar item.
            if let Some(tray) = app.tray_by_id("rdt-connect-menu-bar") {
                tray.set_visible(state.keep_in_menu_bar()).map_err(std::io::Error::other)?;
            }
            // Do not replace the active NSStatusItem menu on a timer. Replacing the
            // menu every few seconds dismisses the user's open menu. Status is refreshed
            // after app-managed Start/Stop/orphan cleanup actions instead.
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_app_build_info, get_backend_status, get_platform_model_catalog, get_backend_connection_status, open_backend_log, get_recent_backend_log, get_app_diagnostics, get_orphaned_backends, stop_orphaned_backend,
            get_keep_in_menu_bar, set_keep_in_menu_bar, set_app_language, remember_recent_server_choice, start_backend, stop_backend, list_available_browsers, open_voice_window,
            get_model_storage_status, set_model_storage_locations, pick_model_storage_directory,
            get_model_download_status, start_model_download, cancel_model_download, delete_rdt_model_caches, open_easywhisper_model_manager,
            get_local_https_status, pick_tls_file, setup_local_https_certificate, link_local_https_certificate, trust_local_https_ca, remove_rdt_https_trust, set_local_transport,
            get_tailscale_status, get_remote_access_diagnostics, enable_private_access, generate_pairing_qr,
            list_pair_requests, approve_pair_request, deny_pair_request,
            list_paired_devices, revoke_paired_device, open_about_link, open_youtube_channel, get_server_token
        ])
        .build(context)
        .expect("error while building RDT Connect");

    app.run(|handle, event| match event {
        tauri::RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::CloseRequested { api, .. },
            ..
        } if label == "main" => {
            let keep = handle
                .try_state::<BackendState>()
                .map(|state| state.keep_in_menu_bar())
                .unwrap_or(false);
            if keep {
                api.prevent_close();
                hide_main_window_to_menu_bar(handle);
            } else {
                // Run managed-server cleanup before asking Tauri to exit.
                if let Some(state) = handle.try_state::<BackendState>() {
                    let _ = stop_owned_and_orphan_backends(&state);
                }
                handle.exit(0);
            }
        }
        tauri::RunEvent::ExitRequested { api, .. } => {
            // A deliberate Quit must not succeed while a verified managed backend survives.
            // If cleanup fails, keep the app alive, restore its window, and show the reason.
            if let Some(state) = handle.try_state::<BackendState>() {
                if let Err(error) = stop_owned_and_orphan_backends(&state) {
                    eprintln!(
                        "[RDT Connect] Quit cancelled because backend cleanup failed: {error}"
                    );
                    api.prevent_exit();
                    show_main_window(handle);
                    let _ = handle.emit("rdt-connect-stop-failed", error);
                }
            }
        }
        // Normal launch never goes through show_main_window(), because the main window is
        // created visible from tauri.conf.json. Apply the Dock icon once the NSApplication
        // has finished launching so the runtime override actually runs on every start.
        tauri::RunEvent::Ready => {
            #[cfg(target_os = "macos")]
            restore_dock_icon();
        }
        tauri::RunEvent::Exit => {
            // Idempotent final safety net.
            if let Some(state) = handle.try_state::<BackendState>() {
                let _ = stop_owned_and_orphan_backends(&state);
            }
        }
        _ => {}
    });
}

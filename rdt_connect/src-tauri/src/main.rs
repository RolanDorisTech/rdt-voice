#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use reqwest::blocking::{Client, RequestBuilder};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
#[cfg(windows)]
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
    atomic::{AtomicBool, AtomicU8, Ordering},
    Arc, Mutex, OnceLock,
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
#[cfg(target_os = "macos")]
use tauri::menu::{PredefinedMenuItem, Submenu};
use tauri::{
    menu::{IsMenuItem, Menu, MenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder,
};
#[cfg(target_os = "macos")]
use tauri_plugin_deep_link::DeepLinkExt;

mod updater_version_policy;

#[cfg(target_os = "linux")]
mod linux_tls;

// Serializes certificate/trust changes now that they run on worker threads.
static LOCAL_TLS_OPERATION: Mutex<()> = Mutex::new(());
static FIREWORKS_SESSION_SEEN: AtomicBool = AtomicBool::new(false);
static FIREWORKS_COUNTER_LOCK: Mutex<()> = Mutex::new(());
static HARDWARE_CANDIDATE_PRIORITY_LOCK: Mutex<()> = Mutex::new(());

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

fn write_model_storage_config_atomic(config_path: &Path, payload: &[u8]) -> Result<(), String> {
    let parent = config_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| "Model-storage settings path has no parent directory.".to_string())?;
    let filename = config_path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .ok_or_else(|| "Model-storage settings path has no filename.".to_string())?;

    let mut created = None;
    for attempt in 0..64u32 {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let temporary = parent.join(format!(
            ".{filename}.tmp-{}-{nonce}-{attempt}",
            std::process::id()
        ));

        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
        {
            Ok(file) => {
                created = Some((temporary, file));
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "Could not create temporary model-storage settings file: {error}"
                ));
            }
        }
    }

    let (temporary, mut file) = created.ok_or_else(|| {
        "Could not allocate a unique temporary model-storage settings file after 64 attempts."
            .to_string()
    })?;

    let result = (|| -> Result<(), String> {
        // Preserve the existing best-effort permission behavior across platforms.
        let _ = set_private_permissions(&temporary, 0o600);

        file.write_all(payload)
            .map_err(|error| format!("Could not write model-storage settings: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("Could not sync model-storage settings: {error}"))?;
        drop(file);

        fs::rename(&temporary, config_path)
            .map_err(|error| format!("Could not replace model-storage settings: {error}"))?;

        // The file is already replaced at this point. Report a directory-sync
        // failure accurately rather than implying that the old file remains.
        #[cfg(unix)]
        {
            let directory = File::open(parent).map_err(|error| {
                format!(
                    "Model-storage settings were replaced, but the containing directory \
                     could not be opened for sync: {error}"
                )
            })?;
            directory.sync_all().map_err(|error| {
                format!(
                    "Model-storage settings were replaced, but the containing directory \
                     could not be synced: {error}"
                )
            })?;
        }

        Ok(())
    })();

    if result.is_err() {
        // This removes only the temporary pathname created above. After a
        // successful rename it no longer exists, so the destination is untouched.
        let _ = fs::remove_file(&temporary);
    }

    result
}

const SIGNAL_TERM: i32 = 15;
const SIGNAL_KILL: i32 = 9;

fn application_support_dir(app_name: &str) -> Result<PathBuf, String> {
    #[cfg(target_os = "macos")]
    {
        return Ok(home_dir()?
            .join("Library")
            .join("Application Support")
            .join(app_name));
    }
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| {
                home_dir()
                    .ok()
                    .map(|home| home.join("AppData").join("Local"))
            })
            .ok_or_else(|| {
                "Could not determine the current user's local application-data folder.".to_string()
            })?;
        return Ok(base.join(app_name));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| {
                home_dir()
                    .ok()
                    .map(|home| home.join(".local").join("share"))
            })
            .ok_or_else(|| {
                "Could not determine the current user's application-data folder.".to_string()
            })?;
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
            .or_else(|| {
                home_dir()
                    .ok()
                    .map(|home| home.join(".local").join("state"))
            })
            .ok_or_else(|| "Could not determine the current user's log folder.".to_string())?;
        return Ok(base.join(app_name));
    }
}

fn app_version_label() -> String {
    let version = env!("CARGO_PKG_VERSION");
    if let Some((base, marker)) = version.rsplit_once('.') {
        let bytes = marker.as_bytes();
        let mac_or_windows_marker = bytes.len() == 4
            && (b'1'..=b'3').contains(&bytes[0])
            && bytes[1].is_ascii_uppercase()
            && bytes[2..].iter().all(u8::is_ascii_digit);
        let linux_marker = bytes.len() == 5
            && (bytes[0] == b'4' || bytes[0] == b'5')
            && bytes[1] == b'L'
            && bytes[2..].iter().all(u8::is_ascii_digit);
        if mac_or_windows_marker || linux_marker {
            return format!("v{base}");
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
    applied_inference_device_preference: Option<String>,
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
    model_download_finalization: Arc<Mutex<()>>,
    resource_dir: PathBuf,
    token_path: PathBuf,
    log_path: PathBuf,
    lock_path: PathBuf,
    menu_bar_pref_path: PathBuf,
    https_dir: PathBuf,
    tls_config_path: PathBuf,
    model_storage_config_path: PathBuf,
    app_language_pref_path: PathBuf,
    offline_only_mode_pref_path: PathBuf,
    inference_device_pref_path: PathBuf,
    last_server_choice_path: PathBuf,
    preferred_browser_path: PathBuf,
    fireworks_lifetime_count_path: PathBuf,
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
    applied_inference_device_preference: Option<String>,
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
        (
            "tiny",
            "ggml-tiny-q8_0.bin",
            "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca",
        ),
        (
            "base",
            "ggml-base-q8_0.bin",
            "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9",
        ),
        (
            "small",
            "ggml-small-q8_0.bin",
            "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f",
        ),
        (
            "large-v3-turbo",
            "ggml-large-v3-turbo-q5_0.bin",
            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        ),
        (
            "large-v3",
            "ggml-large-v3-q5_0.bin",
            "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
        ),
    ];
    let _ = REVISION;
    MODELS
        .iter()
        .find(|(id, _, _)| *id == model)
        .map(|(_, filename, sha)| (*filename, *sha, REVISION))
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
    // SHA-256 pins for RDT-generated Linux TLS files (same purpose as the Windows pins).
    linux_certificate_sha256: Option<String>,
    linux_private_key_sha256: Option<String>,
    linux_ca_sha256: Option<String>,
    // Only fingerprints explicitly imported by RDT Connect may be removed by its UI.
    managed_trust_fingerprints: Vec<String>,
}
impl Default for LocalTlsConfig {
    fn default() -> Self {
        Self {
            mode: if cfg!(any(
                target_os = "macos",
                target_os = "windows",
                target_os = "linux"
            )) {
                "https"
            } else {
                "http"
            }
            .into(),
            certificate_path: None,
            private_key_path: None,
            ca_certificate_path: None,
            generated: false,
            windows_certificate_sha256: None,
            windows_private_key_sha256: None,
            windows_ca_sha256: None,
            linux_certificate_sha256: None,
            linux_private_key_sha256: None,
            linux_ca_sha256: None,
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
        if let (Some(drive), Some(path)) =
            (std::env::var_os("HOMEDRIVE"), std::env::var_os("HOMEPATH"))
        {
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
    if cfg!(windows) {
        "rdt_voice_backend.exe"
    } else {
        "rdt_voice_backend"
    }
}

fn backend_executable_path(resource_dir: &Path) -> PathBuf {
    resource_dir
        .join("rdt_voice_backend")
        .join(backend_executable_name())
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
        let offline_only_mode_pref_path = rdt_support.join("offline-only-mode");
        let inference_device_pref_path = rdt_support.join("inference-device");
        let last_server_choice_path = rdt_support.join("last-server-choice.json");
        let preferred_browser_path = rdt_support.join("preferred-browser");
        let fireworks_lifetime_count_path = rdt_support.join("fireworks-lifetime-count");
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
            model_download_finalization: Arc::new(Mutex::new(())),
            resource_dir,
            token_path,
            log_path,
            lock_path,
            menu_bar_pref_path,
            https_dir,
            tls_config_path,
            model_storage_config_path,
            app_language_pref_path,
            offline_only_mode_pref_path,
            inference_device_pref_path,
            last_server_choice_path,
            preferred_browser_path,
            fireworks_lifetime_count_path,
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
                // An inspection error does not prove that the backend exited.
                Err(_) => true,
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
            inner.applied_inference_device_preference = None;
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
            applied_inference_device_preference: inner.applied_inference_device_preference.clone(),
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
        let (
            mut child,
            process_group,
            managed_serve_port,
            managed_lock,
            old_port,
            old_model,
            old_applied_inference_device_preference,
        ) = {
            let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
            (
                inner.child.take(),
                inner.process_group.take(),
                inner.managed_serve_port.take(),
                inner.managed_lock.take(),
                inner.port,
                inner.model.clone(),
                inner.applied_inference_device_preference.clone(),
            )
        };
        // Remember the actual managed listener before releasing process ownership.
        let verification_port =
            if managed_serve_port.is_some() || process_group.is_some() || child.is_some() {
                managed_serve_port.or(old_port)
            } else {
                None
            };
        let stopped_pid = process_group
            .or_else(|| child.as_ref().map(|process| process.id()))
            .unwrap_or(0);
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
            inner.applied_inference_device_preference =
                old_applied_inference_device_preference.clone();
            inner.managed_serve_port = managed_serve_port;
            inner.managed_lock = managed_lock;
            return Err(format!("Stop did not complete: managed process group {} is still alive after termination was requested. Ownership was retained; retry Stop or inspect the process details.", group.unwrap_or(0)));
        }
        // Process-group exit alone is not enough: prove the managed listener
        // disappeared before clearing state or releasing the process-wide lock.
        if let Some(port) = verification_port {
            if let Err(error) = verify_port_released(port, stopped_pid) {
                let group = process_group;
                let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
                inner.child = child;
                inner.process_group = group;
                inner.port = old_port;
                inner.model = old_model;
                inner.applied_inference_device_preference =
                    old_applied_inference_device_preference.clone();
                inner.managed_serve_port = managed_serve_port;
                inner.managed_lock = managed_lock;
                return Err(format!(
                    "The managed backend process group terminated, but TCP port {port} could not be verified free. The lifecycle lock was retained; RDT Connect did not stop another process. {error}"
                ));
            }
        }
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.child = None;
        inner.process_group = None;
        inner.port = None;
        inner.model = None;
        inner.applied_inference_device_preference = None;
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
    match std::io::Error::last_os_error().raw_os_error() {
        Some(libc::EPERM) => true,
        Some(libc::ESRCH) => false,
        // Only ESRCH proves that the process group is absent.
        _ => true,
    }
}

// A zombie no longer owns a listening socket. For orphan cleanup, don't let a
// zombie awaiting reaping keep Stop stuck forever after the server has exited.

#[cfg(windows)]
fn process_group_alive(group_id: u32) -> bool {
    match process_snapshot_result() {
        Ok(snapshot) => {
            snapshot
                .iter()
                .any(|item| item.pid == group_id && !item.stat.starts_with('Z'))
                || snapshot
                    .iter()
                    .any(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
                || !windows_descendant_pids(group_id, &snapshot).is_empty()
        }
        Err(_) => true,
    }
}

#[cfg(windows)]
fn windows_descendant_pids(
    root_pid: u32,
    snapshot: &[ProcessSnapshot],
) -> std::collections::HashSet<u32> {
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
const WINDOWS_CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[cfg(windows)]
fn windows_taskkill_command(pid: u32, is_tree: bool, force: bool) -> Command {
    let mut command = Command::new("taskkill.exe");
    command.creation_flags(WINDOWS_CREATE_NO_WINDOW);
    command.arg("/PID").arg(pid.to_string());
    if is_tree {
        command.arg("/T");
    }
    if force {
        command.arg("/F");
    }
    command
}

#[cfg(windows)]
fn send_signal_checked(target: i32, signal: i32) -> Result<(), String> {
    let is_tree = target < 0;
    let pid = target.unsigned_abs();
    if pid < 2 {
        return Err("Safety check refused to signal an invalid Windows process ID.".into());
    }

    let snapshot = process_snapshot_result()?;
    let process_is_present = snapshot
        .iter()
        .any(|item| item.pid == pid && !item.stat.starts_with('Z'));
    let descendants = windows_descendant_pids(pid, &snapshot);
    let present = if is_tree {
        process_is_present
            || snapshot
                .iter()
                .any(|item| item.pgid == pid && !item.stat.starts_with('Z'))
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
        let Some(root) = snapshot
            .iter()
            .find(|item| item.pid == pid && !item.stat.starts_with('Z'))
        else {
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
    let mut command = windows_taskkill_command(pid, is_tree, signal == SIGNAL_KILL);
    let output = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("Could not run Windows taskkill for PID {pid}: {error}"))?;
    if output.status.success() {
        return Ok(());
    }

    let after = process_snapshot_result()?;
    let still_present = if is_tree {
        after
            .iter()
            .any(|item| item.pid == pid && !item.stat.starts_with('Z'))
            || after
                .iter()
                .any(|item| item.pgid == pid && !item.stat.starts_with('Z'))
            || !windows_descendant_pids(pid, &after).is_empty()
    } else {
        after
            .iter()
            .any(|item| item.pid == pid && !item.stat.starts_with('Z'))
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
        let wait_deadline = Instant::now() + Duration::from_secs(10);
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

#[cfg(windows)]
fn process_group_has_live_member(group_id: u32) -> bool {
    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return true;
    }
    snapshot
        .iter()
        .any(|item| item.pid == group_id && !item.stat.starts_with('Z'))
        || snapshot
            .iter()
            .any(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
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
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("Could not inspect {description} with PowerShell: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Could not inspect {description}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if raw.is_empty() || raw == "null" {
        return Ok(Vec::new());
    }
    let value: Value = serde_json::from_str(&raw)
        .map_err(|error| format!("Could not parse Windows {description} output: {error}"))?;
    let values: Vec<Value> = match value {
        Value::Array(items) => items,
        Value::Null => Vec::new(),
        single => vec![single],
    };
    Ok(values
        .into_iter()
        .filter_map(|value| {
            value
                .as_u64()
                .or_else(|| value.as_str().and_then(|s| s.parse().ok()))
                .map(|n| n as u32)
        })
        .collect())
}

#[cfg(windows)]
fn listening_pids_on_port(port: u16) -> Result<Vec<u32>, String> {
    let script = format!("$ErrorActionPreference='Stop'; $values=@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); ConvertTo-Json -InputObject $values -Compress");
    let mut values =
        powershell_json_u32_list(&script, &format!("TCP listener ownership for port {port}"))?;
    values.sort_unstable();
    values.dedup();
    Ok(values)
}

#[cfg(windows)]
fn listening_ports_for_pid(pid: u32) -> Result<Vec<u16>, String> {
    let script = format!("$ErrorActionPreference='Stop'; $values=@(Get-NetTCPConnection -State Listen -OwningProcess {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort -Unique); ConvertTo-Json -InputObject $values -Compress");
    let values = powershell_json_u32_list(&script, &format!("listening TCP ports for PID {pid}"))?;
    let mut ports: Vec<u16> = values
        .into_iter()
        .filter_map(|value| u16::try_from(value).ok())
        .filter(|port| *port > 0)
        .collect();
    ports.sort_unstable();
    ports.dedup();
    Ok(ports)
}

#[cfg(windows)]
fn describe_port_owner(port: u16) -> String {
    match listening_pids_on_port(port) {
        Ok(pids) if pids.is_empty() => {
            "No listener details were returned by the Windows TCP table.".into()
        }
        Ok(pids) => {
            let processes = process_snapshot();
            let names = pids
                .iter()
                .take(4)
                .map(|pid| {
                    processes
                        .iter()
                        .find(|item| item.pid == *pid)
                        .map(|item| {
                            format!(
                                "PID {pid}: {}",
                                item.command
                                    .split_whitespace()
                                    .take(4)
                                    .collect::<Vec<_>>()
                                    .join(" ")
                            )
                        })
                        .unwrap_or_else(|| format!("PID {pid}: process details unavailable"))
                })
                .collect::<Vec<_>>();
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
        .filter(|item| {
            item.command
                .to_ascii_lowercase()
                .contains("rdt_voice_backend")
        })
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

fn candidate_listener_group_ownership(
    identity: &ProcessSnapshot,
    listener_pids: &[u32],
    snapshot: &[ProcessSnapshot],
) -> Result<bool, String> {
    if identity.pid == 0
        || identity.pgid != identity.pid
        || identity.started.trim().is_empty()
        || identity.command.trim().is_empty()
    {
        return Err("Captured candidate process identity is incomplete or not isolated.".into());
    }
    let leader = snapshot
        .iter()
        .find(|item| item.pid == identity.pid)
        .ok_or_else(|| {
            "Captured candidate leader is absent from the process snapshot.".to_string()
        })?;
    if leader.pgid != identity.pgid
        || leader.started != identity.started
        || leader.command != identity.command
        || leader.stat.starts_with('Z')
    {
        return Err(
            "Candidate leader identity changed; possible PID reuse or process exit.".into(),
        );
    }
    if listener_pids.is_empty() {
        return Ok(false);
    }
    for pid in listener_pids {
        let owner = snapshot.iter().find(|item| item.pid == *pid)
            .ok_or_else(|| format!("Listener PID {pid} is absent from the process snapshot; ownership is unproven."))?;
        if owner.pgid != identity.pgid {
            return Err(format!(
                "Listener PID {pid} is outside candidate process group {} (actual group {}).",
                identity.pgid, owner.pgid
            ));
        }
        if owner.stat.starts_with('Z') {
            return Err(format!(
                "Listener PID {pid} is a zombie; ownership is unproven."
            ));
        }
    }
    Ok(true)
}

#[cfg(target_os = "macos")]
fn capture_candidate_process_identity(pid: u32) -> Result<ProcessSnapshot, String> {
    for _ in 0..20 {
        let snapshot = process_snapshot();
        if let Some(identity) = snapshot.iter().find(|item| item.pid == pid) {
            if identity.pgid != pid
                || identity.stat.starts_with('Z')
                || identity.started.trim().is_empty()
                || identity.command.trim().is_empty()
            {
                return Err(format!(
                    "Spawned candidate PID {pid} has no valid isolated process identity."
                ));
            }
            return Ok(identity.clone());
        }
        thread::sleep(Duration::from_millis(25));
    }
    Err(format!(
        "Could not capture spawned candidate PID {pid} and its process group."
    ))
}

#[cfg(target_os = "macos")]
fn candidate_listener_ownership_verified(
    port: u16,
    identity: &ProcessSnapshot,
) -> Result<bool, String> {
    let listener_pids = listening_pids_on_port(port)
        .map_err(|error| format!("Candidate listener ownership could not be verified: {error}"))?;
    let snapshot = process_snapshot();
    if snapshot.is_empty() {
        return Err(
            "Candidate listener ownership could not be verified: process snapshot unavailable."
                .into(),
        );
    }
    match candidate_listener_group_ownership(identity, &listener_pids, &snapshot) {
        Err(error) if error.contains("outside candidate process group") => Err(format!(
            "Candidate port {port} address already in use: {error}"
        )),
        result => result,
    }
}

fn checked_process_snapshot_output<Parse>(
    command_name: &str,
    command_output: Result<(bool, String), String>,
    parse: Parse,
) -> Result<Vec<ProcessSnapshot>, String>
where
    Parse: FnOnce(&str) -> Result<Vec<ProcessSnapshot>, String>,
{
    let (successful, stdout) = command_output.map_err(|error| {
        format!("Could not launch process-table inspection ({command_name}): {error}")
    })?;

    if !successful {
        return Err(format!(
            "Process-table inspection ({command_name}) exited unsuccessfully."
        ));
    }

    let snapshot = parse(&stdout)?;
    if snapshot.is_empty() {
        return Err(format!(
            "Process-table inspection ({command_name}) returned no process records."
        ));
    }

    Ok(snapshot)
}

fn parse_unix_process_snapshot(output: &str) -> Result<Vec<ProcessSnapshot>, String> {
    let mut snapshot = Vec::new();

    for (line_index, line) in output.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }

        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() < 10 {
            return Err(format!(
                "The Unix process table contains a malformed row {}.",
                line_index + 1
            ));
        }

        let parse_number = |index: usize, name: &str| {
            fields[index].parse::<u32>().map_err(|_| {
                format!(
                    "The Unix process table has an invalid {name} in row {}.",
                    line_index + 1
                )
            })
        };

        snapshot.push(ProcessSnapshot {
            pid: parse_number(0, "PID")?,
            ppid: parse_number(1, "parent PID")?,
            pgid: parse_number(2, "process-group ID")?,
            stat: fields[3].to_string(),
            started: fields[4..9].join(" "),
            command: fields[9..].join(" "),
        });
    }

    if snapshot.is_empty() {
        return Err("The Unix process table was empty.".into());
    }

    Ok(snapshot)
}

#[cfg_attr(not(windows), allow(dead_code))]
fn parse_windows_process_u32(row: &Value, field: &str, row_number: usize) -> Result<u32, String> {
    let value = row
        .get(field)
        .ok_or_else(|| format!("Windows process-table row {row_number} is missing {field}."))?;

    let number = value
        .as_u64()
        .or_else(|| value.as_str().and_then(|text| text.parse::<u64>().ok()))
        .ok_or_else(|| format!("Windows process-table row {row_number} has invalid {field}."))?;

    u32::try_from(number)
        .map_err(|_| format!("Windows process-table row {row_number} has out-of-range {field}."))
}

#[cfg_attr(not(windows), allow(dead_code))]
fn parse_windows_process_snapshot(value: Value) -> Result<Vec<ProcessSnapshot>, String> {
    let rows: Vec<&Value> = match &value {
        Value::Array(rows) => rows.iter().collect(),
        Value::Object(_) => vec![&value],
        _ => {
            return Err("Windows process-table output was not a JSON object or array.".into());
        }
    };

    if rows.is_empty() {
        return Err("The Windows process table was empty.".into());
    }

    let mut snapshot = Vec::with_capacity(rows.len());

    for (index, row) in rows.into_iter().enumerate() {
        let row_number = index + 1;
        if !row.is_object() {
            return Err(format!(
                "Windows process-table row {row_number} was not an object."
            ));
        }

        let pid = parse_windows_process_u32(row, "ProcessId", row_number)?;
        let ppid = parse_windows_process_u32(row, "ParentProcessId", row_number)?;
        let started = row
            .get("CreationDate")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| format!("Windows process-table row {row_number} has no creation time."))?
            .to_string();

        let exe = row
            .get("ExecutablePath")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let args = row
            .get("CommandLine")
            .and_then(Value::as_str)
            .unwrap_or_default();

        let command = if exe.is_empty() {
            args.to_string()
        } else if args.is_empty() {
            format!("\"{exe}\"")
        } else {
            format!("\"{exe}\" {args}")
        };

        snapshot.push(ProcessSnapshot {
            pid,
            ppid,
            pgid: pid,
            stat: "R".into(),
            started,
            command,
        });
    }

    Ok(snapshot)
}

#[cfg(unix)]
fn process_snapshot_result() -> Result<Vec<ProcessSnapshot>, String> {
    let command_output = Command::new("/bin/ps")
        .args(["-ww", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|output| {
            (
                output.status.success(),
                String::from_utf8_lossy(&output.stdout).into_owned(),
            )
        })
        .map_err(|error| error.to_string());

    checked_process_snapshot_output("/bin/ps", command_output, |stdout: &str| {
        parse_unix_process_snapshot(stdout)
    })
}

#[cfg(unix)]
fn process_snapshot() -> Vec<ProcessSnapshot> {
    process_snapshot_result().unwrap_or_default()
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
    let name = path
        .rsplit(|character| character == '\\' || character == '/')
        .next()
        .unwrap_or(path);
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
fn assign_windows_process_groups(snapshot: &mut [ProcessSnapshot]) {
    let parent_map: HashMap<u32, (u32, bool)> = snapshot
        .iter()
        .map(|item| {
            (
                item.pid,
                (
                    item.ppid,
                    is_windows_executable(&item.command, "rdt_voice_backend.exe")
                        || is_windows_executable(&item.command, "rdt_voice_backend"),
                ),
            )
        })
        .collect();

    for item in snapshot {
        let mut cursor = item.pid;
        let mut group_root = item.pid;
        let mut seen = std::collections::HashSet::new();

        for _ in 0..128 {
            if !seen.insert(cursor) {
                break;
            }

            let Some((parent, is_backend)) = parent_map.get(&cursor) else {
                break;
            };

            if *is_backend {
                group_root = cursor;
            }

            if *parent == 0 || *parent == cursor {
                break;
            }

            cursor = *parent;
        }

        item.pgid = group_root;
    }
}

#[cfg(windows)]
fn process_snapshot_result() -> Result<Vec<ProcessSnapshot>, String> {
    const SCRIPT: &str = "$ErrorActionPreference='Stop'; Get-CimInstance -ClassName Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress";

    let mut command = Command::new("powershell.exe");
    command.creation_flags(0x08000000);

    let command_output = command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            SCRIPT,
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|output| {
            (
                output.status.success(),
                String::from_utf8_lossy(&output.stdout).into_owned(),
            )
        })
        .map_err(|error| error.to_string());

    let mut snapshot = checked_process_snapshot_output(
        "Get-CimInstance Win32_Process",
        command_output,
        |stdout: &str| {
            let value = serde_json::from_str::<Value>(stdout)
                .map_err(|error| format!("Could not parse the Windows process table: {error}"))?;
            parse_windows_process_snapshot(value)
        },
    )?;

    assign_windows_process_groups(&mut snapshot);
    Ok(snapshot)
}

#[cfg(windows)]
fn process_snapshot() -> Vec<ProcessSnapshot> {
    process_snapshot_result().unwrap_or_default()
}

#[cfg(windows)]
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
#[cfg(windows)]
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
    snapshot.is_empty()
        || snapshot
            .iter()
            .any(|item| item.pid != std::process::id() && is_rdt_manager_command(&item.command))
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
    candidates.push(backend_executable_path(
        &manifest.join("target").join("debug"),
    ));
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
                candidate
                    .to_string_lossy()
                    .replace('/', "\\")
                    .to_ascii_lowercase()
            } else {
                candidate.display().to_string()
            };
            let canonical_text = if cfg!(windows) {
                canonical
                    .to_string_lossy()
                    .replace('/', "\\")
                    .to_ascii_lowercase()
            } else {
                canonical.display().to_string()
            };
            command_cmp.contains(&canonical_text) || command_cmp.contains(&candidate_text)
        })
}

#[cfg(target_os = "macos")]
fn process_is_expected_backend(item: &ProcessSnapshot, expected_exe: &Path) -> bool {
    use std::ffi::CStr;

    #[link(name = "proc")]
    extern "C" {
        fn proc_pidpath(
            pid: libc::c_int,
            buffer: *mut libc::c_void,
            buffersize: u32,
        ) -> libc::c_int;
    }

    let mut buffer = [0u8; 4096];
    let written = unsafe {
        proc_pidpath(
            item.pid as libc::c_int,
            buffer.as_mut_ptr().cast(),
            buffer.len() as u32,
        )
    };
    if written <= 0 {
        return false;
    }

    let Ok(path_text) =
        (unsafe { CStr::from_ptr(buffer.as_ptr().cast::<libc::c_char>()) }).to_str()
    else {
        return false;
    };

    command_is_expected_backend(path_text, expected_exe)
}

#[cfg(all(unix, not(target_os = "macos")))]
fn process_is_expected_backend(item: &ProcessSnapshot, expected_exe: &Path) -> bool {
    command_is_expected_backend(&item.command, expected_exe)
}

#[cfg(windows)]
fn process_is_expected_backend(item: &ProcessSnapshot, expected_exe: &Path) -> bool {
    command_is_expected_backend(&item.command, expected_exe)
}

fn verified_backend_process_snapshot(
    pid: u32,
    expected_exe: &Path,
) -> Result<ProcessSnapshot, String> {
    let snapshot = process_snapshot_result()
        .map_err(|error| format!("Could not verify the backend process table: {error}"))?;

    let item = snapshot
        .iter()
        .find(|item| item.pid == pid)
        .ok_or_else(|| format!("Backend PID {pid} has already exited. Refresh server status."))?;

    if !item.stat.starts_with('Z') && !process_is_expected_backend(item, expected_exe) {
        return Err(format!(
            "Safety check refused PID {pid}: its OS-reported executable does not match this packaged RDT Connect backend. No process was stopped."
        ));
    }

    Ok(item.clone())
}

/// Validate the complete process group before a group-wide signal.
/// Zombies are retained in the source snapshot but are not live workloads.
/// The baseline prevents PID reuse or a captured process changing groups.
fn validate_backend_group_snapshot<F>(
    snapshot: &[ProcessSnapshot],
    selected_pid: Option<u32>,
    group_id: u32,
    baseline: Option<&[ProcessSnapshot]>,
    is_expected_backend: F,
) -> Result<Vec<ProcessSnapshot>, String>
where
    F: Fn(&ProcessSnapshot) -> bool,
{
    if group_id <= 1 {
        return Err(format!("Refusing unsafe process group ID {group_id}."));
    }

    if let Some(pid) = selected_pid {
        let selected = snapshot
            .iter()
            .find(|item| item.pid == pid)
            .ok_or_else(|| {
                format!("Selected backend PID {pid} disappeared during verification.")
            })?;

        if selected.pgid != group_id {
            return Err(format!(
                "Selected backend PID {pid} changed process group from {group_id} to {}. No signal sent.",
                selected.pgid
            ));
        }

        if let Some(previous) = baseline.and_then(|items| items.iter().find(|item| item.pid == pid))
        {
            if previous.started != selected.started {
                return Err(format!(
                    "Selected backend PID {pid} was reused. No signal sent."
                ));
            }
        }
    }

    if let Some(previous_group) = baseline {
        for previous in previous_group.iter().filter(|item| item.pgid == group_id) {
            if let Some(current) = snapshot.iter().find(|item| item.pid == previous.pid) {
                if current.started != previous.started {
                    return Err(format!(
                        "PID {} was reused while verifying process group {group_id}. No further signal sent.",
                        previous.pid
                    ));
                }
                if current.pgid != group_id {
                    return Err(format!(
                        "Captured PID {} moved out of process group {group_id}. No further signal sent.",
                        previous.pid
                    ));
                }
            }
        }
    }

    let mut live = Vec::new();
    for item in snapshot
        .iter()
        .filter(|item| item.pgid == group_id && !item.stat.starts_with('Z'))
    {
        if !is_expected_backend(item) {
            return Err(format!(
                "Safety check refused process group {group_id}: live PID {} has an unrelated or unverified executable. No group signal sent.",
                item.pid
            ));
        }

        if let Some(previous) =
            baseline.and_then(|items| items.iter().find(|old| old.pid == item.pid))
        {
            if previous.started != item.started {
                return Err(format!(
                    "PID {} was reused in process group {group_id}. No signal sent.",
                    item.pid
                ));
            }
        }

        live.push(item.clone());
    }

    Ok(live)
}

#[cfg(unix)]
fn terminate_verified_backend_tree(
    pid: u32,
    group_id: u32,
    expected_exe: &Path,
) -> Result<(), String> {
    if group_id <= 1 || group_id == unsafe { libc::getpgrp() } as u32 {
        return Err(format!(
            "Refusing to signal unsafe backend process group {group_id}."
        ));
    }

    let initial_snapshot = process_snapshot_result().map_err(|error| {
        format!("Could not capture a checked process snapshot before Stop: {error}")
    })?;

    let selected = initial_snapshot
        .iter()
        .find(|item| item.pid == pid)
        .ok_or_else(|| {
            format!("Verified backend PID {pid} is no longer present. Refresh server status.")
        })?;

    if selected.pgid != group_id {
        return Err(format!(
            "Backend PID {pid} is in PGID {}, not expected PGID {group_id}. No signal sent.",
            selected.pgid
        ));
    }

    let initial_members =
        validate_backend_group_snapshot(&initial_snapshot, Some(pid), group_id, None, |item| {
            process_is_expected_backend(item, expected_exe)
        })?;

    if initial_members.is_empty() {
        append_stop_trace(format!(
            "PROCESS GROUP {group_id}: no live backend processes remain; zombie-only group treated as stopped."
        ));
        return Ok(());
    }

    append_stop_trace(format!(
        "VERIFIED PROCESS GROUP: selected PID={pid}; PGID={group_id}; live verified members={}.",
        initial_members.len()
    ));

    // Revalidate all members immediately before SIGTERM. A selected helper PID
    // need not equal the process-group leader.
    let before_term = process_snapshot_result()
        .map_err(|error| format!("Could not recheck process group before SIGTERM: {error}"))?;

    let before_term_members = validate_backend_group_snapshot(
        &before_term,
        Some(pid),
        group_id,
        Some(&initial_members),
        |item| process_is_expected_backend(item, expected_exe),
    )?;

    if before_term_members.is_empty() {
        return Ok(());
    }

    send_signal_checked(-(group_id as i32), SIGNAL_TERM)?;

    let term_deadline = Instant::now() + Duration::from_secs(4);
    loop {
        let current = process_snapshot_result().map_err(|error| {
            format!("Could not verify process group during SIGTERM grace period: {error}")
        })?;

        let remaining = validate_backend_group_snapshot(
            &current,
            None,
            group_id,
            Some(&initial_members),
            |item| process_is_expected_backend(item, expected_exe),
        )?;

        if remaining.is_empty() {
            append_stop_trace(format!(
                "SIGTERM RESULT: verified backend process group {group_id} has no live members."
            ));
            return Ok(());
        }

        if Instant::now() >= term_deadline {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }

    // Never escalate using a stale process list. Verify every live member again.
    let before_kill = process_snapshot_result()
        .map_err(|error| format!("Could not recheck process group before SIGKILL: {error}"))?;

    let remaining = validate_backend_group_snapshot(
        &before_kill,
        None,
        group_id,
        Some(&initial_members),
        |item| process_is_expected_backend(item, expected_exe),
    )?;

    if remaining.is_empty() {
        return Ok(());
    }

    append_stop_trace(format!(
        "SIGTERM TIMEOUT: PGID={group_id}; verified live members={}; escalating to SIGKILL.",
        remaining.len()
    ));
    send_signal_checked(-(group_id as i32), SIGNAL_KILL)?;

    let kill_deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let current = process_snapshot_result()
            .map_err(|error| format!("Could not verify process group after SIGKILL: {error}"))?;

        let remaining = validate_backend_group_snapshot(
            &current,
            None,
            group_id,
            Some(&initial_members),
            |item| process_is_expected_backend(item, expected_exe),
        )?;

        if remaining.is_empty() {
            append_stop_trace(format!(
                "SIGKILL RESULT: verified backend process group {group_id} has no live members."
            ));
            return Ok(());
        }

        if Instant::now() >= kill_deadline {
            let pids = remaining
                .iter()
                .map(|item| item.pid.to_string())
                .collect::<Vec<_>>()
                .join(", ");
            return Err(format!(
                "Could not confirm verified backend process group {group_id} stopped. Live PIDs: {pids}. Ownership was not declared released."
            ));
        }

        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod verified_backend_group_snapshot_tests {
    use super::*;

    fn item(pid: u32, ppid: u32, pgid: u32, stat: &str, started: &str) -> ProcessSnapshot {
        ProcessSnapshot {
            pid,
            ppid,
            pgid,
            stat: stat.into(),
            started: started.into(),
            command: format!("/test/rdt_voice_backend --pid {pid}"),
        }
    }

    #[test]
    fn helper_pid_can_select_and_verify_its_entire_backend_group() {
        let snapshot = vec![
            item(500, 1, 500, "S", "leader-start"),
            item(501, 1, 500, "S", "helper-start"),
            item(502, 501, 500, "S", "worker-start"),
        ];

        let members = validate_backend_group_snapshot(&snapshot, Some(501), 500, None, |_| true)
            .expect("verified helper selection should validate the entire group");

        assert_eq!(members.len(), 3);
        assert!(members.iter().any(|member| member.pid == 500));
        assert!(members.iter().any(|member| member.pid == 502));
    }

    #[test]
    fn unrelated_live_group_member_refuses_group_signal() {
        let snapshot = vec![
            item(600, 1, 600, "S", "backend-start"),
            item(601, 1, 600, "S", "unrelated-start"),
        ];

        let result = validate_backend_group_snapshot(&snapshot, Some(600), 600, None, |member| {
            member.pid == 600
        });

        assert!(result.is_err());
    }

    #[test]
    fn zombie_only_group_is_idempotent_success() {
        let snapshot = vec![
            item(700, 1, 700, "Z", "zombie-leader"),
            item(701, 700, 700, "Z", "zombie-helper"),
        ];

        let members = validate_backend_group_snapshot(&snapshot, Some(701), 700, None, |_| false)
            .expect("zombie-only groups have no live workload");

        assert!(members.is_empty());
    }

    #[test]
    fn reused_pid_is_rejected_before_escalation() {
        let baseline = vec![item(800, 1, 800, "S", "original-start")];
        let current = vec![item(800, 1, 800, "S", "reused-pid-start")];

        let result =
            validate_backend_group_snapshot(&current, None, 800, Some(&baseline), |_| true);

        assert!(result.is_err());
    }
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

    let expected_exe = backend_executable_path(&state.resource_dir);
    let first = verified_backend_process_snapshot(pid, &expected_exe)?;
    let group_id = first.pgid;

    let second = verified_backend_process_snapshot(pid, &expected_exe)?;
    if second.pgid != group_id || second.started != first.started {
        return Err(
            "The backend PID or process group changed during verification. No signal was sent; refresh and retry.".into()
        );
    }

    terminate_verified_backend_tree(pid, group_id, &expected_exe)
}

/// Stop the backend owned by this instance, then clean up only verified orphan
/// processes from this same packaged app when no other live manager owns them.
fn stop_owned_and_orphan_backends(state: &BackendState) -> Result<(), String> {
    if NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst) {
        return Err(
            "The backend cannot be stopped while a hardware benchmark request is active.".into(),
        );
    }
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
    set_private_permissions(&state.tls_config_path, 0o600)
        .map_err(|error| format!(
            "HTTPS settings were written, but their permissions could not be secured at {}: {error}",
            state.tls_config_path.display()
        ))?;
    Ok(())
}

#[cfg(windows)]
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
            if detail.is_empty() {
                ".".into()
            } else {
                format!(": {detail}")
            }
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
    let thumbprint = String::from_utf8_lossy(&output.stdout)
        .trim()
        .to_ascii_uppercase();
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
    let output = run_powershell_script(
        &script,
        "add the RDT root CA to the current user's trust store",
    )?;
    let thumbprint = String::from_utf8_lossy(&output.stdout)
        .trim()
        .to_ascii_uppercase();
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
    run_powershell_script(
        &script,
        "remove the exact RDT-managed root from the current user's trust store",
    )?;
    if windows_root_store_contains(thumbprint)? {
        return Err(
            "Windows still reports the RDT-managed root in the current-user trust store.".into(),
        );
    }
    Ok(())
}

#[cfg(windows)]
fn windows_validate_generated_tls_files(
    config: &LocalTlsConfig,
    cert: &Path,
    key: &Path,
    ca: &Path,
) -> Result<(), String> {
    for (path, expected, label) in [
        (
            cert,
            config.windows_certificate_sha256.as_deref(),
            "server certificate",
        ),
        (
            key,
            config.windows_private_key_sha256.as_deref(),
            "server private key",
        ),
        (
            ca,
            config.windows_ca_sha256.as_deref(),
            "root CA certificate",
        ),
    ] {
        let expected = expected.ok_or_else(|| format!("The generated Windows {label} has no integrity record. Run Set up trusted local HTTPS again."))?;
        let actual = sha256_file_hex(path)?;
        if !expected.eq_ignore_ascii_case(&actual) {
            return Err(format!("The generated Windows {label} changed after setup. Run Set up trusted local HTTPS again; HTTPS will not start with altered TLS material."));
        }
    }

    let mut script = powershell_read_certificate(cert)?;
    let ca_path = ca
        .to_str()
        .ok_or_else(|| "The CA path is not valid Unicode on Windows.".to_string())?;
    // The server certificate/private-key match is checked against the CNG key
    // before export in windows_generate_tls_files. Pinning the exact private-key
    // bytes here detects post-setup changes without importing a new CNG key
    // container on every health/status poll.
    script.push_str(&format!(
        "$ca = Read-RdtCertificate {}\n",
        powershell_quote(ca_path)
    ));
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
fn windows_generate_tls_files(
    cert_path: &Path,
    key_path: &Path,
    ca_path: &Path,
) -> Result<(), String> {
    let cert_path = cert_path
        .to_str()
        .ok_or_else(|| "The certificate path is not valid Unicode on Windows.".to_string())?;
    let key_path = key_path
        .to_str()
        .ok_or_else(|| "The private-key path is not valid Unicode on Windows.".to_string())?;
    let ca_path = ca_path
        .to_str()
        .ok_or_else(|| "The CA path is not valid Unicode on Windows.".to_string())?;
    let mut script = String::from(
        r#"
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
  $ca = New-SelfSignedCertificate -Type Custom -Subject 'CN=RDT Connect Local HTTPS CA' -CertStoreLocation 'Cert:\CurrentUser\My' -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(5) -KeyUsage CertSign, CRLSign -TextExtension @('2.5.29.19={critical}{text}ca=TRUE&pathlength=0')
  if ($null -eq $ca -or -not $ca.HasPrivateKey) { throw 'root_ca_generation_failed' }
  $server = New-SelfSignedCertificate -Type Custom -Subject 'CN=127.0.0.1' -Signer $ca -CertStoreLocation 'Cert:\CurrentUser\My' -Provider 'Microsoft Software Key Storage Provider' -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddDays(365) -KeyUsage DigitalSignature, KeyEncipherment -TextExtension @('2.5.29.19={critical}{text}ca=FALSE','2.5.29.37={text}1.3.6.1.5.5.7.3.1','2.5.29.17={text}DNS=localhost&IPAddress=127.0.0.1')
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
"#,
    );
    script = script
        .replace("@CERT@", &powershell_quote(cert_path))
        .replace("@KEY@", &powershell_quote(key_path))
        .replace("@CA@", &powershell_quote(ca_path));
    let output = run_powershell_script(
        &script,
        "generate a loopback-only local HTTPS certificate and private key",
    )?;
    if String::from_utf8_lossy(&output.stdout).trim() != "OK" {
        return Err(
            "Windows certificate generation did not return an explicit success result.".into(),
        );
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
        .map_err(|e| format!("RDT Connect could not run OpenSSL to {what}: {e}"))?;
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
    #[cfg(target_os = "linux")]
    {
        return linux_tls::validate_config(config);
    }
    #[cfg(not(any(target_os = "macos", windows, target_os = "linux")))]
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
    let (Some(cert_raw), Some(ca_raw)) = (
        config.certificate_path.as_deref(),
        config.ca_certificate_path.as_deref(),
    ) else {
        return false;
    };
    let cert_path = Path::new(cert_raw);
    let ca_path = Path::new(ca_raw);
    let Some(ca_path_text) = ca_path.to_str() else {
        return false;
    };
    let Ok(mut script) = powershell_read_certificate(cert_path) else {
        return false;
    };
    script.push_str(&format!(
        "$ca = Read-RdtCertificate {}\n",
        powershell_quote(ca_path_text)
    ));
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
    match run_powershell_script(&script, "verify Windows current-user TLS trust")
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
    {
        Ok(value) => value == "true",
        Err(_) => false,
    }
}

#[cfg(all(not(target_os = "macos"), not(windows)))]
fn certificate_trusted(_cert: &Path) -> bool {
    false
}

fn configured_certificate_trusted(config: &LocalTlsConfig) -> bool {
    #[cfg(windows)]
    {
        return windows_certificate_trusted(config);
    }
    #[cfg(target_os = "linux")]
    {
        return linux_tls::certificate_trusted(config);
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        config
            .certificate_path
            .as_deref()
            .map(Path::new)
            .map(certificate_trusted)
            .unwrap_or(false)
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
    let expected_ca = config
        .ca_certificate_path
        .as_deref()
        .ok_or_else(|| "The Windows HTTPS config has no root CA path.".to_string())?;
    let ca = canonical_file(
        ca_path.to_str().ok_or("Invalid CA path")?,
        "root CA certificate",
    )?;
    let configured_ca = canonical_file(expected_ca, "configured root CA certificate")?;
    if ca != configured_ca {
        return Err("The selected CA is not the root CA recorded for the generated Windows server certificate.".into());
    }
    let fingerprint = windows_certificate_thumbprint(&ca)?;
    let already_tracked = config
        .managed_trust_fingerprints
        .iter()
        .any(|item| item.eq_ignore_ascii_case(&fingerprint));
    if windows_root_store_contains(&fingerprint)? {
        if already_tracked || configured_certificate_trusted(config) {
            return Ok(());
        }
        return Err("This CA already exists in the current user's trusted-root store, but RDT Connect did not record installing it and the configured HTTPS chain does not verify. It was left unchanged.".into());
    }

    let installed_fingerprint = windows_add_root_to_current_user_store(&ca)?;
    if !already_tracked {
        config
            .managed_trust_fingerprints
            .push(installed_fingerprint.clone());
    }
    if let Err(error) = save_local_tls_config(state, config) {
        let _ = windows_remove_root_from_current_user_store(&installed_fingerprint);
        if !already_tracked {
            config
                .managed_trust_fingerprints
                .retain(|item| !item.eq_ignore_ascii_case(&installed_fingerprint));
        }
        return Err(format!("The Windows trust entry was installed but its ownership record could not be saved; the newly installed entry was rolled back. Detail: {error}"));
    }
    if !configured_certificate_trusted(config) {
        let _ = windows_remove_root_from_current_user_store(&installed_fingerprint);
        config
            .managed_trust_fingerprints
            .retain(|item| !item.eq_ignore_ascii_case(&installed_fingerprint));
        save_local_tls_config(state, config)?;
        return Err("The root was added to the current user's trust store, but Windows did not verify the full certificate chain for 127.0.0.1. The new trust entry was removed; no HTTP fallback was attempted.".into());
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn trust_ca_file(
    state: &BackendState,
    config: &mut LocalTlsConfig,
    ca: &Path,
) -> Result<(), String> {
    let configured = config
        .ca_certificate_path
        .as_deref()
        .ok_or_else(|| "The Linux HTTPS config has no root CA path.".to_string())?;
    if canonical_file(configured, "configured root CA certificate")?
        != canonical_file(&ca.display().to_string(), "root CA certificate")?
    {
        return Err("Linux trusts only the RDT-generated root CA recorded in the HTTPS settings; a different CA file was refused.".into());
    }
    linux_tls::trust_ca(state, config)
}

#[cfg(not(any(target_os = "macos", windows, target_os = "linux")))]
fn trust_ca_file(
    _state: &BackendState,
    _config: &mut LocalTlsConfig,
    _ca: &Path,
) -> Result<(), String> {
    Err("Native local certificate trust is not implemented on this platform.".into())
}

#[cfg(target_os = "linux")]
fn linux_untrusted_message(config: &LocalTlsConfig) -> String {
    linux_tls::untrusted_message(config)
}
#[cfg(not(target_os = "linux"))]
fn linux_untrusted_message(_config: &LocalTlsConfig) -> String {
    String::new()
}
#[cfg(target_os = "linux")]
fn linux_trusted_message(config: &LocalTlsConfig) -> String {
    linux_tls::trusted_message(config)
}
#[cfg(not(target_os = "linux"))]
fn linux_trusted_message(_config: &LocalTlsConfig) -> String {
    String::new()
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
        if cfg!(target_os = "linux") {
            linux_untrusted_message(config)
        } else if cfg!(target_os = "windows") {
            "The certificate is configured, but Windows has not verified it against the current user's trusted-root store. Trust the generated RDT root CA, then recheck.".to_string()
        } else {
            {
                "The certificate and key match, but the operating system does not currently trust this certificate for 127.0.0.1. Trust the selected root CA, then recheck.".to_string()
            }
        }
    } else {
        if cfg!(target_os = "linux") {
            linux_trusted_message(config)
        } else if cfg!(target_os = "windows") {
            "HTTPS is configured: the certificate is valid for 127.0.0.1 and Windows current-user trust verification passed.".to_string()
        } else {
            if cfg!(target_os = "macos") {
                "HTTPS is configured: the certificate is valid for 127.0.0.1 and macOS trust verification passed.".to_string()
            } else {
                "HTTPS is configured and operating-system certificate trust verification passed."
                    .to_string()
            }
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
    local_tls_client_with_optional_timeout(config, Some(timeout))
}

fn local_tls_client_with_optional_timeout(
    config: &LocalTlsConfig,
    timeout: Option<Duration>,
) -> Result<Client, String> {
    let mut builder = Client::builder();
    if let Some(timeout) = timeout {
        builder = builder.timeout(timeout);
    }
    if config.mode.eq_ignore_ascii_case("https") {
        // Preserve the existing local CA trust behavior.
        #[cfg(not(target_os = "linux"))]
        {
            if let Some(trust_path) = config.ca_certificate_path.as_deref() {
                let pem = fs::read(trust_path).map_err(|e| {
                    format!("Could not read the configured root CA certificate: {e}")
                })?;
                let root = reqwest::Certificate::from_pem(&pem).map_err(|e| {
                    format!("Could not load the configured root CA certificate: {e}")
                })?;
                builder = builder.add_root_certificate(root);
            }
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
async fn get_local_https_status(app: AppHandle) -> Result<LocalHttpsStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<BackendState>();
        let config = load_local_tls_config(&state)?;
        Ok(local_https_status_from_config(&config))
    })
    .await
    .map_err(|error| format!("Local HTTPS status worker failed: {error}"))?
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
        if status.configured && status.trusted && status.mode.eq_ignore_ascii_case("https") {
            return Ok(status);
        }
        return Err(format!(
            "Windows no longer confirms the existing trusted HTTPS configuration: {}",
            status.message
        ));
    }
    // If file integrity and certificate checks still pass, retry only the
    // current-user trust operation instead of issuing a new CA on each click.
    if existing_status.configured && existing_config.generated {
        if let Some(ca_path) = existing_config.ca_certificate_path.clone() {
            let mut config = existing_config;
            config.mode = "https".into();
            trust_ca_file(state, &mut config, Path::new(&ca_path))?;
            let status = local_https_status_from_config(&config);
            if status.configured && status.trusted {
                return Ok(status);
            }
            return Err(format!(
                "Windows certificate trust could not be confirmed after retry: {}",
                status.message
            ));
        }
    }

    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let dir = state.https_dir.join(format!(
        "generated-windows-{seconds}-{}",
        std::process::id()
    ));
    fs::create_dir_all(&dir).map_err(|error| {
        format!("Could not create the Windows HTTPS certificate folder: {error}")
    })?;
    set_private_permissions(&dir, 0o700).map_err(|error| {
        format!("Could not restrict access to the Windows HTTPS certificate folder: {error}")
    })?;
    let cert = dir.join("rdt-local-server.pem");
    let key = dir.join("rdt-local-server-key.pem");
    let ca = dir.join("rdt-local-ca.pem");
    windows_generate_tls_files(&cert, &key, &ca)?;
    for path in [&cert, &key, &ca] {
        if !path.is_file() {
            return Err(
                "Windows certificate generation did not create every required TLS file.".into(),
            );
        }
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

#[cfg(target_os = "linux")]
fn setup_local_https_certificate_linux(state: &BackendState) -> Result<LocalHttpsStatus, String> {
    if state.status().running {
        return Err("Stop the server before changing TLS certificates.".into());
    }
    let config = linux_tls::setup(state)?;
    let status = local_https_status_from_config(&config);
    if !status.configured || !status.trusted || !status.mode.eq_ignore_ascii_case("https") {
        return Err(format!("Linux local HTTPS setup did not pass its final verification: {}. No HTTP fallback was attempted.", status.message));
    }
    Ok(status)
}

// Certificate generation and administrator prompts can take a long time, so the Tauri
// commands run the blocking work on a worker thread instead of the UI thread.
#[tauri::command]
async fn setup_local_https_certificate(app: AppHandle) -> Result<LocalHttpsStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = LOCAL_TLS_OPERATION
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let state = app.state::<BackendState>();
        setup_local_https_certificate_blocking(&state)
    })
    .await
    .map_err(|error| format!("Local HTTPS setup worker failed: {error}"))?
}

fn setup_local_https_certificate_blocking(
    state: &BackendState,
) -> Result<LocalHttpsStatus, String> {
    #[cfg(windows)]
    {
        return setup_local_https_certificate_windows(&state);
    }
    #[cfg(target_os = "linux")]
    {
        return setup_local_https_certificate_linux(state);
    }
    #[cfg(not(any(target_os = "macos", windows, target_os = "linux")))]
    {
        return Err("Trusted local HTTPS is not implemented on this platform. Use loopback-only HTTP for development; remote access must remain behind verified Tailscale HTTPS/pairing.".into());
    }
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
            .map_err(|e| format!("Could not run OpenSSL to {what}: {e}"))?;
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
    set_private_permissions(&ca_key, 0o600)
        .map_err(|e| format!("Could not secure the temporary CA signing key: {e}"))?;
    set_private_permissions(&server_key, 0o600)
        .map_err(|e| format!("Could not secure the generated server private key: {e}"))?;

    // The CA key has completed its only task: signing the loopback server
    // certificate. Retain the public CA certificate and server key only.
    fs::remove_file(&ca_key)
        .map_err(|e| format!("Could not remove the temporary local CA signing key: {e}"))?;
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
    if cfg!(target_os = "linux") {
        return Err("Linux supports RDT-generated trusted local HTTPS only. Select Set up trusted local HTTPS; linking an external certificate is intentionally disabled because its private key, chain and trust cannot be verified safely.".into());
    }
    if cfg!(not(any(target_os = "macos", windows, target_os = "linux"))) {
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
async fn trust_local_https_ca(
    app: AppHandle,
    ca_certificate_path: Option<String>,
) -> Result<LocalHttpsStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = LOCAL_TLS_OPERATION
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let state = app.state::<BackendState>();
        trust_local_https_ca_blocking(&state, ca_certificate_path)
    })
    .await
    .map_err(|error| format!("Local HTTPS trust worker failed: {error}"))?
}

fn trust_local_https_ca_blocking(
    state: &BackendState,
    ca_certificate_path: Option<String>,
) -> Result<LocalHttpsStatus, String> {
    if cfg!(not(any(target_os = "macos", windows, target_os = "linux"))) {
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
async fn remove_rdt_https_trust(app: AppHandle) -> Result<LocalHttpsStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = LOCAL_TLS_OPERATION
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        let state = app.state::<BackendState>();
        remove_rdt_https_trust_blocking(&state)
    })
    .await
    .map_err(|error| format!("Local HTTPS trust-removal worker failed: {error}"))?
}

fn remove_rdt_https_trust_blocking(state: &BackendState) -> Result<LocalHttpsStatus, String> {
    if cfg!(not(any(target_os = "macos", windows, target_os = "linux"))) {
        return Err("Removing RDT-managed CA trust is not implemented on this platform. No system trust store was modified.".into());
    }
    if state.status().running {
        return Err("Stop the server before changing TLS trust.".into());
    }
    #[cfg(target_os = "linux")]
    {
        let mut config = load_local_tls_config(state)?;
        linux_tls::remove_trust(state, &mut config)?;
        return Ok(local_https_status_from_config(&config));
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
                Ok(()) => {}
                Err(error) => {
                    errors.push(format!("fingerprint {fingerprint}: {error}"));
                    remaining.push(fingerprint);
                }
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
    if normalized == "https" && cfg!(not(any(target_os = "macos", windows, target_os = "linux"))) {
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

const RDT_MODEL_LOCK_JSON: &str = include_str!("../../backend/rdt_model_lock.json");

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
            PlatformModelOption {
                id: "tiny".into(),
                name: "Tiny".into(),
                description: "Fastest, smallest GGML model".into(),
                size: "~43.5 MB".into(),
                recommended: false,
            },
            PlatformModelOption {
                id: "base".into(),
                name: "Base".into(),
                description: "Lightweight GGML choice".into(),
                size: "~81.8 MB".into(),
                recommended: false,
            },
            PlatformModelOption {
                id: "small".into(),
                name: "Small".into(),
                description: "Balanced GGML model".into(),
                size: "~264 MB".into(),
                recommended: false,
            },
            PlatformModelOption {
                id: "large-v3-turbo".into(),
                name: "Large v3 Turbo".into(),
                description: "Strong balance of speed and quality".into(),
                size: "~574 MB".into(),
                recommended: true,
            },
            PlatformModelOption {
                id: "large-v3".into(),
                name: "Large v3".into(),
                description: "Largest GGML model in this list".into(),
                size: "~1.08 GB".into(),
                recommended: false,
            },
        ];
    }
    vec![
        PlatformModelOption {
            id: "mlx-community/whisper-tiny-mlx".into(),
            name: "Tiny".into(),
            description: "Fastest · smallest download".into(),
            size: "~74 MB".into(),
            recommended: false,
        },
        PlatformModelOption {
            id: "mlx-community/whisper-base-mlx".into(),
            name: "Base".into(),
            description: "Lightweight choice".into(),
            size: "~144 MB".into(),
            recommended: false,
        },
        PlatformModelOption {
            id: "mlx-community/whisper-small-mlx".into(),
            name: "Small".into(),
            description: "Middle-size model".into(),
            size: "~481 MB".into(),
            recommended: false,
        },
        PlatformModelOption {
            id: "mlx-community/whisper-large-v3-turbo".into(),
            name: "Large v3 Turbo".into(),
            description: "Strong balance of speed and quality".into(),
            size: "~1.61 GB".into(),
            recommended: true,
        },
        PlatformModelOption {
            id: "mlx-community/whisper-large-v3-mlx".into(),
            name: "Large v3".into(),
            description: "Largest download in this list".into(),
            size: "~3.08 GB".into(),
            recommended: false,
        },
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

    if let Some((exception_name, _detail)) = trimmed.split_once(": ") {
        let simple_name = exception_name.rsplit('.').next().unwrap_or(exception_name);
        let safe_name = !simple_name.is_empty()
            && simple_name.len() <= 64
            && simple_name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_');
        if safe_name
            && (simple_name.ends_with("Error")
                || simple_name.ends_with("Exception")
                || simple_name.ends_with("Timeout"))
        {
            let family = if cfg!(any(target_os = "windows", target_os = "linux")) {
                "GGML Whisper"
            } else {
                "MLX Whisper"
            };
            let message = format!(
                "{family} model download failed: Python exception {simple_name} during preflight. The model is not marked Downloaded; retry the download."
            );
            publish_model_download_status(app, store, "failed", &message, true);
        }
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

fn model_download_diagnostics_directory(state: &BackendState) -> Result<PathBuf, String> {
    let support = state.app_language_pref_path.parent().ok_or_else(|| {
        "Could not locate RDT Connect app support for model diagnostics.".to_string()
    })?;

    let support_meta = fs::symlink_metadata(support)
        .map_err(|error| format!("Could not inspect app support for model diagnostics: {error}"))?;
    if support_meta.file_type().is_symlink() || !support_meta.is_dir() {
        return Err(
            "RDT Connect app support is not a real directory; model diagnostics are disabled."
                .into(),
        );
    }

    let directory = support.join("model-download-diagnostics");
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create private model-download diagnostics: {error}"))?;
    set_private_permissions(&directory, 0o700)
        .map_err(|error| format!("Could not secure model-download diagnostics: {error}"))?;

    let metadata = fs::symlink_metadata(&directory)
        .map_err(|error| format!("Could not inspect model-download diagnostics: {error}"))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err("Model-download diagnostics path is not a real directory.".into());
    }

    let support_canonical = fs::canonicalize(support)
        .map_err(|error| format!("Could not resolve app support: {error}"))?;
    let directory_canonical = fs::canonicalize(&directory)
        .map_err(|error| format!("Could not resolve model diagnostics directory: {error}"))?;
    if directory_canonical.parent() != Some(support_canonical.as_path()) {
        return Err("Model-download diagnostics directory escaped app support.".into());
    }
    Ok(directory_canonical)
}

fn model_download_log_path_is_safe(directory: &Path, path: &Path) -> bool {
    if path.parent() != Some(directory) {
        return false;
    }
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    if !name.starts_with("rdt-connect-model-download-") || !name.ends_with(".log") {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return false;
    }
    fs::canonicalize(path)
        .ok()
        .and_then(|canonical| canonical.parent().map(Path::to_path_buf))
        .as_deref()
        == Some(directory)
}

fn prune_model_download_logs(directory: &Path, preserve: &Path) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    let mut files: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| model_download_log_path_is_safe(directory, path))
        .collect();
    files.sort_by_key(|path| path.file_name().map(|name| name.to_os_string()));

    while files.len() > 5 {
        let Some(index) = files.iter().position(|path| path != preserve) else {
            break;
        };
        let stale = files.remove(index);
        let _ = fs::remove_file(stale);
    }
}

fn create_model_download_log(directory: &Path) -> Result<(PathBuf, File), String> {
    let metadata = fs::symlink_metadata(directory)
        .map_err(|error| format!("Could not inspect private model log directory: {error}"))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err("Private model log directory is not a real directory.".into());
    }

    let stamp = unix_epoch_millis();
    for attempt in 0..16u8 {
        let path = directory.join(format!(
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
            Ok(file) => {
                prune_model_download_logs(directory, &path);
                return Ok((path, file));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "Could not create a private model-download log: {error}"
                ));
            }
        }
    }
    Err("Could not allocate a unique model-download diagnostics log.".into())
}

fn retain_model_download_log_path(store: &Arc<Mutex<Option<PathBuf>>>, expected_path: &Path) {
    match store.lock() {
        Ok(mut current) => {
            if current.as_deref() == Some(expected_path) {
                *current = None;
            }
        }
        Err(_) => {
            eprintln!("RDT Connect retained model-download log; log-path mutex is poisoned.");
        }
    }
}

fn append_model_download_process_snapshot(
    log_path: &Path,
    event: &str,
    identity: &ProcessSnapshot,
    expected_exe: &Path,
) {
    let Some(directory) = log_path.parent() else {
        return;
    };
    if !model_download_log_path_is_safe(directory, log_path) {
        return;
    }

    let Ok(snapshot) = process_snapshot_result() else {
        if let Ok(mut file) = OpenOptions::new().append(true).open(log_path) {
            let _ = writeln!(
                file,
                "RDT_CONNECT_PROCESS_SNAPSHOT:event={event};snapshot_ok=false"
            );
        }
        return;
    };

    let mut file = match OpenOptions::new().append(true).open(log_path) {
        Ok(file) => file,
        Err(_) => return,
    };
    let leader = snapshot.iter().find(|item| item.pid == identity.pid);
    let leader_state = match leader {
        None => "leader_absent",
        Some(item) if item.pgid != identity.pgid => "leader_pid_outside_original_group",
        Some(item) if item.started != identity.started => "leader_start_time_changed_inside_group",
        Some(item) if item.command != identity.command => "leader_command_changed_inside_group",
        Some(_) => "leader_identity_matches",
    };
    let group_count = snapshot
        .iter()
        .filter(|item| item.pgid == identity.pgid && !item.stat.starts_with('Z'))
        .count();
    let _ = writeln!(
        file,
        "RDT_CONNECT_PROCESS_SNAPSHOT:event={event};snapshot_ok=true;leader_state={leader_state};live_group_members={group_count}"
    );

    for item in snapshot
        .iter()
        .filter(|item| item.pid == identity.pid || item.pgid == identity.pgid)
        .take(12)
    {
        let command_hash = format!("{:x}", Sha256::digest(item.command.as_bytes()));
        let expected = command_is_expected_backend(&item.command, expected_exe);
        let _ = writeln!(
            file,
            "RDT_CONNECT_PROCESS_SNAPSHOT:event={event};pid={};ppid={};pgid={};stat={};started={};command_sha256={};expected_backend={expected}",
            item.pid, item.ppid, item.pgid, item.stat, item.started, command_hash
        );
    }
}

fn model_download_diagnostic_path_label(path: &Path) -> String {
    if let Ok(home) = home_dir() {
        if let Ok(relative) = path.strip_prefix(home.as_path()) {
            return format!("~/{}", relative.display());
        }
    }
    path.display().to_string()
}

fn model_download_diagnostics_report(state: &BackendState) -> Vec<String> {
    let mut lines = vec![
        "MODEL DOWNLOAD DIAGNOSTICS (read-only; no download or inference started):".to_string(),
    ];

    let status = match state.model_download_status.lock() {
        Ok(status) => status.clone(),
        Err(_) => {
            lines.push("Attempt status: unavailable (status mutex poisoned).".into());
            ModelDownloadStatus::default()
        }
    };
    let identity = match state.model_download_pid.lock() {
        Ok(identity) => identity.clone(),
        Err(_) => {
            lines.push("Captured process identity: unavailable (identity mutex poisoned).".into());
            None
        }
    };

    lines.push(format!(
        "Attempt state: active={}; phase={}; progress={}%; measured_transfer_bytes={}; total_bytes={}; total_is_estimate={}",
        status.active,
        status.phase,
        status.progress_percent,
        status.downloaded_bytes,
        status.total_bytes.map(|value| value.to_string()).unwrap_or_else(|| "unknown".into()),
        status.size_is_estimate
    ));

    if let Some(model) = status.model.as_deref() {
        lines.push(format!("Model/repository: {model}"));
        lines.push(format!(
            "Locked revision: {}",
            model_lock_revision(model).unwrap_or_else(|| "not found in bundled lock".into())
        ));
        lines.push(format!(
            "Locked weight SHA-256: {}",
            model_lock_sha256(model).unwrap_or_else(|| "not found in bundled lock".into())
        ));
    } else {
        lines.push("Model/repository: no recorded attempt.".into());
    }

    if let Some(root) = status.destination_root.as_deref() {
        let root = PathBuf::from(root);
        lines.push(format!(
            "Selected model storage root: {}",
            model_download_diagnostic_path_label(&root)
        ));
        lines.push(format!(
            "Effective HF_HOME: {}",
            model_download_diagnostic_path_label(&root)
        ));
        lines.push(format!(
            "Effective HF_HUB_CACHE: {}",
            model_download_diagnostic_path_label(&root.join("hub"))
        ));
        lines.push(format!(
            "Effective HF_XET_CACHE: {}",
            model_download_diagnostic_path_label(&root.join("xet"))
        ));
    } else {
        lines.push("Selected model storage root: not recorded.".into());
    }

    lines.push(format!("Last status message: {}", status.message));

    if let Some(identity) = identity.as_ref() {
        let command_hash = format!("{:x}", Sha256::digest(identity.command.as_bytes()));
        lines.push(format!(
            "Captured downloader identity: PID={} PGID={} start={} state={} command_sha256={}",
            identity.pid, identity.pgid, identity.started, identity.stat, command_hash
        ));

        match process_snapshot_result() {
            Ok(snapshot) => {
                let group_members = snapshot.iter()
                    .filter(|item| item.pgid == identity.pgid && !item.stat.starts_with('Z'))
                    .count();
                lines.push(format!(
                    "Current process snapshot: parsed successfully; {} live members in original PGID {}.",
                    group_members, identity.pgid
                ));
                match snapshot.iter().find(|item| item.pid == identity.pid) {
                    None => lines.push("Original numeric PID: absent from current process table.".into()),
                    Some(current) if current.pgid != identity.pgid => lines.push(
                        "Original numeric PID: now outside the original PGID; this PID is not targeted.".into()
                    ),
                    Some(current) => {
                        let started_same = current.started == identity.started;
                        let command_same = current.command == identity.command;
                        lines.push(format!(
                            "Original PID remains in original PGID: start_time_matches={started_same}; command_matches={command_same}; expected_backend={}",
                            command_is_expected_backend(&current.command, &backend_executable_path(&state.resource_dir))
                        ));
                        if !started_same || !command_same {
                            lines.push(
                                "Ownership warning: leader identity differs inside the original PGID; no relaxed signal decision is made.".into()
                            );
                        }
                    }
                }
            }
            Err(_) => lines.push(
                "Current process snapshot: FAILED; no process absence or ownership conclusion is inferred.".into()
            ),
        }
    } else {
        lines.push("Captured downloader identity: none currently registered.".into());
    }

    let directory = match model_download_diagnostics_directory(state) {
        Ok(directory) => directory,
        Err(_) => {
            lines.push("Private model-download log directory: unavailable or unsafe.".into());
            return lines;
        }
    };

    let active_path = state
        .model_download_log_path
        .lock()
        .ok()
        .and_then(|path| path.clone())
        .filter(|path| model_download_log_path_is_safe(&directory, path));

    let log_path = active_path.or_else(|| {
        let mut candidates: Vec<PathBuf> = fs::read_dir(&directory)
            .ok()?
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| model_download_log_path_is_safe(&directory, path))
            .collect();
        candidates.sort_by_key(|path| path.file_name().map(|name| name.to_os_string()));
        candidates.pop()
    });

    let Some(log_path) = log_path else {
        lines.push("Latest private attempt log: none retained.".into());
        return lines;
    };

    let metadata = match fs::symlink_metadata(&log_path) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => metadata,
        _ => {
            lines.push("Latest private attempt log: unsafe or unavailable; not opened.".into());
            return lines;
        }
    };

    lines.push(format!(
        "Latest private attempt log: model-download-diagnostics/{} ({} bytes; private file).",
        log_path.file_name().unwrap_or_default().to_string_lossy(),
        metadata.len()
    ));

    const MAX_TAIL_BYTES: u64 = 65_536;
    let mut file = match File::open(&log_path) {
        Ok(file) => file,
        Err(_) => {
            lines.push("Log excerpt: unavailable.".into());
            return lines;
        }
    };
    let start = metadata.len().saturating_sub(MAX_TAIL_BYTES);
    if start > 0 {
        let _ = std::io::Seek::seek(&mut file, std::io::SeekFrom::Start(start));
    }
    let mut bytes = Vec::new();
    let _ = file.take(MAX_TAIL_BYTES).read_to_end(&mut bytes);
    let excerpt = String::from_utf8_lossy(&bytes);
    let safe_lines: Vec<&str> = excerpt
        .lines()
        .filter(|line| {
            line.starts_with("RDT_MODEL_DOWNLOAD_STAGE:")
                || line.starts_with("RDT_MODEL_DOWNLOAD_META:")
                || line.starts_with("RDT_MODEL_DOWNLOAD_PROGRESS:")
                || line.starts_with("RDT_MODEL_DOWNLOAD_ERROR:")
                || line.starts_with("RDT_CONNECT_PROCESS_SNAPSHOT:")
        })
        .collect();

    lines.push("Redacted structured log tail (raw stderr/tracebacks/credentials omitted):".into());
    if safe_lines.is_empty() {
        lines.push("(No structured downloader events retained in the log tail.)".into());
    } else {
        for line in safe_lines.iter().rev().take(18).rev() {
            lines.push((*line).to_string());
        }
    }
    lines.push(
        "Recommended action: inspect the structured phase and process snapshot evidence before retrying; do not signal an unverified PID or PGID.".into()
    );
    lines
}

fn clear_model_download_log_path_with<Remove>(
    store: &Mutex<Option<PathBuf>>,
    expected_path: &Path,
    remove_file: Remove,
) -> Result<(), String>
where
    Remove: FnOnce(&Path) -> std::io::Result<()>,
{
    // Log-path bookkeeping is independent of process state. A poisoned mutex
    // must not prevent unlinking this attempt's known private log.
    let state_result = match store.lock() {
        Ok(mut current) => {
            if current.as_deref() == Some(expected_path) {
                *current = None;
            }
            Ok(())
        }
        Err(_) => Err("The model-download log-path mutex is poisoned.".to_string()),
    };

    // Always attempt unlink, even when bookkeeping-lock acquisition failed.
    let unlink_result = match remove_file(expected_path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "Could not remove the private model-download progress log: {error}"
        )),
    };

    match (state_result, unlink_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(state_error), Ok(())) => Err(state_error),
        (Ok(()), Err(unlink_error)) => Err(unlink_error),
        (Err(state_error), Err(unlink_error)) => Err(format!("{state_error} {unlink_error}")),
    }
}

fn clear_model_download_log_path(store: &Arc<Mutex<Option<PathBuf>>>, expected_path: &Path) {
    if let Err(error) =
        clear_model_download_log_path_with(store.as_ref(), expected_path, |path: &Path| {
            fs::remove_file(path)
        })
    {
        // Log cleanup diagnostics never change downloader process state.
        eprintln!("RDT Connect model-download log cleanup: {error}");
    }
}

fn capture_model_download_identity(
    pid: u32,
    expected_exe: &Path,
) -> Result<ProcessSnapshot, String> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let snapshot = process_snapshot_result()?;
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
async fn start_model_download(
    app: AppHandle,
    model: String,
) -> Result<ModelDownloadStatus, String> {
    tauri::async_runtime::spawn_blocking(move || start_model_download_blocking(app, model))
        .await
        .map_err(|error| format!("Model-download worker failed: {error}"))?
}

fn inspect_model_download_status<T, R>(
    store: &Mutex<T>,
    inspect: impl FnOnce(&T) -> R,
) -> Result<R, String> {
    let status = store
        .lock()
        .map_err(|_| "The model-download status mutex is poisoned.".to_string())?;
    Ok(inspect(&status))
}

fn lock_model_download_registration_stores<'a>(
    pid_store: &'a std::sync::Mutex<Option<ProcessSnapshot>>,
    log_path_store: &'a std::sync::Mutex<Option<PathBuf>>,
) -> Result<
    (
        std::sync::MutexGuard<'a, Option<ProcessSnapshot>>,
        std::sync::MutexGuard<'a, Option<PathBuf>>,
    ),
    String,
> {
    let pid_guard = pid_store
        .lock()
        .map_err(|_| "Model-download PID store is unavailable.".to_string())?;
    let log_guard = log_path_store
        .lock()
        .map_err(|_| "Model-download log-path store is unavailable.".to_string())?;
    Ok((pid_guard, log_guard))
}

fn start_model_download_blocking(
    app: AppHandle,
    model: String,
) -> Result<ModelDownloadStatus, String> {
    let state = app.state::<BackendState>();
    validate_model(&model)?;
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let _finalization = state
        .model_download_finalization
        .lock()
        .map_err(|_| "Model-download finalization lock is unavailable.".to_string())?;
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
                "A verified local GGML Whisper model is already available; no download is needed."
                    .to_string()
            } else {
                "A verified local MLX model copy is already available; no download is needed."
                    .to_string()
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
    let log_directory = model_download_diagnostics_directory(state.inner())?;
    let (log_path, log_writer) = create_model_download_log(&log_directory)?;
    let stderr_writer = match log_writer.try_clone() {
        Ok(writer) => writer,
        Err(error) => {
            let _ = fs::remove_file(&log_path);
            return Err(format!(
                "Could not attach downloader stderr to its private log: {error}"
            ));
        }
    };
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
        .stderr(Stdio::from(stderr_writer));
    #[cfg(any(windows, target_os = "linux"))]
    command.env("RDT_GGML_MODEL_DIR", root.join("ggml"));
    #[cfg(unix)]
    command.process_group(0);
    // Acquire both ownership stores before creating the child. A poisoned
    // store must fail before spawn, never leave an untracked downloader.
    let (mut download_pid, mut stored_log) = match lock_model_download_registration_stores(
        state.model_download_pid.as_ref(),
        state.model_download_log_path.as_ref(),
    ) {
        Ok(guards) => guards,
        Err(error) => {
            drop(command);
            let _ = fs::remove_file(&log_path);
            publish_model_download_status(
                &app,
                &state.model_download_status,
                "failed",
                &format!("Could not safely prepare downloader ownership: {error}"),
                false,
            );
            return Err(error);
        }
    };

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
            let deadline = Instant::now() + Duration::from_secs(10);
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
    *download_pid = Some(identity.clone());
    *stored_log = Some(log_path.clone());
    drop(stored_log);
    drop(download_pid);
    append_model_download_process_snapshot(&log_path, "identity-captured", &identity, &exe);

    let app_thread = app.clone();
    let status_store = Arc::clone(&state.model_download_status);
    let pid_store = Arc::clone(&state.model_download_pid);
    let log_path_store = Arc::clone(&state.model_download_log_path);
    let cancel_requested = Arc::clone(&state.model_download_cancel_requested);
    let finalization_gate = Arc::clone(&state.model_download_finalization);
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
                    publish_model_download_failure_for_identity_with_gate(
                        &finalization_gate,
                        &pid_store,
                        &status_store,
                        &app_thread,
                        &identity,
                        &message,
                    );
                    retain_model_download_log_path(&log_path_store, &log_path);
                    return;
                }
                let finalized = finalize_model_download_state_with(
                    &finalization_gate,
                    &pid_store,
                    &status_store,
                    &cancel_requested,
                    &identity,
                    false,
                    |_, _| {
                        (
                        "failed".into(),
                        "The model downloader's progress log could not be opened. Retry the download.".into(),
                    )
                    },
                    |snapshot| {
                        let _ = app_thread.emit("rdt-model-download-status", snapshot);
                    },
                );
                if let Err(finalize_error) = finalized {
                    let message = format!(
                        "The downloader was stopped, but its terminal state could not be committed: {finalize_error}. The download remains locked; retry Cancel."
                    );
                    publish_model_download_failure_for_identity_with_gate(
                        &finalization_gate,
                        &pid_store,
                        &status_store,
                        &app_thread,
                        &identity,
                        &message,
                    );
                }
                retain_model_download_log_path(&log_path_store, &log_path);
                return;
            }
        };
        let mut pending_line = Vec::with_capacity(512);
        let mut complete_seen_at: Option<Instant> = None;
        let mut exit_status = None;
        let mut supervisor_error: Option<String> = None;
        let mut recovered_after_complete = false;
        let mut metadata_check_timed_out = false;
        let checking_started_at = Instant::now();

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

            // Metadata preflight is a distinct phase from the large weight transfer.
            // Bound its total duration, then stop only this captured, isolated group.
            // Never unlock the UI unless both group cleanup and leader exit are confirmed.
            let checking_still_active = match inspect_model_download_status(
                status_store.as_ref(),
                |status| status.active && status.phase == "checking",
            ) {
                Ok(active) => active,
                Err(error) => {
                    eprintln!(
                        "RDT Connect could not inspect model-download status; keeping the watchdog armed: {error}"
                    );
                    true
                }
            };
            if checking_still_active
                && !cancel_requested.load(Ordering::SeqCst)
                && checking_started_at.elapsed() >= Duration::from_secs(90)
            {
                append_model_download_process_snapshot(
                    &log_path,
                    "metadata-watchdog-before-stop",
                    &identity,
                    &expected_exe,
                );
                match finish_stopping_model_download_group(&identity, &expected_exe) {
                    Ok(()) => {
                        let exit_deadline = Instant::now() + Duration::from_secs(2);
                        loop {
                            match child.try_wait() {
                                Ok(Some(status)) => {
                                    exit_status = Some(status);
                                    metadata_check_timed_out = true;
                                    break;
                                }
                                Ok(None) if Instant::now() < exit_deadline => {
                                    thread::sleep(Duration::from_millis(50));
                                }
                                Ok(None) => {
                                    supervisor_error = Some(
                                        "The model metadata check exceeded 90 seconds, but downloader exit could not be confirmed. The download remains locked; select Try cancel again.".into()
                                    );
                                    break;
                                }
                                Err(error) => {
                                    supervisor_error = Some(format!(
                                        "The model metadata check exceeded 90 seconds, but downloader exit could not be confirmed ({error}). The download remains locked; select Try cancel again."
                                    ));
                                    break;
                                }
                            }
                        }
                    }
                    Err(error) => {
                        supervisor_error = Some(format!(
                            "The model metadata check exceeded 90 seconds, but safe cleanup of its verified process group could not be confirmed ({error}). The download remains locked; select Try cancel again."
                        ));
                    }
                }
                break;
            }

            // COMPLETE is emitted only after weight SHA-256 validation and the verified
            // cache marker are durable. Give normal interpreter teardown 1.5s; if still
            // alive, independently verify the pinned cache and terminate only this exact
            // isolated process group. No unbounded pipe EOF wait or arbitrary 8s stall.
            if !cancel_requested.load(Ordering::SeqCst)
                && complete_seen_at.is_some_and(|at| at.elapsed() >= Duration::from_millis(1500))
            {
                let cache_valid = model_cache_has_weights(&root_for_worker, &model_for_worker);
                let cache_valid =
                    cache_valid && model_cache_fully_verified(&root_for_worker, &model_for_worker);
                match finish_stopping_model_download_group(&identity, &expected_exe) {
                    Ok(()) => {
                        let exit_deadline = Instant::now() + Duration::from_secs(10);
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
            publish_model_download_failure_for_identity_with_gate(
                &finalization_gate,
                &pid_store,
                &status_store,
                &app_thread,
                &identity,
                &error,
            );
            retain_model_download_log_path(&log_path_store, &log_path);
            return;
        }

        // The downloader's parent can exit while a multiprocessing resource tracker
        // still owns the process-group membership. Close out that exact group before
        // releasing the UI or allowing a second model download.
        if let Err(error) = finish_stopping_model_download_group(&identity, &expected_exe) {
            let message = format!("The downloader exited, but cleanup of its verified process group is not confirmed: {error}. Select Try cancel again.");
            publish_model_download_failure_for_identity_with_gate(
                &finalization_gate,
                &pid_store,
                &status_store,
                &app_thread,
                &identity,
                &message,
            );
            retain_model_download_log_path(&log_path_store, &log_path);
            return;
        }

        let cache_valid = !metadata_check_timed_out
            && model_cache_has_weights(&root_for_worker, &model_for_worker);
        let cache_valid =
            cache_valid && model_cache_fully_verified(&root_for_worker, &model_for_worker);

        let terminal_result = finalize_model_download_state_with(
            &finalization_gate,
            &pid_store,
            &status_store,
            &cancel_requested,
            &identity,
            false,
            |canceled, current_status| {
                if cache_valid {
                    let family = if cfg!(any(target_os = "windows", target_os = "linux")) {
                        "GGML Whisper"
                    } else {
                        "MLX Whisper"
                    };
                    let message = if recovered_after_complete {
                        format!("The {family} model files were downloaded and verified. RDT Connect recovered the downloader's final shutdown. Model loading and inference have not yet been checked.")
                    } else if canceled {
                        format!("The {family} model files finished downloading and passed integrity verification before cancellation completed. Model loading and inference have not yet been checked.")
                    } else if exit_status.is_some_and(|status| status.success()) {
                        format!("Download complete. The verified {family} model files are saved; model loading and inference will be checked when you start the server.")
                    } else {
                        format!("The downloader ended unexpectedly, but the {family} model files are complete and verified. Model loading and inference have not yet been checked.")
                    };
                    ("complete".into(), message)
                } else if canceled {
                    (
                        "cancelled".into(),
                        "Download canceled. Only complete, verified model files will be shown as Downloaded; valid completed files remain reusable for retry.".into(),
                    )
                } else {
                    let family = if cfg!(any(target_os = "windows", target_os = "linux")) {
                        "GGML Whisper"
                    } else {
                        "MLX Whisper"
                    };
                    let previous_message = current_status.message.clone();

                    let message = if metadata_check_timed_out {
                        "The model metadata check exceeded 90 seconds. The downloader was stopped safely; no model is marked Downloaded. Retry the check.".to_string()
                    } else if previous_message.contains(" model download failed:") {
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
                    ("failed".into(), message)
                }
            },
            |snapshot| {
                let _ = app_thread.emit("rdt-model-download-status", snapshot);
            },
        );

        if let Err(finalize_error) = terminal_result {
            let message = format!(
                "The downloader process group was stopped, but terminal state could not be committed: {finalize_error}. The download remains locked; select Try cancel again."
            );
            publish_model_download_failure_for_identity_with_gate(
                &finalization_gate,
                &pid_store,
                &status_store,
                &app_thread,
                &identity,
                &message,
            );
        }

        let download_succeeded = match status_store.lock() {
            Ok(status) => !status.active && status.phase == "complete",
            Err(_) => {
                eprintln!("RDT Connect could not confirm the downloader terminal state; retaining its private attempt log.");
                false
            }
        };
        if download_succeeded {
            clear_model_download_log_path(&log_path_store, &log_path);
        } else {
            retain_model_download_log_path(&log_path_store, &log_path);
        }
    });
    Ok(initial)
}

// Model downloads use their own process group (CommandExt::process_group(0)). The
// captured leader identity includes start time, PGID and full command. Helper processes
// such as Python's resource_tracker are permitted only within that isolated group.
fn model_download_group_snapshot_is_owned<F>(
    identity: &ProcessSnapshot,
    snapshot: &[ProcessSnapshot],
    descendants_remain: bool,
    is_expected_backend: F,
) -> Result<bool, String>
where
    F: Fn(&str) -> bool,
{
    if identity.pid == 0 || identity.pgid != identity.pid {
        return Err("The stored downloader identity is not an isolated process group.".into());
    }
    if snapshot.is_empty() {
        return Err(
            "Could not inspect the process table; no further cancellation signal was sent.".into(),
        );
    }

    let has_live_group_member = snapshot
        .iter()
        .any(|item| item.pgid == identity.pgid && !item.stat.starts_with('Z'));

    if !has_live_group_member {
        if descendants_remain {
            return Err("The downloader leader exited while descendant processes remain. RDT Connect cannot safely identify or terminate those helpers yet; no cancellation success was reported.".into());
        }
        return Ok(false);
    }

    if let Some(leader) = snapshot.iter().find(|item| item.pid == identity.pid) {
        // The original leader may exit while an owned helper remains in its
        // isolated process group. The numeric PID can then identify an unrelated
        // process outside that group. Never signal that PID; cleanup targets only
        // the captured PGID, whose live membership was established above.
        if leader.pgid == identity.pgid {
            if leader.started != identity.started || leader.command != identity.command {
                return Err("The downloader PID now identifies a different process inside its original process group; no process-group signal was sent.".into());
            }
            if !is_expected_backend(&leader.command) {
                return Err("The original downloader leader no longer matches the packaged backend; no process-group signal was sent.".into());
            }
        }
    }

    // Python's multiprocessing.resource_tracker and similar helpers may have a
    // different command line, but they inherit this uniquely isolated PGID.
    // A populated isolated PGID cannot be reused while any member remains.
    // Never signal by port, process name, or an unverified enumeration index.
    Ok(true)
}

fn model_download_process_identity_matches(
    captured: &ProcessSnapshot,
    expected: &ProcessSnapshot,
) -> bool {
    captured.pid == expected.pid
        && captured.pgid == expected.pgid
        && captured.started == expected.started
        && captured.command == expected.command
}

fn model_download_cancel_attempt_is_current(
    cancellation_requested: bool,
    captured_identity: Option<&ProcessSnapshot>,
    expected_identity: &ProcessSnapshot,
    status_is_active_canceling: bool,
) -> bool {
    cancellation_requested
        && status_is_active_canceling
        && captured_identity.is_some_and(|captured| {
            model_download_process_identity_matches(captured, expected_identity)
        })
}

fn model_download_cancel_transition(current: &ModelDownloadStatus) -> Option<ModelDownloadStatus> {
    if !current.active || current.phase == "canceling" {
        return None;
    }
    let mut next = current.clone();
    next.phase = "canceling".into();
    next.message = "Stopping the download and its helper workers…".into();
    Some(next)
}

// Call only after the supervisor has confirmed that the captured process
// group and its owned helpers are gone. Lock order is:
// finalization gate -> PID identity -> status.
// No state is changed until both state locks have been acquired and checked.
fn finalize_model_download_state_with<Decide, Publish>(
    finalization_gate: &Mutex<()>,
    pid_store: &Mutex<Option<ProcessSnapshot>>,
    status_store: &Mutex<ModelDownloadStatus>,
    cancel_requested: &AtomicBool,
    expected_identity: &ProcessSnapshot,
    require_canceling: bool,
    decide: Decide,
    publish: Publish,
) -> Result<(), String>
where
    Decide: FnOnce(bool, &ModelDownloadStatus) -> (String, String),
    Publish: FnOnce(ModelDownloadStatus),
{
    let gate = finalization_gate
        .lock()
        .map_err(|_| "Model-download finalization lock is unavailable.".to_string())?;

    let mut current_identity = pid_store
        .lock()
        .map_err(|_| "Model-download process identity is unavailable.".to_string())?;

    if !current_identity.as_ref().is_some_and(|captured| {
        model_download_process_identity_matches(captured, expected_identity)
    }) {
        return Err(
            "The captured downloader identity changed; terminal state was not published and the download remains locked."
                .into(),
        );
    }

    let mut status = status_store
        .lock()
        .map_err(|_| "Model-download status is unavailable.".to_string())?;

    if !status.active || (require_canceling && status.phase != "canceling") {
        return Err(
            "The downloader attempt is no longer eligible for this terminal update; ownership was retained."
                .into(),
        );
    }

    let (phase, message) = decide(cancel_requested.load(Ordering::SeqCst), &status);
    let is_complete = phase == "complete";

    status.phase = phase;
    status.message = message;
    status.active = false;

    if is_complete {
        status.progress_percent = 100;
        if let Some(total) = status.total_bytes {
            status.downloaded_bytes = status.downloaded_bytes.max(total);
        }
    }

    // Commit PID, status and cancel flag while both state locks are held.
    *current_identity = None;
    cancel_requested.store(false, Ordering::SeqCst);
    let snapshot = status.clone();

    drop(status);
    drop(current_identity);

    // Hold the gate through publication so a new Start cannot overtake this
    // attempt's terminal status event.
    publish(snapshot);
    drop(gate);
    Ok(())
}

// A cleanup failure is scoped to this exact active attempt. It never releases
// the PID or active lock and cannot overwrite a newer download's status.
fn publish_model_download_failure_for_identity_with_gate(
    finalization_gate: &Mutex<()>,
    pid_store: &Mutex<Option<ProcessSnapshot>>,
    status_store: &Mutex<ModelDownloadStatus>,
    app: &AppHandle,
    expected_identity: &ProcessSnapshot,
    message: &str,
) {
    let _gate = match finalization_gate.lock() {
        Ok(gate) => gate,
        Err(_) => return,
    };

    let current_identity = match pid_store.lock() {
        Ok(identity) => identity,
        Err(_) => return,
    };

    if !current_identity.as_ref().is_some_and(|captured| {
        model_download_process_identity_matches(captured, expected_identity)
    }) {
        return;
    }

    let snapshot = {
        let mut status = match status_store.lock() {
            Ok(status) => status,
            Err(_) => return,
        };
        if !status.active {
            return;
        }
        status.phase = "failed".into();
        status.message = message.to_string();
        status.clone()
    };

    drop(current_identity);
    let _ = app.emit("rdt-model-download-status", snapshot);
}

fn model_download_group_members_are_owned(
    identity: &ProcessSnapshot,
    expected_exe: &Path,
) -> Result<bool, String> {
    if identity.pid == 0 || identity.pgid != identity.pid {
        return Err("The stored downloader identity is not an isolated process group.".into());
    }

    let snapshot = process_snapshot_result()?;
    #[cfg(windows)]
    let has_live_group_member = snapshot
        .iter()
        .any(|item| item.pgid == identity.pgid && !item.stat.starts_with('Z'));

    #[cfg(windows)]
    let descendants_remain =
        !has_live_group_member && !windows_descendant_pids(identity.pid, &snapshot).is_empty();
    #[cfg(not(windows))]
    let descendants_remain = false;

    model_download_group_snapshot_is_owned(identity, &snapshot, descendants_remain, |command| {
        command_is_expected_backend(command, expected_exe)
    })
}

// Send TERM to only the verified download process group, allow graceful shutdown,
// then revalidate identity before escalating to KILL. Every wait is bounded.
fn finish_stopping_model_download_group_with<Owned, Signal, Alive, Wait>(
    identity: &ProcessSnapshot,
    mut is_owned: Owned,
    mut send_signal: Signal,
    mut group_alive: Alive,
    mut wait_until_unowned: Wait,
) -> Result<(), String>
where
    Owned: FnMut(&ProcessSnapshot) -> Result<bool, String>,
    Signal: FnMut(u32, i32) -> Result<(), String>,
    Alive: FnMut(u32) -> bool,
    Wait: FnMut(&ProcessSnapshot, Duration) -> Result<bool, String>,
{
    if !is_owned(identity)? {
        return Ok(());
    }

    let group_id = identity.pgid;
    if let Err(error) = send_signal(group_id, SIGNAL_TERM) {
        if !group_alive(group_id) {
            return Ok(());
        }
        return Err(format!(
            "Could not request graceful cleanup of the verified downloader group: {error}"
        ));
    }

    if wait_until_unowned(identity, Duration::from_millis(700))? {
        return Ok(());
    }

    // Ownership is deliberately checked immediately before SIGKILL.
    if !is_owned(identity)? {
        return Ok(());
    }

    if let Err(error) = send_signal(group_id, SIGNAL_KILL) {
        if !group_alive(group_id) {
            return Ok(());
        }
        return Err(format!(
            "Could not force-stop the verified downloader group: {error}"
        ));
    }

    if wait_until_unowned(identity, Duration::from_secs(10))? {
        return Ok(());
    }

    Err("The verified download process group is still active after bounded termination.".into())
}

fn finish_stopping_model_download_group(
    identity: &ProcessSnapshot,
    expected_exe: &Path,
) -> Result<(), String> {
    finish_stopping_model_download_group_with(
        identity,
        |captured| model_download_group_members_are_owned(captured, expected_exe),
        |group_id, signal| send_signal_checked(-(group_id as i32), signal),
        process_group_alive,
        |captured, timeout| {
            let deadline = Instant::now() + timeout;
            loop {
                if !model_download_group_members_are_owned(captured, expected_exe)? {
                    return Ok(true);
                }
                if Instant::now() >= deadline {
                    return Ok(false);
                }
                thread::sleep(Duration::from_millis(75));
            }
        },
    )
}

#[tauri::command]
fn cancel_model_download(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<ModelDownloadStatus, String> {
    // Lock order matches Start: lifecycle -> finalization -> PID -> status.
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let _finalization = state
        .model_download_finalization
        .lock()
        .map_err(|_| "Model-download finalization lock is unavailable.".to_string())?;

    let current_snapshot = state
        .model_download_status
        .lock()
        .map_err(|_| "Model-download state is unavailable.".to_string())?
        .clone();

    let Some(mut snapshot) = model_download_cancel_transition(&current_snapshot) else {
        // Inactive cancellation is a no-op. Repeated clicks during the same
        // canceling attempt must not create multiple escalation threads.
        return Ok(current_snapshot);
    };

    let identity = state
        .model_download_pid
        .lock()
        .map_err(|_| "Model-download process state is unavailable.".to_string())?
        .clone();

    let Some(identity) = identity else {
        snapshot.phase = "failed".into();
        snapshot.message = "No active downloader process was recorded. Refresh status, then retry Cancel or retry the download.".into();
        *state
            .model_download_status
            .lock()
            .map_err(|_| "Model-download state is unavailable.".to_string())? = snapshot.clone();
        let _ = app.emit("rdt-model-download-status", snapshot.clone());
        return Ok(snapshot);
    };

    let expected_exe = backend_executable_path(&state.resource_dir);
    if let Ok(current_log) = state.model_download_log_path.lock() {
        if let Some(path) = current_log.as_ref() {
            append_model_download_process_snapshot(
                path,
                "cancel-before-ownership-check",
                &identity,
                &expected_exe,
            );
        }
    }
    let group_owned = match model_download_group_members_are_owned(&identity, &expected_exe) {
        Ok(owned) => owned,
        Err(error) => {
            snapshot.phase = "failed".into();
            snapshot.message = format!("Cancellation could not verify the download process safely: {error} Select Try cancel again after checking Developer Options → Diagnostics.");
            *state
                .model_download_status
                .lock()
                .map_err(|_| "Model-download state is unavailable.".to_string())? =
                snapshot.clone();
            let _ = app.emit("rdt-model-download-status", snapshot.clone());
            return Ok(snapshot);
        }
    };

    // Store canceling status before setting the atomic flag. Holding the shared
    // gate prevents worker finalization during this short transition.
    {
        let mut status = state
            .model_download_status
            .lock()
            .map_err(|_| "Model-download state is unavailable.".to_string())?;
        *status = snapshot.clone();
        state
            .model_download_cancel_requested
            .store(true, Ordering::SeqCst);
    }
    let _ = app.emit("rdt-model-download-status", snapshot.clone());

    if group_owned {
        if let Err(signal_error) = send_signal_checked(-(identity.pgid as i32), SIGNAL_TERM) {
            // Revalidate ownership. A process-table failure is not evidence that
            // the group exited and must never permit terminal publication.
            match model_download_group_members_are_owned(&identity, &expected_exe) {
                Ok(false) => {}
                Ok(true) => {
                    snapshot.phase = "failed".into();
                    snapshot.message = format!("Could not request cancellation: {signal_error}. Select Try cancel again to retry.");
                    *state
                        .model_download_status
                        .lock()
                        .map_err(|_| "Model-download state is unavailable.".to_string())? =
                        snapshot.clone();
                    let _ = app.emit("rdt-model-download-status", snapshot.clone());
                    return Ok(snapshot);
                }
                Err(check_error) => {
                    snapshot.phase = "failed".into();
                    snapshot.message = format!("Cancellation could not confirm downloader shutdown after TERM failed: {signal_error}; ownership recheck failed: {check_error}. Select Try cancel again.");
                    *state
                        .model_download_status
                        .lock()
                        .map_err(|_| "Model-download state is unavailable.".to_string())? =
                        snapshot.clone();
                    let _ = app.emit("rdt-model-download-status", snapshot.clone());
                    return Ok(snapshot);
                }
            }
        }
    }
    // An already-absent group is never signaled. The delayed supervisor still
    // confirms the captured attempt before committing its terminal state.

    let app_for_escalation = app.clone();
    let status_store = Arc::clone(&state.model_download_status);
    let pid_store = Arc::clone(&state.model_download_pid);
    let log_path_store = Arc::clone(&state.model_download_log_path);
    let cancel_requested = Arc::clone(&state.model_download_cancel_requested);
    let finalization_gate = Arc::clone(&state.model_download_finalization);
    let identity_for_escalation = identity.clone();
    let model = snapshot.model.clone();
    let destination_root = snapshot.destination_root.clone();
    let expected_log_path = log_path_store
        .lock()
        .map(|current| current.clone())
        .map_err(|_| {
            "The model-download log-path mutex is poisoned; log cleanup may be skipped.".to_string()
        });

    thread::spawn(move || {
        // Existing production grace interval; do not shorten for tests.
        thread::sleep(Duration::from_millis(900));

        // First attempt guard. Mutex poison, a changed identity, or a terminal
        // worker result prevents any delayed stop/escalation.
        let attempt_gate = match finalization_gate.lock() {
            Ok(gate) => gate,
            Err(_) => return,
        };
        let captured_identity = match pid_store.lock() {
            Ok(current) => current.clone(),
            Err(_) => return,
        };
        let status_is_active_canceling = match status_store.lock() {
            Ok(status) => status.active && status.phase == "canceling",
            Err(_) => return,
        };
        let still_this_attempt = model_download_cancel_attempt_is_current(
            cancel_requested.load(Ordering::SeqCst),
            captured_identity.as_ref(),
            &identity_for_escalation,
            status_is_active_canceling,
        );
        drop(attempt_gate);
        if !still_this_attempt {
            return;
        }

        if let Err(error) =
            finish_stopping_model_download_group(&identity_for_escalation, &expected_exe)
        {
            let message = format!("Cancellation could not be confirmed: {error} Select Try cancel again; no unverified process was signaled.");
            publish_model_download_failure_for_identity_with_gate(
                &finalization_gate,
                &pid_store,
                &status_store,
                &app_for_escalation,
                &identity_for_escalation,
                &message,
            );
            return;
        }

        // Recheck after process cleanup; the worker may have published a
        // terminal result while the bounded stop operation was running.
        let attempt_gate = match finalization_gate.lock() {
            Ok(gate) => gate,
            Err(_) => return,
        };
        let captured_identity = match pid_store.lock() {
            Ok(current) => current.clone(),
            Err(_) => return,
        };
        let status_is_active_canceling = match status_store.lock() {
            Ok(status) => status.active && status.phase == "canceling",
            Err(_) => return,
        };
        let still_this_attempt = model_download_cancel_attempt_is_current(
            cancel_requested.load(Ordering::SeqCst),
            captured_identity.as_ref(),
            &identity_for_escalation,
            status_is_active_canceling,
        );
        drop(attempt_gate);
        if !still_this_attempt {
            return;
        }

        let cache_valid = model
            .as_deref()
            .zip(destination_root.as_deref())
            .is_some_and(|(model, root)| model_cache_fully_verified(Path::new(root), model));

        let finalized = finalize_model_download_state_with(
            &finalization_gate,
            &pid_store,
            &status_store,
            &cancel_requested,
            &identity_for_escalation,
            true,
            |_, _| {
                if cache_valid {
                    (
                        "complete".into(),
                        "The MLX model files are complete and verified, and the downloader has been safely stopped. Model loading and real inference have not yet been verified.".into(),
                    )
                } else {
                    (
                        "cancelled".into(),
                        "Download canceled. Only complete, verified MLX model files will be shown as Downloaded; completed files can be reused on retry.".into(),
                    )
                }
            },
            |snapshot| {
                let _ = app_for_escalation.emit("rdt-model-download-status", snapshot);
            },
        );

        // Log cleanup is post-commit housekeeping. It cannot affect whether
        // process shutdown was verified or whether terminal state was committed.
        if finalized.is_ok() {
            match expected_log_path {
                Ok(Some(path)) => retain_model_download_log_path(&log_path_store, &path),
                Ok(None) => {}
                Err(error) => {
                    // Process shutdown and terminal state are already committed.
                    // A log-bookkeeping failure must not roll them back.
                    eprintln!(
                        "RDT Connect confirmed downloader shutdown; private log cleanup skipped: {error}"
                    );
                }
            }
        }
    });

    Ok(snapshot)
}

fn canonical_mlx_cache_repository(hub: &Path, repo: &Path) -> Result<PathBuf, String> {
    let hub_meta = fs::symlink_metadata(hub)
        .map_err(|error| format!("Could not inspect Hugging Face hub: {error}"))?;
    if hub_meta.file_type().is_symlink() || !hub_meta.is_dir() {
        return Err("Hugging Face hub is not a real directory.".into());
    }
    let canonical_hub = fs::canonicalize(hub)
        .map_err(|error| format!("Could not resolve Hugging Face hub: {error}"))?;

    let repo_meta = fs::symlink_metadata(repo)
        .map_err(|error| format!("Could not inspect repository: {error}"))?;
    if repo_meta.file_type().is_symlink() || !repo_meta.is_dir() {
        return Err("Repository is not a real directory.".into());
    }
    let canonical_repo =
        fs::canonicalize(repo).map_err(|error| format!("Could not resolve repository: {error}"))?;
    if canonical_repo.parent() != Some(canonical_hub.as_path()) {
        return Err("Repository resolves outside its direct Hugging Face hub parent.".into());
    }
    Ok(canonical_repo)
}

fn remove_regular_model_companion(path: &Path) -> Result<bool, String> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(format!("Could not inspect entry: {error}")),
    };

    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("Refusing to remove a symlink or non-regular file.".into());
    }

    fs::remove_file(path)
        .map(|()| true)
        .map_err(|error| format!("Could not remove regular file: {error}"))
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
    let download_active = state
        .model_download_status
        .lock()
        .map(|status| status.active)
        .unwrap_or(true);
    if download_active {
        return Err(
            "Finish or cancel the active model download before deleting cached models.".into(),
        );
    }
    if models.is_empty() {
        return Err("Select at least one downloaded RDT Connect model to delete.".into());
    }
    let model_ids = platform_model_ids();
    if models
        .iter()
        .any(|model| !model_ids.contains(&model.as_str()))
    {
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
            let root_meta = match fs::symlink_metadata(root) {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
                failures.push(format!(
                    "Skipped unsafe model-storage root: {}",
                    root.display()
                ));
                continue;
            }
            let canonical_storage_root = match fs::canonicalize(root) {
                Ok(path) => path,
                Err(_) => {
                    failures.push(format!(
                        "Could not resolve model-storage root: {}",
                        root.display()
                    ));
                    continue;
                }
            };
            checked += 1;
            let ggml = root.join("ggml");
            let ggml_meta = match fs::symlink_metadata(&ggml) {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if ggml_meta.file_type().is_symlink() || !ggml_meta.is_dir() {
                failures.push(format!(
                    "Skipped unsafe GGML directory under {}",
                    root.display()
                ));
                continue;
            }
            let canonical_root = match fs::canonicalize(&ggml) {
                Ok(path) if path.parent() == Some(canonical_storage_root.as_path()) => path,
                _ => {
                    failures.push(format!(
                        "Skipped out-of-root GGML directory under {}",
                        root.display()
                    ));
                    continue;
                }
            };
            for model in selected_models
                .iter()
                .copied()
                .filter(|id| native_ggml_spec(id).is_some())
            {
                let (filename, _, _) = native_ggml_spec(model).expect("filtered spec");
                let artifact = ggml.join(filename);
                let sidecar = artifact.with_file_name(format!("{filename}.rdt-verified.json"));
                let meta = match fs::symlink_metadata(&artifact) {
                    Ok(m) => m,
                    Err(_) => continue,
                };
                if meta.file_type().is_symlink() || !meta.is_file() {
                    failures.push(format!(
                        "Skipped unsafe GGML entry under {}",
                        root.display()
                    ));
                    continue;
                }
                let canonical = match fs::canonicalize(&artifact) {
                    Ok(p) => p,
                    Err(_) => continue,
                };
                if canonical.parent() != Some(canonical_root.as_path()) {
                    failures.push(format!(
                        "Skipped out-of-root GGML entry under {}",
                        root.display()
                    ));
                    continue;
                }
                match fs::remove_file(&artifact) {
                    Ok(()) => {
                        deleted += 1;
                        bytes = bytes.saturating_add(meta.len());

                        let partial = artifact.with_file_name(format!("{filename}.part"));
                        for companion in [&sidecar, &partial] {
                            if let Err(error) = remove_regular_model_companion(companion) {
                                failures.push(format!(
                                    "Could not safely remove GGML companion {}: {error}",
                                    companion.display()
                                ));
                            }
                        }
                    }
                    Err(e) => failures.push(format!(
                        "Could not remove cached GGML model under {}: {e}",
                        root.display()
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
            "No verified RDT Connect GGML model files were found in configured storage roots."
                .into()
        } else if failures.is_empty() {
            format!("Removed {deleted} RDT Connect GGML model file(s) ({freed}); unrelated files were left unchanged.")
        } else {
            format!(
                "Removed {deleted} GGML model file(s) ({freed}) with {} safety/removal warning(s).",
                failures.len()
            )
        };
        return Ok(ModelCacheDeleteResult {
            deleted_repositories: deleted,
            locations_checked: checked,
            approximate_bytes_freed: bytes,
            message,
        });
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
        let root_meta = match fs::symlink_metadata(root) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
            failures.push(format!(
                "Skipped unsafe model-storage root: {}",
                root.display()
            ));
            continue;
        }
        let canonical_storage_root = match fs::canonicalize(root) {
            Ok(path) => path,
            Err(_) => {
                failures.push(format!(
                    "Could not resolve model-storage root: {}",
                    root.display()
                ));
                continue;
            }
        };
        checked += 1;
        let hub = root.join("hub");
        let hub_meta = match fs::symlink_metadata(&hub) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if hub_meta.file_type().is_symlink() || !hub_meta.is_dir() {
            failures.push(format!(
                "Skipped unsafe Hugging Face hub directory under {}",
                root.display()
            ));
            continue;
        }
        match fs::canonicalize(&hub) {
            Ok(path) if path.parent() == Some(canonical_storage_root.as_path()) => {}
            _ => {
                failures.push(format!(
                    "Skipped out-of-root Hugging Face hub directory under {}",
                    root.display()
                ));
                continue;
            }
        }
        for model in MODEL_IDS
            .into_iter()
            .filter(|model| selected_models.contains(model))
        {
            let key = format!("models--{}", model.replace('/', "--"));
            let repo = hub.join(key);
            let canonical_repo = match canonical_mlx_cache_repository(&hub, &repo) {
                Ok(path) => path,
                Err(reason) => {
                    failures.push(format!(
                        "Skipped unsafe MLX cache entry in {}: {reason}",
                        root.display()
                    ));
                    continue;
                }
            };
            let size = approximate_directory_bytes(&canonical_repo);
            match fs::remove_dir_all(&canonical_repo) {
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
fn open_easywhisper_model_manager(
    state: State<'_, BackendState>,
    browser_id: Option<String>,
) -> Result<(), String> {
    let status = state.status();
    let _port = status
        .port
        .ok_or_else(|| "Start the local backend first.".to_string())?;
    let backend_url = status
        .local_url
        .clone()
        .ok_or_else(|| "The local server has no active endpoint.".to_string())?;

    let handoff = api_request(&state, "POST", "/v1/browser-handoff", Some(json!({})))?;
    let code = handoff
        .get("code")
        .and_then(Value::as_str)
        .filter(|value| value.len() >= 32)
        .ok_or_else(|| {
            "The backend did not return a valid one-time browser handoff code.".to_string()
        })?;

    let locale = read_app_language(&state);
    let fragment = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("rdt_connect_handoff", code)
        .append_pair("rdt_connect_backend", &backend_url)
        .finish();

    let mut launch_url = url::Url::parse(&backend_url)
        .map_err(|e| format!("The local RDT Voice endpoint is invalid: {e}"))?;
    launch_url.set_path("/");
    launch_url.set_query(None);
    launch_url
        .query_pairs_mut()
        .append_pair("rdt_connect_lang", &locale)
        .append_pair("rdt_manage_easywhisper_models", "1");
    launch_url.set_fragment(Some(&fragment));

    #[cfg(target_os = "macos")]
    {
        let selected_browser = browser_id.as_deref().unwrap_or("default");
        let mut command = Command::new("/usr/bin/open");
        if selected_browser != "default" {
            let bundle_id = browser_bundle_id(selected_browser).ok_or_else(|| {
                "Choose a supported browser from the Local Server browser list.".to_string()
            })?;
            if !browser_app_is_installed(selected_browser) {
                return Err("The selected browser is no longer installed. Choose another browser and retry.".into());
            }
            command.arg("-b").arg(bundle_id);
        }
        command
            .arg(launch_url.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("Could not open the model manager: {e}"))
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(launch_url.as_str(), browser_id.as_deref())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = browser_id;
        Command::new("xdg-open")
            .arg(launch_url.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("Could not open the model manager in the default browser: {e}"))
    }
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ModelStorageConfig {
    locations: Vec<String>,
    #[serde(default)]
    active_root: Option<String>,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ModelStorageStatus {
    model: String,
    locations: Vec<String>,
    locations_availability: Vec<String>,
    active_root: String,
    selected_root: String,
    model_cached: bool,
    cached_model_path: Option<String>,
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

fn load_model_storage_selected_root(
    state: &BackendState,
    locations: &[String],
) -> Result<String, String> {
    let fallback = locations
        .first()
        .cloned()
        .ok_or_else(|| "No model-storage location is configured. Open Settings.".to_string())?;
    if !state.model_storage_config_path.is_file() {
        return Ok(fallback);
    }
    let raw = fs::read(&state.model_storage_config_path)
        .map_err(|e| format!("Could not read model-storage settings: {e}"))?;
    let config: ModelStorageConfig = serde_json::from_slice(&raw)
        .map_err(|e| format!("Model-storage settings are not valid JSON: {e}"))?;
    let selected = config.active_root.unwrap_or(fallback);
    if !Path::new(&selected).is_absolute() || !locations.contains(&selected) {
        return Err("The selected model-download folder is not in the configured folder list. Open Settings and select a valid folder.".into());
    }
    Ok(selected)
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
    let modified_ns = meta
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .and_then(|duration| i64::try_from(duration.as_nanos()).ok());
    modified_ns.is_some() && marker.get("mtime_ns").and_then(Value::as_i64) == modified_ns
}

#[cfg(target_os = "macos")]
fn mounted_storage_volume_roots() -> Vec<PathBuf> {
    // MNT_NOWAIT reads the mount census without opening configured user paths.
    let count = unsafe { libc::getfsstat(std::ptr::null_mut(), 0, libc::MNT_NOWAIT) };
    if count <= 0 {
        return Vec::new();
    }

    let capacity = (count as usize).min(8192);
    let mut filesystems: Vec<libc::statfs> = (0..capacity)
        .map(|_| unsafe { std::mem::zeroed() })
        .collect();

    let Some(buffer_bytes) = filesystems
        .len()
        .checked_mul(std::mem::size_of::<libc::statfs>())
        .and_then(|size| libc::c_int::try_from(size).ok())
    else {
        return Vec::new();
    };

    let actual =
        unsafe { libc::getfsstat(filesystems.as_mut_ptr(), buffer_bytes, libc::MNT_NOWAIT) };
    if actual <= 0 {
        return Vec::new();
    }

    let actual_len = (actual as usize).min(filesystems.len());
    filesystems.truncate(actual_len);

    filesystems
        .into_iter()
        .filter_map(|filesystem| {
            let mount = unsafe { std::ffi::CStr::from_ptr(filesystem.f_mntonname.as_ptr()) };
            mount.to_str().ok().map(PathBuf::from)
        })
        .filter(|mount| mount.starts_with("/Volumes") && mount.as_path() != Path::new("/Volumes"))
        .collect()
}

#[cfg(not(target_os = "macos"))]
fn mounted_storage_volume_roots() -> Vec<PathBuf> {
    Vec::new()
}

fn model_storage_location_is_available(path: &Path, mounted_volumes: &[PathBuf]) -> bool {
    if cfg!(target_os = "macos") && path.starts_with("/Volumes") {
        let mounted = mounted_volumes.iter().any(|mount| path.starts_with(mount));
        if !mounted {
            // Do not stat or canonicalize a stale removable-volume path.
            return false;
        }
    }
    path.is_dir()
}

fn model_cache_snapshot_path(root: &Path, model: &str) -> Option<PathBuf> {
    let revision = model_lock_revision(model)?;
    let repo_key = format!("models--{}", model.replace('/', "--"));
    let snapshot = root
        .join("hub")
        .join(repo_key)
        .join("snapshots")
        .join(revision);
    model_snapshot_is_complete(root, model, &snapshot).then_some(snapshot)
}

fn model_cache_has_weights(root: &Path, model: &str) -> bool {
    model_cache_snapshot_path(root, model).is_some()
}

fn model_cache_fully_verified(root: &Path, model: &str) -> bool {
    if !model_cache_has_weights(root, model) {
        return false;
    }
    if !cfg!(any(target_os = "windows", target_os = "linux")) {
        return true;
    }
    let Some((filename, expected_sha, _revision)) = native_ggml_spec(model) else {
        return false;
    };
    let artifact = root.join("ggml").join(filename);
    let Ok(mut file) = File::open(&artifact) else {
        return false;
    };
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
    let selected_root = load_model_storage_selected_root(state, &locations)?;
    let mounted_volumes = mounted_storage_volume_roots();

    // Probe each configured root once. Do not probe later removable volumes
    // after finding the first verified cached copy.
    let mut availability: Vec<Option<bool>> = vec![None; locations.len()];
    let mut cached_index: Option<usize> = None;

    for (index, root) in locations.iter().enumerate() {
        let path = Path::new(root.as_str());

        if cached_index
            .map(|chosen| index > chosen && path.starts_with("/Volumes"))
            .unwrap_or(false)
        {
            continue;
        }

        let available = model_storage_location_is_available(path, &mounted_volumes);
        availability[index] = Some(available);

        if cached_index.is_none() && available && model_cache_has_weights(path, model) {
            cached_index = Some(index);
        }
    }

    let locations_availability = availability
        .iter()
        .map(|value| match value {
            Some(true) => "available".to_string(),
            Some(false) => "unavailable".to_string(),
            None => "not-checked".to_string(),
        })
        .collect::<Vec<_>>();

    let cached_root = cached_index.and_then(|index| locations.get(index));
    let model_cached = cached_root.is_some();
    let cached_model_path = cached_root.and_then(|root| {
        model_cache_snapshot_path(Path::new(root.as_str()), model)
            .map(|path| path.to_string_lossy().into_owned())
    });

    // Keep the verified cache location distinct from the selected download folder.
    let active_root = cached_root
        .map(|root| root.to_string())
        .unwrap_or_else(|| selected_root.clone());

    Ok(ModelStorageStatus {
        model: model.to_string(),
        locations,
        locations_availability,
        active_root,
        selected_root,
        model_cached,
        cached_model_path,
        server_running: state.status().running,
    })
}

fn resolve_model_storage_root(
    state: &BackendState,
    model: &str,
) -> Result<(PathBuf, bool), String> {
    validate_model(model)?;
    let locations = load_model_storage_locations(state)?;
    let mounted_volumes = mounted_storage_volume_roots();

    // Existing models remain discoverable in every configured location.
    for configured_root in &locations {
        let path = Path::new(configured_root.as_str());
        if model_storage_location_is_available(path, &mounted_volumes)
            && model_cache_has_weights(path, model)
        {
            return Ok((path.to_path_buf(), true));
        }
    }

    // New downloads use the separately persisted radio selection.
    let selected_path = PathBuf::from(load_model_storage_selected_root(state, &locations)?);
    if model_storage_location_is_available(&selected_path, &mounted_volumes) {
        return Ok((selected_path, false));
    }

    if selected_path.starts_with("/Volumes") {
        return Err(format!(
            "The selected model-download folder {} is not mounted. Reconnect it or select another download folder in Settings. RDT Connect will not silently download to a different folder.",
            selected_path.display()
        ));
    }
    let home = home_dir()?;
    if selected_path.starts_with(&home) {
        fs::create_dir_all(&selected_path).map_err(|error| {
            format!(
                "Could not create the selected model-download folder {}: {error}",
                selected_path.display()
            )
        })?;
        return Ok((selected_path, false));
    }
    Err(format!(
        "The selected model-download folder {} is unavailable. Make it available or select another folder in Settings. RDT Connect will not silently download to a different folder.",
        selected_path.display()
    ))
}

fn normalize_model_storage_locations(locations: Vec<String>) -> Result<Vec<String>, String> {
    if locations.is_empty() || locations.len() > MODEL_STORAGE_MAX_LOCATIONS {
        return Err(format!(
            "Choose between 1 and {MODEL_STORAGE_MAX_LOCATIONS} model-storage folders."
        ));
    }
    let mounted_volumes = mounted_storage_volume_roots();
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
        if !model_storage_location_is_available(&path, &mounted_volumes) {
            // Keep an already configured removable-drive path in the ordered list even
            // while that volume is disconnected. Existing cached models may still be
            // found in other configured roots, but a new download to this selected path
            // must fail closed. This branch never creates the missing volume or a directory beneath it.
            #[cfg(target_os = "macos")]
            let is_unmounted_volume = raw.starts_with("/Volumes/")
                && path
                    .components()
                    .all(|component| !matches!(component, std::path::Component::ParentDir))
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

fn model_storage_append_only_change_allowed(
    existing_locations: &[String],
    existing_root: &str,
    locations: &[String],
    active_root: &str,
) -> bool {
    locations.len() > existing_locations.len()
        && locations.starts_with(existing_locations)
        && active_root == existing_root
}

fn save_model_storage_locations(
    state: &BackendState,
    locations: Vec<String>,
    active_root: String,
) -> Result<Vec<String>, String> {
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    let server_running = state.status().running;
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
    let mut selected = normalize_model_storage_locations(vec![active_root])?;
    if selected.len() != 1 || !locations.contains(&selected[0]) {
        return Err("Select a download folder from the configured folder list.".into());
    }
    let active_root = selected.remove(0);
    if server_running {
        let existing_locations = load_model_storage_locations(state)?;
        let existing_root = load_model_storage_selected_root(state, &existing_locations)?;
        if !model_storage_append_only_change_allowed(
            &existing_locations,
            &existing_root,
            &locations,
            &active_root,
        ) {
            return Err("While the server is running, storage folders may only be appended; existing folders and the download destination cannot be changed.".into());
        }
    }
    let payload = serde_json::to_vec_pretty(&ModelStorageConfig {
        locations: locations.clone(),
        active_root: Some(active_root),
    })
    .map_err(|e| format!("Could not encode model-storage settings: {e}"))?;
    write_model_storage_config_atomic(&state.model_storage_config_path, &payload)?;
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
async fn set_model_storage_locations(
    app: AppHandle,
    model: String,
    locations: Vec<String>,
    active_root: String,
) -> Result<ModelStorageStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<BackendState>();
        validate_model(&model)?;
        save_model_storage_locations(&state, locations, active_root)?;
        make_model_storage_status(&state, &model)
    })
    .await
    .map_err(|error| format!("Model-storage settings worker failed: {error}"))?
}

#[tauri::command]
fn reveal_model_storage_folder(path: String) -> Result<(), String> {
    let requested = std::path::PathBuf::from(path);
    let metadata = std::fs::metadata(&requested)
        .map_err(|error| format!("The model storage folder is unavailable: {error}"))?;
    if !metadata.is_dir() {
        return Err("The selected model storage path is not a folder.".into());
    }
    let folder = std::fs::canonicalize(&requested)
        .map_err(|error| format!("Could not resolve the model storage folder: {error}"))?;

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&folder)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("Finder could not open the folder: {error}"))
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut command = std::process::Command::new("explorer.exe");
        command.creation_flags(WINDOWS_CREATE_NO_WINDOW);
        command
            .arg(&folder)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("File Explorer could not open the folder: {error}"))
    }

    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&folder)
            .spawn()
            .map(|_| ())
            .map_err(|error| format!("The default file manager could not open the folder: {error}"))
    }

    #[cfg(not(any(target_os = "macos", windows, target_os = "linux")))]
    {
        let _ = folder;
        Err("Opening model storage folders is not supported on this platform.".into())
    }
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
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(&state.log_path)
        .map_err(|e| format!("Could not create or open the backend log: {e}"))?;
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut c = Command::new("/usr/bin/open");
        c.args(["-a", "TextEdit"]);
        c
    };
    #[cfg(target_os = "windows")]
    let mut command = Command::new("notepad.exe");
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = Command::new("xdg-open");
    command
        .arg(&state.log_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("Could not open the backend log: {e}"))
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

fn diagnostic_v2_safe_value(object: &Value, key: &str) -> Option<String> {
    let value = object.get(key)?;
    let raw = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        Value::Bool(flag) => flag.to_string(),
        _ => return None,
    };
    let safe: String = raw.chars().filter(|ch| !ch.is_control()).take(96).collect();
    if safe.trim().is_empty() {
        None
    } else {
        Some(safe)
    }
}

fn diagnostic_v2_record_line(label: &str, record: &Value, fields: &[&str]) -> String {
    let values: Vec<String> = fields
        .iter()
        .filter_map(|key| {
            diagnostic_v2_safe_value(record, key).map(|value| format!("{key}={value}"))
        })
        .collect();
    if values.is_empty() {
        format!("{label}: (no allow-listed fields)")
    } else {
        format!("{label}: {}", values.join("; "))
    }
}

fn format_transcription_diagnostics_v2(http_status: u16, payload: Option<&Value>) -> Vec<String> {
    let mut lines =
        vec!["Recent transcription diagnostics (authenticated, read-only):".to_string()];
    if http_status == 401 || http_status == 403 {
        lines.push(format!("FAIL — diagnostics endpoint rejected authentication/authorization (HTTP {http_status})."));
        return lines;
    }
    if http_status == 404 {
        lines.push("UNAVAILABLE — this backend does not expose /v1/diagnostics/transcriptions/recent (HTTP 404).".into());
        return lines;
    }
    if http_status != 200 {
        lines.push(format!(
            "CHECK — diagnostics endpoint returned HTTP {http_status}."
        ));
        return lines;
    }
    let Some(payload) = payload else {
        lines.push("FAIL — endpoint returned HTTP 200 but the response was not valid JSON.".into());
        return lines;
    };
    let schema = diagnostic_v2_safe_value(payload, "schema").unwrap_or_else(|| "missing".into());
    lines.push(format!("Backend diagnostics schema: {schema}"));
    if schema != "rdt-transcription-diagnostics-v2" {
        lines.push("CHECK — endpoint responded, but its schema is not the expected Diagnostics v2 contract.".into());
        return lines;
    }

    let runtime = payload.get("runtime").unwrap_or(payload);
    let runtime_fields = [
        "provider",
        "active_provider",
        "model",
        "model_id",
        "device",
        "active_device",
        "active_inference_count",
        "inference_lock_busy",
        "queue_busy",
        "readiness",
        "transcription_readiness",
    ];
    lines.push(diagnostic_v2_record_line(
        "Runtime",
        runtime,
        &runtime_fields,
    ));

    let events = payload
        .get("events")
        .or_else(|| payload.get("recent_events"))
        .and_then(Value::as_array);
    let jobs = payload
        .get("jobs")
        .or_else(|| payload.get("recent_jobs"))
        .and_then(Value::as_array);
    let event_count = events.map_or(0, Vec::len);
    let job_count = jobs.map_or(0, Vec::len);
    lines.push(format!("Backend history: {event_count} events; {job_count} jobs (in-memory for this server process)."));

    let event_fields = [
        "job_id",
        "event",
        "stage",
        "status",
        "percent",
        "timestamp",
        "created_at",
        "updated_at",
        "error_type",
        "http_status",
    ];
    if let Some(items) = events {
        for (index, item) in items.iter().rev().take(8).rev().enumerate() {
            lines.push(diagnostic_v2_record_line(
                &format!("Event {}", index + 1),
                item,
                &event_fields,
            ));
        }
    }
    let job_fields = [
        "job_id",
        "status",
        "stage",
        "started_at",
        "updated_at",
        "completed_at",
        "error_type",
        "http_status",
    ];
    if let Some(items) = jobs {
        for (index, item) in items.iter().rev().take(5).rev().enumerate() {
            lines.push(diagnostic_v2_record_line(
                &format!("Job {}", index + 1),
                item,
                &job_fields,
            ));
        }
    }
    if event_count == 0 && job_count == 0 {
        lines.push(
            "No transcription attempts have been recorded in this running backend process yet."
                .into(),
        );
    }
    lines
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
        "Diagnostics schema: rdt-connect-transcription-diagnostics-v2".into(),
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
            format!("Local transport: {}; certificate configured={}; operating-system trust verified={}; RDT-added CA trust entries={}; {}", tls.mode.to_ascii_uppercase(), tls.configured, tls.trusted, tls.rdt_added_trust_count, tls.message)
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
    lines.push(String::new());
    lines.extend(model_download_diagnostics_report(&state));
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
        let transcription_diagnostics = match state.api_credentials() {
            Ok((diagnostic_port, token)) => {
                let tls_config = load_local_tls_config(&state).unwrap_or_default();
                let scheme = if tls_config.mode.eq_ignore_ascii_case("http") { "http" } else { "https" };
                let origin = status.local_url.clone()
                    .unwrap_or_else(|| format!("{scheme}://127.0.0.1:{diagnostic_port}"));
                match local_tls_client(&tls_config, Duration::from_secs(3)) {
                    Ok(client) => match client
                        .get(format!("{origin}/v1/diagnostics/transcriptions/recent"))
                        .header("Authorization", format!("Bearer {token}"))
                        .header("Origin", origin.as_str())
                        .send()
                    {
                        Ok(response) => {
                            let code = response.status().as_u16();
                            let payload = if code == 200 { response.json::<Value>().ok() } else { None };
                            format_transcription_diagnostics_v2(code, payload.as_ref())
                        }
                        Err(_) => vec!["Recent transcription diagnostics: FAIL — authenticated request did not complete within 3 seconds.".into()],
                    },
                    Err(_) => vec!["Recent transcription diagnostics: FAIL — could not create the local HTTP client.".into()],
                }
            }
            Err(_) => vec!["Recent transcription diagnostics: NOT RUN — backend authentication credentials are unavailable.".into()],
        };
        lines.extend(transcription_diagnostics);
        lines.push("Backend event history is intentionally process-local and clears on backend restart; the separate backend log remains on disk and its sanitized tail is included below.".into());
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
    let recent = safe_tail_log(&state.log_path, 80);
    lines.push(String::new());
    lines.push("Recent sanitized backend log (up to 80 lines):".into());
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
async fn get_orphaned_backends(app: AppHandle) -> Result<OrphanScan, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<BackendState>();
        scan_orphaned_backends(&state)
    })
    .await
    .map_err(|error| format!("Orphan-process scan worker failed: {error}"))
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
        return Err(
            "Could not verify the Windows process table; no manager process was signaled.".into(),
        );
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
    let deadline = Instant::now() + Duration::from_secs(10);
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
    let deadline = Instant::now() + Duration::from_secs(10);
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
    if let Some(error) = port_lookup_error.as_deref() {
        append_stop_trace(format!(
            "INITIAL TARGET PORT LOOKUP: unable to verify listening port: {}",
            sanitize_log_line(error, "")
        ));
    }
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
        let _verified_target = verified_backend_process_snapshot(pid, &expected_exe)?;
        let owner = manager_parent_for_backend(&state, pid);
        stop_orphaned_backend_safely(&state, pid)?;
        if let Some((owner_pid, owner_command)) = owner {
            close_legacy_manager_after_stop(owner_pid, &owner_command)?;
        }

        if let Some(port) = port {
            verify_port_released(port, pid)?;
        } else {
            append_stop_trace(format!(
                "PORT RELEASE CHECK UNAVAILABLE: backend PID {pid} had no independently identified listener port; verified process-group cleanup completed."
            ));
        }

        let scan = scan_orphaned_backends(&state);
        if scan.instances.iter().any(|item| item.pid == pid) {
            let port_detail = port
                .map(|value| format!("Port {value} was checked."))
                .unwrap_or_else(|| "No listener port was independently identified.".into());
            return Err(format!(
                "Backend PID {pid} still appears in the process scan after Stop. {port_detail} Process cleanup is not confirmed."
            ));
        }
        refresh_tray_menu(&app);
        Ok(scan)
    })();
    append_stop_trace("STOP COMMAND PHASE: existing stop logic returned; collecting final process and port state.");
    for line in stop_diagnostic_snapshot(&state, Some(pid), &diagnostic_ports, "AFTER") {
        append_stop_trace(line);
    }
    match &result {
        Ok(_) => append_stop_trace(format!(
            "FINAL RESULT: SUCCESS; verified backend process cleanup; port verification={}; elapsed_ms={}.",
            port.map(|value| format!("released {value}")).unwrap_or_else(|| "unavailable (no listener identified)".into()),
            started.elapsed().as_millis()
        )),
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

fn tray_location_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "menu bar"
    } else if cfg!(target_os = "windows") {
        "notification area"
    } else {
        "system tray"
    }
}

#[tauri::command]
fn get_tray_location_name() -> String {
    tray_location_name().to_string()
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
        return Err(format!(
            "The {} icon is not available in this session; the previous preference was restored.",
            tray_location_name()
        ));
    };
    if let Err(error) = tray.set_visible(enabled) {
        let _ = state.set_keep_in_menu_bar(previous);
        return Err(format!("Could not update the {} icon visibility: {error}. The previous preference was restored.", tray_location_name()));
    }
    Ok(())
}

#[tauri::command]
async fn start_backend(
    app: AppHandle,
    model: String,
    preferred_port: u16,
) -> Result<BackendStatus, String> {
    let worker_app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        start_backend_blocking(worker_app, model, preferred_port)
    })
    .await
    .map_err(|error| format!("Server startup worker failed: {error}"))?
}

const NORMAL_BACKEND_STARTUP_TIMEOUT: Duration = Duration::from_secs(180);

fn start_backend_blocking(
    app: AppHandle,
    model: String,
    preferred_port: u16,
) -> Result<BackendStatus, String> {
    // Model resolution and the real MLX warm-up inference happen before /health
    // is served. Allow a bounded first load without changing benchmark deadlines.
    start_backend_blocking_with_options(
        app,
        model,
        preferred_port,
        None,
        NORMAL_BACKEND_STARTUP_TIMEOUT,
    )
}

fn start_backend_blocking_with_options(
    app: AppHandle,
    model: String,
    preferred_port: u16,
    inference_device_override: Option<&str>,
    startup_timeout: Duration,
) -> Result<BackendStatus, String> {
    if preferred_port < 1024 {
        return Err("Choose a preferred port between 1024 and 65535.".into());
    }

    // A port probe is only a candidate selector, never proof that the port
    // remains available. The backend's actual bind is authoritative.
    let mut attempted_ports = Vec::with_capacity(3);
    attempted_ports.push(preferred_port);

    for attempt in 1..=3 {
        let port = if attempt == 1 {
            preferred_port
        } else {
            let mut selected = None;
            for _ in 0..128 {
                let candidate = if attempt == 2 {
                    native_benchmark_ephemeral_loopback_port()?
                } else {
                    native_benchmark_random_five_digit_loopback_port()?
                };
                if !attempted_ports.contains(&candidate) {
                    selected = Some(candidate);
                    break;
                }
            }
            selected.ok_or_else(|| {
                format!(
                    "Could not select a distinct loopback port for startup attempt {attempt}/3."
                )
            })?
        };

        attempted_ports.push(port);
        eprintln!("[RDT Connect] normal backend startup attempt {attempt}/3 on 127.0.0.1:{port}");

        let result = start_backend_blocking_with_options_once(
            app.clone(),
            app.state::<BackendState>(),
            model.clone(),
            port,
            inference_device_override,
            startup_timeout,
        );

        match result {
            Ok(status) => {
                if status.port != Some(port) {
                    return Err(format!(
                        "Backend endpoint mismatch: requested 127.0.0.1:{port}, but manager reported {:?}.",
                        status.port
                    ));
                }
                return Ok(status);
            }
            Err(error) if native_benchmark_is_address_in_use_error(&error) && attempt < 3 => {
                eprintln!(
                    "[RDT Connect] actual backend bind collision on attempt {attempt}/3 at 127.0.0.1:{port}; retrying with a different port: {error}"
                );
            }
            Err(error) if native_benchmark_is_address_in_use_error(&error) => {
                return Err(format!(
                    "The backend could not bind after three actual startup attempts on distinct loopback ports {:?}. No further retry was attempted. Last error: {error}",
                    attempted_ports
                ));
            }
            Err(error) => return Err(error),
        }
    }

    Err("Backend startup ended without a result after three attempts.".into())
}

fn start_backend_blocking_with_options_once(
    app: AppHandle,
    state: State<'_, BackendState>,
    model: String,
    preferred_port: u16,
    inference_device_override: Option<&str>,
    startup_timeout: Duration,
) -> Result<BackendStatus, String> {
    // Keep this lock from the initial running check until the backend is registered or startup fails.
    // A second Start waits, then sees the first server instead of spawning another process.
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    if inference_device_override.is_none()
        && (NATIVE_BENCHMARK_STARTING.load(Ordering::SeqCst)
            || NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst))
    {
        return Err("The local server cannot be started separately while hardware optimization owns the backend lifecycle.".into());
    }
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
    if use_https && cfg!(not(any(target_os = "macos", windows, target_os = "linux"))) {
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

    // Do not preflight-bind and release this port. The packaged backend's
    // actual Uvicorn bind is the authoritative availability check.
    let port = preferred_port;
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
        .env(
            "RDT_CONNECT_STATS_PATH",
            state
                .app_language_pref_path
                .with_file_name("lifetime-statistics.json"),
        )
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

    // MLX device selection is macOS-only. Windows/Linux benchmark candidates
    // select the shared whisper.cpp worker category without changing normal fallback.
    command
        .env_remove("RDT_MLX_DEVICE")
        .env_remove("RDT_WHISPER_WORKER_VARIANT");
    let mut applied_inference_device_preference: Option<String> = None;
    if cfg!(target_os = "macos") {
        let device = inference_device_override
            .map(str::to_string)
            .unwrap_or_else(|| read_effective_inference_device_preference(state.inner(), &model));
        command.env("RDT_MLX_DEVICE", device.clone());
        applied_inference_device_preference = Some(device);
    } else if let Some(worker_variant) = inference_device_override {
        command.env("RDT_WHISPER_WORKER_VARIANT", worker_variant);
    }
    #[cfg(not(windows))]
    {
        let parent_exe = std::env::current_exe().map_err(|e| {
            format!("Could not determine RDT Connect executable for backend supervision: {e}")
        })?;
        command
            .env("RDT_CONNECT_PARENT_PID", std::process::id().to_string())
            .env("RDT_CONNECT_PARENT_EXE", parent_exe);
    }
    #[cfg(any(windows, target_os = "linux"))]
    command.env("RDT_GGML_MODEL_DIR", model_storage_root.join("ggml"));
    command
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr_log));
    #[cfg(unix)]
    command.process_group(0);

    let mut child = command
        .spawn()
        .map_err(|e| format!("Could not launch the packaged Whisper backend: {e}"))?;
    let process_group = child.id();
    #[cfg(target_os = "macos")]
    let candidate_identity = if inference_device_override.is_some() {
        match capture_candidate_process_identity(process_group) {
            Ok(identity) => Some(identity),
            Err(error) => {
                // The Child handle still identifies the exact spawned process.
                // Signal its group only while that child is alive and the OS
                // confirms that it remains the leader of its isolated group.
                let child_alive = matches!(child.try_wait(), Ok(None));
                let group_is_owned = child_alive
                    && unsafe {
                        libc::getpgid(process_group as libc::pid_t) == process_group as libc::pid_t
                    };
                let stopped = if group_is_owned {
                    terminate_process_group(process_group, Some(&mut child))
                } else {
                    // Never signal an unverified/reused PGID. Killing and waiting
                    // for the exact Child is safe, but descendants remain
                    // unconfirmed, so report cleanup as unconfirmed.
                    let already_exited_cleanly = matches!(child.try_wait(), Ok(Some(_)))
                        && !process_group_alive(process_group);
                    if !already_exited_cleanly {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    already_exited_cleanly
                };
                return Err(format!(
                    "Could not establish candidate process ownership: {error} Candidate cleanup {}.",
                    if stopped { "completed" } else { "could not be confirmed" }
                ));
            }
        }
    } else {
        None
    };
    let deadline = Instant::now() + startup_timeout;
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
        #[cfg(target_os = "macos")]
        let backend_ready = if let Some(identity) = candidate_identity.as_ref() {
            match candidate_listener_ownership_verified(port, identity) {
                Ok(true) => healthy(port, &tls_config),
                Ok(false) => false,
                Err(error) => {
                    terminate_process_group(process_group, Some(&mut child));
                    return Err(error);
                }
            }
        } else {
            healthy(port, &tls_config)
        };
        #[cfg(not(target_os = "macos"))]
        let backend_ready = healthy(port, &tls_config);

        if backend_ready {
            is_ready = true;
            break;
        }
        thread::sleep(Duration::from_millis(300));
    }
    if !is_ready {
        terminate_process_group(process_group, Some(&mut child));
        let details = safe_tail_log(&state.log_path, 24);
        return Err(format!(
            "The backend did not become healthy within {} seconds.\n{}",
            startup_timeout.as_secs(),
            if details.is_empty() {
                "Open the backend log for details."
            } else {
                &details
            }
        ));
    }

    if inference_device_override.is_none() {
        let recent_choice = LastServerChoice {
            model: model.clone(),
            preferred_port: port,
        };
        if let Err(error) = save_last_server_choice(&state, &recent_choice) {
            eprintln!("[RDT Connect] Could not persist the recent server choice: {error}");
        }
    }
    {
        let mut inner = state.inner.lock().unwrap_or_else(|p| p.into_inner());
        inner.child = Some(child);
        inner.applied_inference_device_preference = applied_inference_device_preference;
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
            (
                HKEY_CURRENT_USER,
                format!(r"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"),
            ),
            (
                HKEY_LOCAL_MACHINE,
                format!(r"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"),
            ),
            (
                HKEY_LOCAL_MACHINE,
                format!(r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe}"),
            ),
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
            add(
                &program_files,
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            );
            add(
                &program_files_x86,
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            );
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
                add(
                    &local,
                    &format!(r"Programs\Mullvad Browser\Browser\{executable}"),
                );
                add(
                    &program_files,
                    &format!(r"Mullvad Browser\Browser\{executable}"),
                );
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
    let parsed =
        url::Url::parse(url).map_err(|error| format!("The external URL is invalid: {error}"))?;
    let allowed = match parsed.scheme() {
        "https" => matches!(
            parsed.host_str(),
            Some("rolandoristech.github.io" | "github.com" | "youtube.com" | "www.youtube.com")
        ),
        "mailto" => parsed.path() == "woknewsbusiness@gmail.com",
        _ => false,
    };
    if !allowed {
        return Err(
            "RDT Connect refused to open an external URL outside its approved destinations.".into(),
        );
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
                .map_err(|error| {
                    format!("Could not open the URL in the selected browser: {error}")
                })?;
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
            std::ptr::null_mut(),
            operation.as_ptr(),
            target.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1,
        )
    };
    if result <= 32 {
        return Err(format!("Windows could not open the URL with the registered default browser (ShellExecuteW={result})."));
    }
    Ok(())
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

fn read_preferred_browser(state: &BackendState) -> Option<String> {
    let saved = fs::read_to_string(&state.preferred_browser_path).ok()?;
    let saved = saved.trim();

    if list_available_browsers()
        .iter()
        .any(|browser| browser.id.as_str() == saved)
    {
        Some(saved.to_string())
    } else {
        None
    }
}

#[tauri::command]
fn get_preferred_browser(state: State<'_, BackendState>) -> Option<String> {
    read_preferred_browser(&state)
}

#[tauri::command]
fn set_preferred_browser(state: State<'_, BackendState>, browser_id: String) -> Result<(), String> {
    if !list_available_browsers()
        .iter()
        .any(|browser| browser.id == browser_id)
    {
        return Err("The selected browser is not currently available.".into());
    }

    write_private_atomic(&state.preferred_browser_path, browser_id.as_bytes())
}

fn read_offline_only_mode(state: &BackendState) -> bool {
    fs::read_to_string(&state.offline_only_mode_pref_path)
        .map(|value| value.trim().eq_ignore_ascii_case("true"))
        .unwrap_or(false)
}

#[tauri::command]
fn set_offline_only_mode(state: State<'_, BackendState>, enabled: bool) -> Result<(), String> {
    let value: &[u8] = if enabled { &b"true"[..] } else { &b"false"[..] };
    write_private_atomic(&state.offline_only_mode_pref_path, value)
}

#[tauri::command]
fn open_voice_window(
    app: AppHandle,
    state: State<'_, BackendState>,
    browser_id: Option<String>,
    locale: Option<String>,
    offline_only_mode: Option<bool>,
) -> Result<bool, String> {
    let browser_id = browser_id.or_else(|| read_preferred_browser(&state));
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
    let force_offline = offline_only_mode.unwrap_or_else(|| read_offline_only_mode(&state));

    // Diagnostic-only cross-origin Alpha test. Release builds ignore this.
    #[cfg(debug_assertions)]
    let performance_test_origin = if force_offline {
        None
    } else {
        std::env::var("RDT_VOICE_PERF_TEST_ORIGIN")
            .ok()
            .and_then(|raw| {
                let mut parsed = url::Url::parse(raw.trim()).ok()?;
                let host = parsed.host_str()?;
                let loopback = matches!(host, "localhost" | "127.0.0.1" | "[::1]" | "::1");
                if parsed.scheme() != "http" || !loopback || parsed.port().is_none() {
                    return None;
                }
                parsed.set_path("/");
                parsed.set_query(None);
                parsed.set_fragment(None);
                Some(parsed)
            })
    };
    #[cfg(not(debug_assertions))]
    let performance_test_origin: Option<url::Url> = None;
    #[cfg(debug_assertions)]
    let using_performance_test_origin = performance_test_origin.is_some();
    #[cfg(debug_assertions)]
    let performance_diagnostics_enabled = using_performance_test_origin
        || std::env::var("RDT_VOICE_PERF_DIAGNOSTICS").as_deref() == Ok("1");
    #[cfg(not(debug_assertions))]
    let performance_diagnostics_enabled = false;

    let online_available = performance_test_origin.is_none()
        && !force_offline
        && Client::builder()
            .connect_timeout(Duration::from_millis(500))
            .timeout(Duration::from_secs(1))
            .build()
            .ok()
            .and_then(|client| {
                client
                    .get("https://rolandoristech.github.io/rdt-voice/")
                    .send()
                    .ok()
            })
            .map(|response| {
                response.status().is_success()
                    && response.url().host_str() == Some("rolandoristech.github.io")
                    && response.url().path().starts_with("/rdt-voice/")
            })
            .unwrap_or(false);
    let using_offline_copy = !online_available;

    // Prefer the hosted RDT Voice frontend when reachable. Offline mode uses
    // the bundled frontend on the authenticated local backend origin.
    // The URL fragment contains only the one-time handoff code and backend URL;
    // the bearer token is never exposed to the browser URL.
    let mut launch_url = if let Some(origin) = performance_test_origin {
        origin
    } else if online_available {
        url::Url::parse("https://rolandoristech.github.io/rdt-voice/")
            .map_err(|e| format!("The hosted RDT Voice URL is invalid: {e}"))?
    } else {
        let mut local = url::Url::parse(&backend_url)
            .map_err(|e| format!("The local RDT Voice endpoint is invalid: {e}"))?;
        local.set_path("/");
        local.set_query(None);
        local.set_fragment(None);
        local
    };
    launch_url.set_query(None);
    launch_url.set_fragment(None);
    {
        let mut query = launch_url.query_pairs_mut();
        if performance_diagnostics_enabled {
            query.append_pair("rdtPerf", "1");
        }
        query.append_pair("rdt_connect_lang", &active_locale);
    }
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
        command
            .arg(launch_url.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Could not open RDT Voice in the selected browser: {e}"))?;
    }
    #[cfg(target_os = "windows")]
    {
        open_url_windows(launch_url.as_str(), browser_id.as_deref())?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = browser_id;
        Command::new("xdg-open")
            .arg(launch_url.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Could not open RDT Voice in the default browser: {e}"))?;
    }
    Ok(using_offline_copy)
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
    api_request_with_timeout(state, method, path, body, Duration::from_secs(12))
}

fn api_request_with_timeout(
    state: &BackendState,
    method: &str,
    path: &str,
    body: Option<Value>,
    timeout: Duration,
) -> Result<Value, String> {
    let (_port, token) = state.api_credentials()?;
    let status = state.status();
    let origin = status
        .local_url
        .ok_or_else(|| "The local server has no active endpoint.".to_string())?;
    let url = format!("{origin}{path}");
    let tls_config = load_local_tls_config(state)?;
    let client = local_tls_client(&tls_config, timeout)?;
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
    let response = api_request_with_timeout(
        &state,
        "POST",
        "/auth/tailscale/setup",
        Some(json!({})),
        Duration::from_secs(45),
    )?;
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

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct FireworksActivationResult {
    first_this_session: bool,
    lifetime_count: u64,
}

#[tauri::command]
fn record_fireworks_activation(
    state: State<'_, BackendState>,
) -> Result<FireworksActivationResult, String> {
    let _guard = FIREWORKS_COUNTER_LOCK
        .lock()
        .unwrap_or_else(|p| p.into_inner());

    let current = match fs::read_to_string(&state.fireworks_lifetime_count_path) {
        Ok(value) => value
            .trim()
            .parse::<u64>()
            .map_err(|error| format!("The saved fireworks counter is invalid: {error}"))?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => {
            return Err(format!("Could not read the fireworks counter: {error}"));
        }
    };

    let lifetime_count = current
        .checked_add(1)
        .ok_or_else(|| "The fireworks lifetime counter is full.".to_string())?;

    write_private_atomic(
        &state.fireworks_lifetime_count_path,
        lifetime_count.to_string().as_bytes(),
    )?;

    let first_this_session = !FIREWORKS_SESSION_SEEN.swap(true, Ordering::SeqCst);

    Ok(FireworksActivationResult {
        first_this_session,
        lifetime_count,
    })
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

fn tray_model_display_name(locale: &str, model: &str) -> String {
    let leaf = model.rsplit('/').next().unwrap_or(model).trim();
    let name = leaf.strip_prefix("whisper-").unwrap_or(leaf);
    let name = name.strip_suffix("-mlx").unwrap_or(name);
    match name {
        "tiny" => tray_text(locale, "msg.whisper_tiny.bd612f06", "Whisper Tiny", &[]),
        "base" => tray_text(locale, "msg.whisper_base.69e01c4a", "Whisper Base", &[]),
        "small" => tray_text(locale, "msg.whisper_small.274ca4bf", "Whisper Small", &[]),
        "large-v3" => tray_text(
            locale,
            "msg.whisper_large_v3.332fb9ac",
            "Whisper Large v3",
            &[],
        ),
        "large-v3-turbo" => tray_text(
            locale,
            "msg.whisper_large_v3_turbo.ba2e4b36",
            "Whisper Large v3 Turbo",
            &[],
        ),
        _ => {
            let words = name.replace('-', " ");
            let label = format!("Whisper {words}");
            if label.chars().count() > 40 {
                format!("{}…", label.chars().take(39).collect::<String>())
            } else {
                label
            }
        }
    }
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
        let model = status
            .model
            .as_deref()
            .map(|model| tray_model_display_name(&locale, model))
            .unwrap_or_else(|| "Model unavailable".to_string());
        format!(
            "🟢 {} · 127.0.0.1:{} · {}",
            tray_text(&locale, "msg.stop_server.a21bf605", "Stop server", &[]),
            port,
            model,
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
    let update_state = UPDATE_TRAY_STATE.load(Ordering::SeqCst);
    let (update_label_key, update_label_fallback) = match update_state {
        UPDATE_TRAY_CHECKING => ("updater.checking", "Checking for Updates…"),
        UPDATE_TRAY_AVAILABLE => ("updater.downloadInstall", "Download Update"),
        UPDATE_TRAY_DOWNLOADING => ("updater.downloading", "Downloading Update…"),
        UPDATE_TRAY_RESTART => ("updater.reopenApp", "Restart to Update"),
        UPDATE_TRAY_UP_TO_DATE => ("updater.upToDate", "Up to Date"),
        UPDATE_TRAY_UP_TO_DATE_AUTO => ("updater.upToDate", "RDT Connect is up to date"),
        UPDATE_TRAY_UP_TO_DATE_MANUAL => ("msg.check_for_updates.f26f3275", "Check for Update"),
        UPDATE_TRAY_ERROR => ("updater.checkFailed", "Update Check Failed"),
        UPDATE_TRAY_OFFLINE => (
            "updater.offline",
            "No internet connection. Try again when online.",
        ),
        _ => ("msg.check_for_updates.f26f3275", "Check for Updates"),
    };
    // Keep the tray entry actionable after a check. Detailed results remain
    // visible in the update window; they must not replace the manual-check action.
    let (update_label_key, update_label_fallback) = if matches!(
        update_state,
        UPDATE_TRAY_IDLE
            | UPDATE_TRAY_UP_TO_DATE
            | UPDATE_TRAY_UP_TO_DATE_MANUAL
            | UPDATE_TRAY_ERROR
            | UPDATE_TRAY_OFFLINE
    ) {
        ("msg.check_for_updates.f26f3275", "Check for Updates")
    } else {
        (update_label_key, update_label_fallback)
    };
    let update_label = match update_state {
        UPDATE_TRAY_UP_TO_DATE_AUTO => "RDT Connect is up to date".to_string(),
        UPDATE_TRAY_UP_TO_DATE_MANUAL => tray_text(
            &locale,
            "msg.check_for_updates.f26f3275",
            "Check for Updates",
            &[],
        ),
        _ => tray_text(&locale, update_label_key, update_label_fallback, &[]),
    };
    let update_enabled = !matches!(update_state, UPDATE_TRAY_CHECKING | UPDATE_TRAY_DOWNLOADING);
    let update_menu_item = MenuItem::with_id(
        app,
        "update-app",
        update_label,
        update_enabled,
        None::<&str>,
    )?;
    if let Ok(mut item) = UPDATE_TRAY_MENU_ITEM
        .get_or_init(|| Mutex::new(None))
        .lock()
    {
        *item = Some(update_menu_item.clone());
    }
    #[cfg(target_os = "macos")]
    let about_label = tray_text(
        &locale,
        "msg.about_rdt_connect.1d22",
        "About RDT Connect",
        &[],
    );
    let about_item = MenuItem::with_id(app, "tray-about-app", &about_label, true, None::<&str>)?;

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
    items.push(&update_menu_item);
    #[cfg(target_os = "macos")]
    items.push(&about_item);
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
fn raise_webview_window_above_other_apps(window: &tauri::WebviewWindow) -> tauri::Result<()> {
    window.with_webview(|webview| {
        use objc2::MainThreadMarker;
        use objc2_app_kit::{NSApplication, NSModalPanelWindowLevel, NSWindow};

        let Some(mtm) = MainThreadMarker::new() else {
            eprintln!("[RDT Connect] Could not obtain the macOS main-thread marker");
            return;
        };
        NSApplication::sharedApplication(mtm).activate();
        unsafe {
            let native_window: &NSWindow = &*webview.ns_window().cast();
            native_window.setLevel(NSModalPanelWindowLevel);
            native_window.makeKeyAndOrderFront(None);
            native_window.orderFrontRegardless();
        }
    })?;
    Ok(())
}

#[cfg(target_os = "macos")]
fn show_about_panel_on_top() {
    use objc2::{rc::Retained, runtime::AnyObject, AnyThread, MainThreadMarker};
    use objc2_app_kit::{
        NSAboutPanelOptionApplicationIcon, NSAboutPanelOptionApplicationName,
        NSAboutPanelOptionApplicationVersion, NSApp, NSImage, NSModalPanelWindowLevel,
    };
    use objc2_foundation::{ns_string, NSData, NSDictionary, NSString};

    let Some(mtm) = MainThreadMarker::new() else {
        eprintln!("[RDT Connect] About panel requires the macOS main thread");
        return;
    };

    let mut keys: Vec<&NSString> = Vec::new();
    let mut objects: Vec<Retained<AnyObject>> = Vec::new();

    keys.push(unsafe { NSAboutPanelOptionApplicationName });
    objects.push(Retained::into_super(Retained::into_super(
        NSString::from_str("RDT Connect"),
    )));

    keys.push(unsafe { NSAboutPanelOptionApplicationVersion });
    objects.push(Retained::into_super(Retained::into_super(
        NSString::from_str(env!("CARGO_PKG_VERSION")),
    )));

    keys.push(ns_string!("Copyright"));
    objects.push(Retained::into_super(Retained::into_super(
        NSString::from_str("© 2026 Rolan & Doris Tech"),
    )));
    let icon_data = NSData::with_bytes(include_bytes!("../icons/icon.png"));
    if let Some(icon) = NSImage::initWithData(NSImage::alloc(), &icon_data) {
        keys.push(unsafe { NSAboutPanelOptionApplicationIcon });
        objects.push(Retained::into_super(Retained::into_super(icon)));
    }

    let options = NSDictionary::from_retained_objects(&keys, &objects);
    let application = NSApp(mtm);

    // A tray click may arrive while another app owns the active window.
    // Activate RDT Connect before ordering its standard About panel forward.
    application.activate();
    unsafe {
        application.orderFrontStandardAboutPanelWithOptions(&options);
    }

    // Locate the actual standard About panel; keyWindow() may refer to another window.
    let windows = application.windows();
    let about_title = NSString::from_str("About RDT Connect");
    for index in 0..windows.count() {
        let window = windows.objectAtIndex(index);
        if window.title().isEqualToString(&about_title) {
            window.setLevel(NSModalPanelWindowLevel);
            window.makeKeyAndOrderFront(None);
            window.orderFrontRegardless();
        }
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

fn show_update_window(app: &AppHandle, recheck: bool) -> tauri::Result<()> {
    if let Some(window) = app.get_webview_window("updates") {
        window.set_always_on_top(true)?;
        window.show()?;
        window.set_focus()?;
        #[cfg(target_os = "macos")]
        raise_webview_window_above_other_apps(&window)?;
        let event = if recheck {
            "rdt-connect-update-check-request"
        } else {
            "rdt-connect-update-window-state-request"
        };
        let _ = app.emit(event, ());
        return Ok(());
    }

    let window = WebviewWindowBuilder::new(app, "updates", WebviewUrl::App("update.html".into()))
        .title("RDT Connect Updates")
        .always_on_top(true)
        .inner_size(440.0, 230.0)
        .min_inner_size(360.0, 190.0)
        .resizable(false)
        .center()
        .build()?;
    #[cfg(target_os = "macos")]
    raise_webview_window_above_other_apps(&window)?;
    #[cfg(not(target_os = "macos"))]
    let _ = window;

    if recheck {
        let _ = app.emit("rdt-connect-update-check-request", ());
    }
    Ok(())
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
fn single_instance_lock_path() -> Result<std::path::PathBuf, String> {
    let home = std::env::var_os("HOME")
        .ok_or_else(|| "HOME is unavailable; cannot locate the single-instance lock".to_string())?;
    let support_name = if option_env!("RDT_CONNECT_LOCALE_TEST").is_some() {
        "RDT Connect Locale Test"
    } else {
        "RDT Connect"
    };
    Ok(std::path::PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join(support_name)
        .join("rdt-connect-single-instance.lock"))
}

#[cfg(target_os = "macos")]
fn acquire_single_instance_lock() -> Result<Option<std::fs::File>, String> {
    use fs2::FileExt;
    use std::io::Write;

    let lock_path = single_instance_lock_path()?;
    let support_dir = lock_path
        .parent()
        .ok_or_else(|| "single-instance lock path has no parent directory".to_string())?;
    std::fs::create_dir_all(support_dir)
        .map_err(|e| format!("cannot create the single-instance support directory: {e}"))?;
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
    let path = single_instance_lock_path().ok();
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
fn open_windows_installation_guide(app: AppHandle) -> Result<(), String> {
    #[cfg(not(target_os = "windows"))]
    {
        let _ = app;
        Err("The Windows installation guide is only available in the Windows app.".into())
    }
    #[cfg(target_os = "windows")]
    {
        let resource_dir = app
            .path()
            .resource_dir()
            .map_err(|error| format!("Could not locate the app resource directory: {error}"))?;
        let guide = resource_dir
            .join("windows_installation_guide")
            .join("windows_installation_guide.html");

        if !guide.is_file() {
            return Err(format!(
                "The packaged Windows installation guide is missing: {}. Repair or reinstall RDT Connect.",
                guide.display()
            ));
        }

        use std::ffi::c_void;

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
        let target: Vec<u16> = guide
            .to_string_lossy()
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();

        let result = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                operation.as_ptr(),
                target.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                1,
            )
        };

        if result <= 32 {
            return Err(format!(
                "Windows could not open the installation guide (ShellExecuteW={result})."
            ));
        }
        Ok(())
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
}

fn native_benchmark_edit_distance<T: Eq>(left: &[T], right: &[T]) -> usize {
    let (left, right) = if left.len() < right.len() {
        (right, left)
    } else {
        (left, right)
    };
    let mut previous: Vec<usize> = (0..=right.len()).collect();
    for (i, a) in left.iter().enumerate() {
        let mut current = Vec::with_capacity(right.len() + 1);
        current.push(i + 1);
        for (j, b) in right.iter().enumerate() {
            let substitution_cost = if a == b { 0 } else { 1 };
            current.push(
                (current[j] + 1)
                    .min(previous[j + 1] + 1)
                    .min(previous[j] + substitution_cost),
            );
        }
        previous = current;
    }
    previous[right.len()]
}

fn native_benchmark_accuracy(transcript: &str, reference: &str) -> Value {
    use unicode_casefold::UnicodeCaseFold;
    use unicode_categories::UnicodeCategories;
    use unicode_normalization::UnicodeNormalization;

    let fold = |text: &str| -> String { text.nfc().case_fold().collect::<String>() };
    let normalized_reference = fold(reference);
    let normalized_transcript = fold(transcript);

    let clean_chars = |value: &str, strict: bool| -> Vec<char> {
        value
            .chars()
            .filter(|character| {
                !character.is_whitespace() && (strict || !character.is_punctuation())
            })
            .collect()
    };

    let ref_normalized = clean_chars(&normalized_reference, false);
    let out_normalized = clean_chars(&normalized_transcript, false);
    let ref_strict = clean_chars(&normalized_reference, true);
    let out_strict = clean_chars(&normalized_transcript, true);

    let normalized_cer = if ref_normalized.is_empty() {
        Value::Null
    } else {
        json!(
            native_benchmark_edit_distance(&ref_normalized, &out_normalized) as f64
                / ref_normalized.len() as f64
        )
    };
    let strict_cer = if ref_strict.is_empty() {
        Value::Null
    } else {
        json!(
            native_benchmark_edit_distance(&ref_strict, &out_strict) as f64
                / ref_strict.len() as f64
        )
    };

    let has_cjk = reference.chars().any(|c| {
        matches!(c as u32,
            0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF
            | 0x3040..=0x30FF | 0x31F0..=0x31FF
            | 0xAC00..=0xD7AF | 0x1100..=0x11FF
            | 0x3130..=0x318F | 0xA960..=0xA97F | 0xD7B0..=0xD7FF
            | 0x20000..=0x2A6DF | 0x2A700..=0x2B73F | 0x2B740..=0x2B81F
            | 0x2B820..=0x2CEAF | 0x2CEB0..=0x2EBEF | 0x2EBF0..=0x2EE5F
            | 0x2F800..=0x2FA1F | 0x30000..=0x3134F | 0x31350..=0x323AF
        )
    });

    let words = |text: &str| -> Vec<String> {
        let folded = fold(text);
        let mut result = Vec::new();
        let mut current = String::new();
        for character in folded.chars() {
            if character.is_alphanumeric() || character == '_' {
                current.push(character);
            } else if !current.is_empty() {
                result.push(std::mem::take(&mut current));
            }
        }
        if !current.is_empty() {
            result.push(current);
        }
        result
    };

    let wer = if has_cjk {
        Value::Null
    } else {
        let reference_words = words(reference);
        let transcript_words = words(transcript);
        if reference_words.is_empty() {
            Value::Null
        } else {
            json!(
                native_benchmark_edit_distance(&reference_words, &transcript_words) as f64
                    / reference_words.len() as f64
            )
        }
    };

    json!({
        "cer": normalized_cer,
        "normalized_cer": normalized_cer,
        "strict_cer": strict_cer,
        "wer": wer
    })
}

#[cfg(test)]
fn native_benchmark_utc_timestamp() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    if month <= 2 {
        year += 1;
    }
    let hour = day_seconds / 3_600;
    let minute = (day_seconds % 3_600) / 60;
    let second = day_seconds % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

#[tauri::command]
fn is_hardware_optimization_supported() -> bool {
    cfg!(all(target_os = "macos", target_arch = "aarch64"))
}

#[tauri::command]
fn is_first_run_hardware_optimization_required() -> bool {
    // This 1D12 build has a qualified Apple-Silicon MLX manual comparison only.
    // Its integrated Apple GPU/CPU are not an additional accelerator pair, so
    // they do not trigger a mandatory first-run comparison. Non-Apple provider
    // adapters must register verified, eligible candidates before returning true.
    false
}

// Benchmark-only listener selection. Port 0 asks the OS for an ephemeral
// loopback port; the short reservation is released before the child binds.
// A startup collision is handled by the bounded retry in the command below.
fn native_benchmark_ephemeral_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("Could not obtain an OS-assigned loopback port: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("Could not read the OS-assigned loopback port: {error}"))?
        .port();
    drop(listener);
    Ok(port)
}

fn native_benchmark_random_five_digit_loopback_port() -> Result<u16, String> {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hash, Hasher};
    use std::time::{SystemTime, UNIX_EPOCH};
    const MIN_PORT: u64 = 10_000;
    const PORT_COUNT: u64 = 65_536 - MIN_PORT;
    let random_state = RandomState::new();
    for selection_attempt in 0..128u64 {
        let mut hasher = random_state.build_hasher();
        (
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
            std::process::id(),
            selection_attempt,
        )
            .hash(&mut hasher);
        let candidate_port = (MIN_PORT + hasher.finish() % PORT_COUNT) as u16;
        match TcpListener::bind(("127.0.0.1", candidate_port)) {
            Ok(listener) => {
                let selected_port = listener.local_addr()
                    .map_err(|error| format!("Could not verify randomized five-digit loopback port {candidate_port}: {error}"))?
                    .port();
                drop(listener);
                return Ok(selected_port);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => continue,
            Err(error) => {
                return Err(format!(
                    "Could not check randomized five-digit loopback port {candidate_port}: {error}"
                ))
            }
        }
    }
    Err("Could not find an available randomized five-digit loopback port after 128 checked candidates.".into())
}

fn native_benchmark_is_address_in_use_error(message: &str) -> bool {
    let value = message.to_ascii_lowercase();
    [
        "address already in use",
        "eaddrinuse",
        "errno 48",
        "errno 98",
        "os error 48",
        "os error 98",
        "10048",
    ]
    .iter()
    .any(|needle| value.contains(needle))
}

#[tauri::command]
async fn start_hardware_candidate_backend(
    app: AppHandle,
    model: String,
    preferred_port: u16,
    device: String,
    startup_timeout_seconds: u64,
) -> Result<BackendStatus, String> {
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        return Err("The MLX hardware comparison is available only on Apple Silicon Macs.".into());
    }
    if !NATIVE_BENCHMARK_STARTING.load(Ordering::SeqCst)
        || NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst)
    {
        return Err(
            "Hardware candidate startup is allowed only inside the guarded optimization flow."
                .into(),
        );
    }

    let device = match device.as_str() {
        "gpu" => "gpu",
        "cpu" => "cpu",
        _ => return Err("Hardware candidate must be gpu or cpu.".into()),
    };
    let timeout = Duration::from_secs(startup_timeout_seconds.clamp(1, 90));

    // Keep the user's normal port as the restoration target in the frontend.
    // It is deliberately NOT used by any temporary benchmark candidate.
    let normal_server_port_restore_target = preferred_port;
    eprintln!(
        "[RDT Benchmark] isolated candidate startup; normal server restore port={}",
        normal_server_port_restore_target
    );

    for attempt in 1..=3 {
        let candidate_port = if attempt == 1 {
            native_benchmark_ephemeral_loopback_port()?
        } else {
            native_benchmark_random_five_digit_loopback_port()?
        };
        let state_app = app.clone();
        let worker_app = app.clone();
        let attempt_model = model.clone();

        eprintln!(
            "[RDT Benchmark] candidate attempt {}/3 using 127.0.0.1:{}",
            attempt, candidate_port
        );

        let startup_result = tauri::async_runtime::spawn_blocking(move || {
            let state = state_app.state::<BackendState>();

            // The original server must already have been stopped and verified.
            // Never take over a running manager or signal an unrelated process.
            if state.status().running {
                return Err(
                    "The normal backend is still running; refusing to replace it with a benchmark candidate.".into()
                );
            }

            let result = start_backend_blocking_with_options(
        worker_app.clone(),
        attempt_model,
        candidate_port,
        Some(device),
        timeout
    );

            // The startup helper consumes the State handle. Reacquire it from
            // this same AppHandle before inspecting or stopping the owned child.
            // This never scans for, signals, or kills unrelated processes.
            match result {
                Err(error) => {
                    let cleanup_state = worker_app.state::<BackendState>();
                    if cleanup_state.status().running {
                        if let Err(cleanup_error) = cleanup_state.stop() {
                            return Err(format!(
                                "{error}; candidate cleanup also failed: {cleanup_error}"
                            ));
                        }
                    }
                    Err(error)
                }
                success => success,
            }
        })
        .await
        .map_err(|error| format!("Hardware candidate startup worker failed: {error}"))?;

        match startup_result {
            Ok(status) => {
                if status.port != Some(candidate_port) {
                    // The returned manager status must agree with the endpoint
                    // actually selected for this candidate.
                    let state_app = app.clone();
                    let cleanup = tauri::async_runtime::spawn_blocking(move || {
                        let state = state_app.state::<BackendState>();
                        if state.status().running {
                            state.stop().map(|_| ())
                        } else {
                            Ok(())
                        }
                    })
                    .await
                    .map_err(|error| {
                        format!("Candidate identity cleanup worker failed: {error}")
                    })?;
                    cleanup.map_err(|error| {
                        format!("Candidate endpoint mismatch; cleanup failed: {error}")
                    })?;
                    return Err(format!(
                        "Candidate endpoint mismatch: requested ephemeral port {}, backend reported {:?}.",
                        candidate_port, status.port
                    ));
                }
                return Ok(status);
            }
            Err(error) if native_benchmark_is_address_in_use_error(&error) => {
                eprintln!(
                    "[RDT Benchmark] loopback port collision on attempt {}/3; candidate cleanup completed: {}",
                    attempt, error
                );
                if attempt == 3 {
                    return Err(format!(
                        "Could not start the isolated benchmark backend after three checked loopback-port selections (OS-assigned ephemeral first, randomized five-digit fallbacks thereafter). The normal server was not touched. Last error: {error}"
                    ));
                }
                // Retry only for an actual address-in-use failure.
            }
            Err(error) => return Err(error),
        }
    }

    Err("Benchmark candidate startup exhausted its three port attempts.".into())
}

#[tauri::command]
async fn stop_hardware_candidate_backend(app: AppHandle) -> Result<BackendStatus, String> {
    if !NATIVE_BENCHMARK_STARTING.load(Ordering::SeqCst)
        || NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst)
    {
        return Err(
            "Candidate shutdown is allowed only between guarded optimization steps.".into(),
        );
    }
    let state_app = app.clone();
    let worker_app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = state_app.state::<BackendState>();
        let status = state.stop()?;
        refresh_tray_menu(&worker_app);
        Ok(status)
    })
    .await
    .map_err(|error| format!("Hardware candidate shutdown worker failed: {error}"))?
}

// Native benchmark runs exclusively against the already-running local backend.
// Fixture files are loaded from the installed Tauri Resources/benchmark folder.
static NATIVE_BENCHMARK_RUNNING: AtomicBool = AtomicBool::new(false);
static NATIVE_BENCHMARK_STARTING: AtomicBool = AtomicBool::new(false);
static NATIVE_BENCHMARK_CANCEL_REQUESTED: AtomicBool = AtomicBool::new(false);
// Serializes the start/reset transition with cancellation requests so a cancel
// arriving as a native worker starts cannot be overwritten.
static NATIVE_BENCHMARK_CONTROL_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
static NATIVE_BENCHMARK_DIAGNOSTICS_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

struct NativeBenchmarkRunGuard;

impl Drop for NativeBenchmarkRunGuard {
    fn drop(&mut self) {
        NATIVE_BENCHMARK_CANCEL_REQUESTED.store(false, Ordering::SeqCst);
        NATIVE_BENCHMARK_RUNNING.store(false, Ordering::SeqCst);
    }
}

fn native_benchmark_response(
    response: reqwest::blocking::Response,
    operation: &str,
) -> Result<Value, String> {
    let status = response.status();
    let body = response
        .text()
        .map_err(|error| format!("Could not read {operation} response: {error}"))?;
    let value: Value = serde_json::from_str(&body).unwrap_or(Value::Null);

    if !status.is_success() {
        let detail = value
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or_else(|| body.trim());
        return Err(format!(
            "{operation} failed (HTTP {}): {}",
            status.as_u16(),
            detail
        ));
    }
    Ok(value)
}

fn native_benchmark_normalize_words(value: &str) -> Vec<String> {
    value
        .split_whitespace()
        .map(|part| {
            part.chars()
                .filter(|character| character.is_alphanumeric() || *character == '\'')
                .flat_map(char::to_lowercase)
                .collect::<String>()
        })
        .filter(|part| !part.is_empty())
        .collect()
}

fn native_benchmark_word_error_rate(reference: &str, hypothesis: &str) -> Option<f64> {
    let reference = native_benchmark_normalize_words(reference);
    let hypothesis = native_benchmark_normalize_words(hypothesis);
    if reference.is_empty() {
        return None;
    }

    let mut previous: Vec<usize> = (0..=hypothesis.len()).collect();
    let mut current = vec![0usize; hypothesis.len() + 1];

    for (i, reference_word) in reference.iter().enumerate() {
        current[0] = i + 1;
        for (j, hypothesis_word) in hypothesis.iter().enumerate() {
            let substitution = previous[j] + usize::from(reference_word != hypothesis_word);
            let deletion = previous[j + 1] + 1;
            let insertion = current[j] + 1;
            current[j + 1] = substitution.min(deletion).min(insertion);
        }
        std::mem::swap(&mut previous, &mut current);
    }

    Some(previous[hypothesis.len()] as f64 / reference.len() as f64)
}

fn native_benchmark_realtime_factor(
    inference_seconds: f64,
    audio_duration_seconds: f64,
) -> Option<f64> {
    if !inference_seconds.is_finite()
        || inference_seconds <= 0.0
        || !audio_duration_seconds.is_finite()
        || audio_duration_seconds <= 0.0
    {
        return None;
    }
    let value = inference_seconds / audio_duration_seconds;
    (value.is_finite() && value > 0.0).then_some(value)
}

fn native_benchmark_median(values: &[f64]) -> Option<f64> {
    if values.len() == 7 {
        return native_benchmark_median_seven(values);
    }

    if values.is_empty() || values.len() > 7 || values.iter().any(|v| !v.is_finite()) {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let middle = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        Some(sorted[middle])
    } else {
        Some(sorted[middle - 1] / 2.0 + sorted[middle] / 2.0)
    }
}

fn native_benchmark_median_seven(values: &[f64]) -> Option<f64> {
    if values.len() != 7 || values.iter().any(|value| !value.is_finite()) {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    Some(sorted[3])
}

fn native_benchmark_segment_metadata(value: &Value) -> (usize, bool) {
    let Some(segments) = value.get("segments").and_then(Value::as_array) else {
        return (0, false);
    };
    let mut usable = 0usize;
    let mut timing_available = true;
    for segment in segments {
        let has_text = segment
            .get("text")
            .and_then(Value::as_str)
            .is_some_and(|text| !text.trim().is_empty());
        if !has_text {
            continue;
        }
        usable += 1;
        let start = segment.get("start").and_then(Value::as_f64);
        let end = segment.get("end").and_then(Value::as_f64);
        if !matches!((start, end), (Some(a), Some(b))
            if a.is_finite() && b.is_finite() && a >= 0.0 && b >= a)
        {
            timing_available = false;
        }
    }
    (usable, usable > 0 && timing_available)
}

fn native_benchmark_run_qualification_valid(run: &Value) -> bool {
    let number = |key: &str| run.get(key).and_then(Value::as_f64);
    let (Some(wer), Some(cer), Some(strict_cer), Some(duration), Some(inference), Some(rtf)) = (
        number("wordErrorRate"),
        number("characterErrorRate"),
        number("strictCharacterErrorRate"),
        number("audioDurationSeconds"),
        number("inferenceSeconds"),
        number("realtimeFactor"),
    ) else {
        return false;
    };
    let Some(expected_rtf) = native_benchmark_realtime_factor(inference, duration) else {
        return false;
    };
    wer.is_finite()
        && (0.0..=0.10).contains(&wer)
        && cer.is_finite()
        && cer >= 0.0
        && strict_cer.is_finite()
        && strict_cer >= 0.0
        && (rtf - expected_rtf).abs() <= expected_rtf.abs().max(1.0) * 1e-12
}

fn native_benchmark_summary_metrics_valid(summary: &Value) -> bool {
    let number = |key: &str| summary.get(key).and_then(Value::as_f64);
    matches!(
        (
            number("audioDurationSeconds"),
            number("medianRealtimeFactor"),
            number("medianCharacterErrorRate"),
            number("medianStrictCharacterErrorRate"),
            number("medianInferenceMs"),
            number("medianInferenceSeconds"),
            number("medianWordErrorRate"),
        ),
        (
            Some(duration),
            Some(rtf),
            Some(cer),
            Some(strict_cer),
            Some(inference_ms),
            Some(inference_seconds),
            Some(wer),
        )
            if duration.is_finite() && duration > 0.0
                && rtf.is_finite() && rtf > 0.0
                && cer.is_finite() && cer >= 0.0
                && strict_cer.is_finite() && strict_cer >= 0.0
                && inference_ms.is_finite() && inference_ms > 0.0
                && inference_seconds.is_finite() && inference_seconds > 0.0
                && wer.is_finite() && (0.0..=0.10).contains(&wer)
    )
}

fn native_benchmark_profile_metrics_valid(profile: &Value) -> bool {
    let Some(runs) = profile.get("runs").and_then(Value::as_array) else {
        return false;
    };
    let count = runs.len();
    if !(1..=7).contains(&count) || !runs.iter().all(native_benchmark_run_qualification_valid) {
        return false;
    }

    let Some(summary) = profile.get("summary") else {
        return false;
    };
    if summary.get("measuredRunCount").and_then(Value::as_u64) != Some(count as u64)
        || profile.get("measuredRunCount").and_then(Value::as_u64) != Some(count as u64)
    {
        return false;
    }

    let collect = |key: &str| -> Option<Vec<f64>> {
        let values: Vec<f64> = runs
            .iter()
            .map(|run| run.get(key).and_then(Value::as_f64))
            .collect::<Option<Vec<_>>>()?;
        if values.len() != count || values.iter().any(|v| !v.is_finite()) {
            return None;
        }
        Some(values)
    };

    let (
        Some(inference_ms),
        Some(inference_seconds),
        Some(durations),
        Some(rtfs),
        Some(wers),
        Some(cers),
        Some(strict_cers),
    ) = (
        collect("inferenceMs"),
        collect("inferenceSeconds"),
        collect("audioDurationSeconds"),
        collect("realtimeFactor"),
        collect("wordErrorRate"),
        collect("characterErrorRate"),
        collect("strictCharacterErrorRate"),
    )
    else {
        return false;
    };

    let run_precision_matches =
        inference_ms
            .iter()
            .zip(inference_seconds.iter())
            .all(|(milliseconds, seconds)| {
                milliseconds.is_finite()
                    && *milliseconds > 0.0
                    && seconds.is_finite()
                    && *seconds > 0.0
                    && ((*milliseconds / 1000.0) - *seconds).abs() <= 0.00000051
            });
    if !run_precision_matches {
        return false;
    }

    let median = |values: &[f64]| native_benchmark_median(values);
    let (
        Some(median_ms),
        Some(median_seconds),
        Some(median_duration),
        Some(median_rtf),
        Some(median_wer),
        Some(median_cer),
        Some(median_strict_cer),
    ) = (
        median(&inference_ms),
        median(&inference_seconds),
        median(&durations),
        median(&rtfs),
        median(&wers),
        median(&cers),
        median(&strict_cers),
    )
    else {
        return false;
    };

    let mut sorted_ms = inference_ms.clone();
    sorted_ms.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let matches = |key: &str, expected: f64| -> bool {
        summary
            .get(key)
            .and_then(Value::as_f64)
            .is_some_and(|actual| {
                actual.is_finite() && (actual - expected).abs() <= expected.abs().max(1.0) * 1e-9
            })
    };

    let summary_matches = matches("medianInferenceMs", median_ms)
        && matches("medianInferenceSeconds", median_seconds)
        && matches("audioDurationSeconds", median_duration)
        && matches("medianRealtimeFactor", median_rtf)
        && matches("medianWordErrorRate", median_wer)
        && matches("medianCharacterErrorRate", median_cer)
        && matches("medianStrictCharacterErrorRate", median_strict_cer)
        && matches("minimumInferenceMs", sorted_ms[0])
        && matches("maximumInferenceMs", sorted_ms[count - 1]);

    summary_matches && (count != 7 || native_benchmark_summary_metrics_valid(summary))
}
#[tauri::command]
fn set_native_benchmark_starting(
    state: State<'_, BackendState>,
    enabled: bool,
) -> Result<(), String> {
    // Share the backend lifecycle mutex with normal Start so a tray/UI start cannot race
    // the optimizer's reservation and create a backend between candidate runs.
    let _lifecycle = state.lifecycle.lock().unwrap_or_else(|p| p.into_inner());
    if enabled {
        if NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst) {
            return Err("A native hardware benchmark is already running.".into());
        }
        NATIVE_BENCHMARK_STARTING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "A native hardware benchmark is already starting.".to_string())?;
    } else {
        NATIVE_BENCHMARK_STARTING.store(false, Ordering::SeqCst);
    }
    Ok(())
}

#[tauri::command]
fn cancel_native_benchmark() -> bool {
    let _control = NATIVE_BENCHMARK_CONTROL_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst) {
        NATIVE_BENCHMARK_CANCEL_REQUESTED.store(true, Ordering::SeqCst);
        true
    } else {
        false
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct NativeBenchmarkFixtureSpec {
    mode: &'static str,
    directory: &'static str,
    audio_filename: &'static str,
    fixture_id: &'static str,
    expected_sha256: &'static str,
    minimum_duration_seconds: f64,
    maximum_duration_seconds: f64,
    expected_word_count: usize,
    reference_filename: Option<&'static str>,
    expected_reference_sha256: Option<&'static str>,
}

fn native_benchmark_fixture_spec(
    mode: &str,
    manual_fixture: Option<&str>,
) -> Result<NativeBenchmarkFixtureSpec, String> {
    match (mode, manual_fixture) {
        ("first-run", None) => Ok(NativeBenchmarkFixtureSpec {
            mode: "first-run",
            directory: "first-run-benchmark",
            audio_filename: "audio.wav",
            fixture_id: "rdt-user-voice-first-run-12s-v1",
            expected_sha256: "55c7b7d3b127a50ce6943a08e0078ff3aa67f42ca926814201a819654914450a",
            minimum_duration_seconds: 11.5,
            maximum_duration_seconds: 12.5,
            expected_word_count: 28,
            reference_filename: Some("reference-28-words.txt"),
            expected_reference_sha256: Some(
                "c2a2654a48f82a06d23928bd46b85a5801fb40935bcaa3fbfb356ccbd75aca56",
            ),
        }),
        ("first-run", Some(_)) => {
            Err("Mandatory first-run benchmarking does not accept manual fixture settings.".into())
        }
        ("manual", Some("12s")) => Ok(NativeBenchmarkFixtureSpec {
            mode: "manual",
            directory: "first-run-benchmark",
            audio_filename: "audio.wav",
            fixture_id: "rdt-user-voice-first-run-12s-v1",
            expected_sha256: "55c7b7d3b127a50ce6943a08e0078ff3aa67f42ca926814201a819654914450a",
            minimum_duration_seconds: 11.5,
            maximum_duration_seconds: 12.5,
            expected_word_count: 28,
            reference_filename: Some("reference-28-words.txt"),
            expected_reference_sha256: Some(
                "c2a2654a48f82a06d23928bd46b85a5801fb40935bcaa3fbfb356ccbd75aca56",
            ),
        }),
        ("manual", Some("21s")) => Ok(NativeBenchmarkFixtureSpec {
            mode: "manual",
            directory: "benchmark",
            audio_filename: "user-voice-21s.wav",
            fixture_id: "rdt-user-voice-manual-21s-v1",
            expected_sha256: "060ce7a72e16521bb90a646015b27bc714c662a155f3bd78371ed66c264d1938",
            minimum_duration_seconds: 20.0,
            maximum_duration_seconds: 21.5,
            expected_word_count: 50,
            reference_filename: None,
            expected_reference_sha256: None,
        }),
        ("manual", _) => Err("Manual benchmark fixture must be exactly 12s or 21s.".into()),
        _ => Err("Benchmark fixture mode must be exactly first-run or manual.".into()),
    }
}

fn native_benchmark_candidate_timeout_ms(
    fixture_mode: &str,
    manual_fixture: Option<&str>,
    remaining_budget_ms: Option<u64>,
) -> Result<Option<u64>, String> {
    match fixture_mode {
        "first-run" => {
            if manual_fixture.is_some() {
                return Err(
                    "Mandatory first-run benchmarking cannot use manual fixture settings.".into(),
                );
            }
            let remaining = remaining_budget_ms
                .filter(|value| *value > 0)
                .ok_or_else(|| "The shared benchmark time budget has expired.".to_string())?;
            Ok(Some(remaining.min(60_000)))
        }
        "manual" => {
            let maximum = match manual_fixture {
                Some("12s") => 60_000,
                Some("21s") => 120_000,
                _ => return Err("Manual benchmark fixture must be exactly 12s or 21s.".into()),
            };
            match remaining_budget_ms {
                Some(0) => Err("The selected manual candidate time limit has expired.".into()),
                Some(remaining) => Ok(Some(remaining.min(maximum))),
                None => Ok(None),
            }
        }
        _ => Err("Benchmark fixture mode must be exactly first-run or manual.".into()),
    }
}

fn native_benchmark_remaining_timeout(
    deadline: Option<&Instant>,
) -> Result<Option<Duration>, String> {
    if NATIVE_BENCHMARK_CANCEL_REQUESTED.load(Ordering::SeqCst) {
        return Err(
            "Benchmark cancellation was requested; no further HTTP request will start.".into(),
        );
    }
    let Some(deadline) = deadline else {
        return Ok(None);
    };
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return Err(
            "The benchmark hard time limit was reached; this candidate cannot qualify.".into(),
        );
    }
    Ok(Some(remaining))
}

trait NativeBenchmarkRequestDeadline {
    fn apply_benchmark_deadline(self, deadline: Option<&Instant>) -> Result<Self, String>
    where
        Self: Sized;
}

impl NativeBenchmarkRequestDeadline for reqwest::blocking::RequestBuilder {
    fn apply_benchmark_deadline(self, deadline: Option<&Instant>) -> Result<Self, String> {
        Ok(match native_benchmark_remaining_timeout(deadline)? {
            Some(timeout) => self.timeout(timeout),
            None => self,
        })
    }
}

fn native_benchmark_safe_failure_code(message: &str) -> &'static str {
    let value = message.to_ascii_lowercase();
    if value.contains("segment") {
        "transcript_segment_validation"
    } else if value.contains("provider") {
        "provider_verification_failed"
    } else if value.contains("lease") {
        "benchmark_lease_failed"
    } else if value.contains("cancel") {
        "benchmark_cancelled"
    } else if value.contains("time limit") || value.contains("timed out") {
        "benchmark_time_limit"
    } else if value.contains("decode") || value.contains("wav") {
        "fixture_or_audio_decode_failed"
    } else if value.contains("transcription") {
        "transcription_failed"
    } else if value.contains("health") || value.contains("http") || value.contains("api") {
        "backend_api_failed"
    } else {
        "benchmark_run_failed"
    }
}

fn native_benchmark_diagnostic_record(
    started_at_ms: u128,
    finished_at_ms: u128,
    fixture_mode: &str,
    candidate: Option<&str>,
    backend_ready_ms: Option<u64>,
    backend_reused: Option<bool>,
    candidate_index: Option<u32>,
    candidate_count: Option<u32>,
    result: &Result<Value, String>,
    last_phase: &str,
    terminal_outcome: Option<&str>,
) -> Value {
    let profile = result.as_ref().ok();
    let field = |key: &str| {
        profile
            .and_then(|value| value.get(key))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let summary_source = profile.and_then(|value| value.get("summary"));
    let fixture_source = profile.and_then(|value| value.get("fixture"));
    let candidate_failure = profile
        .and_then(|value| value.get("candidateFailure"))
        .and_then(Value::as_str);
    let failure_message = result
        .as_ref()
        .err()
        .map(String::as_str)
        .or(candidate_failure);
    let failed = result.is_err() || candidate_failure.is_some();
    let complete = field("candidateComplete").as_bool() == Some(true);
    let outcome = if failed {
        "failed"
    } else if complete {
        "complete"
    } else {
        "partial"
    };
    let failure_code = failure_message
        .map(native_benchmark_safe_failure_code)
        .or_else(|| {
            if outcome == "partial" {
                Some("incomplete_measured_samples")
            } else {
                None
            }
        });

    let runs = profile.and_then(|value| value.get("runs"))
        .and_then(Value::as_array)
        .map(|items| items.iter().map(|run| {
            let provider = run.get("provider");
            json!({
                "inferenceMs": run.get("inferenceMs").cloned().unwrap_or(Value::Null),
                "inferenceSeconds": run.get("inferenceSeconds").cloned().unwrap_or(Value::Null),
                "audioDurationSeconds": run.get("audioDurationSeconds").cloned().unwrap_or(Value::Null),
                "realtimeFactor": run.get("realtimeFactor").cloned().unwrap_or(Value::Null),
                "requestWallMs": run.get("requestWallMs").cloned().unwrap_or(Value::Null),
                "providerName": provider.and_then(|p| p.get("provider")).cloned().unwrap_or(Value::Null),
                "providerVerified": provider.and_then(|p| p.get("provider_verified")).cloned().unwrap_or(Value::Null),
                "requestedDevice": provider.and_then(|p| p.get("requested_device")).cloned().unwrap_or(Value::Null),
                "wordErrorRate": run.get("wordErrorRate").cloned().unwrap_or(Value::Null),
                "characterErrorRate": run.get("characterErrorRate").cloned().unwrap_or(Value::Null),
                "strictCharacterErrorRate": run.get("strictCharacterErrorRate").cloned().unwrap_or(Value::Null),
                "transcriptCharacters": run.get("transcriptCharacters").cloned().unwrap_or(Value::Null),
                "segmentCount": run.get("segmentCount").cloned().unwrap_or(Value::Null)
            })
        }).collect::<Vec<Value>>())
        .unwrap_or_default();

    let summary_field = |key: &str| {
        summary_source
            .and_then(|value| value.get(key))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let fixture_field = |key: &str| {
        fixture_source
            .and_then(|value| value.get(key))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let summary = json!({
        "provider": summary_field("provider"),
        "providerStableAcrossAllRequests": summary_field("providerStableAcrossAllRequests"),
        "medianInferenceMs": summary_field("medianInferenceMs"),
        "minimumInferenceMs": summary_field("minimumInferenceMs"),
        "maximumInferenceMs": summary_field("maximumInferenceMs"),
        "medianRealtimeFactor": summary_field("medianRealtimeFactor"),
        "medianWordErrorRate": summary_field("medianWordErrorRate"),
        "medianCharacterErrorRate": summary_field("medianCharacterErrorRate"),
        "medianStrictCharacterErrorRate": summary_field("medianStrictCharacterErrorRate"),
        "measuredRunCount": summary_field("measuredRunCount")
    });

    let category_failure_code = failure_message.map(native_benchmark_safe_failure_code);
    let measured_run_count = field("measuredRunCount").as_u64().unwrap_or(0);
    let lease_release_failed = field("leaseReleaseConfirmed").as_bool() == Some(false);
    let qualification_failure = matches!(
        category_failure_code,
        Some("transcript_segment_validation") | Some("provider_verification_failed")
    );
    let infrastructure_failure = failed || lease_release_failed;

    let failure_category = if terminal_outcome == Some("timed_out") {
        "timeout"
    } else if terminal_outcome == Some("interrupted") {
        "interrupted"
    } else if qualification_failure {
        "qualification"
    } else if infrastructure_failure {
        "infrastructure"
    } else if !complete {
        "incomplete"
    } else {
        "none"
    };

    let diagnostic_outcome = if terminal_outcome == Some("timed_out") {
        "timed_out"
    } else if terminal_outcome == Some("interrupted") {
        "interrupted"
    } else if qualification_failure {
        "qualification_failed"
    } else if infrastructure_failure && measured_run_count > 0 && measured_run_count < 7 {
        "partial"
    } else if infrastructure_failure {
        "failed"
    } else if complete {
        "complete"
    } else {
        "partial"
    };

    json!({
        "schema": "rdt-connect-native-benchmark-attempt-v1",
        "startedAtUnixMs": started_at_ms,
        "finishedAtUnixMs": finished_at_ms,
        "elapsedMs": finished_at_ms.saturating_sub(started_at_ms),
        "fixtureMode": fixture_mode,
        "candidateDevice": candidate,
        "candidateIndex": candidate_index,
        "candidateCount": candidate_count,
        "model": field("model"),
        "platform": field("platform"),
        "architecture": field("architecture"),
        "buildId": field("buildId"),
        "appVersion": field("appVersion"),
        "backendReadyMs": backend_ready_ms,
        "backendReused": backend_reused.unwrap_or(false),
        "fixture": {
            "fixtureId": fixture_field("fixture_id").as_str().map(|v| v.to_owned()).or_else(|| field("fixtureId").as_str().map(|v| v.to_owned())).unwrap_or_else(|| "unknown".to_owned()),
            "durationSeconds": fixture_field("duration_seconds"),
            "sampleRateHz": fixture_field("sample_rate_hz"),
            "sha256": fixture_field("sha256")
        },
        "outcome": diagnostic_outcome,
        "failureCategory": failure_category,
        "lastPhase": last_phase,
        "terminalOutcome": terminal_outcome.unwrap_or("none"),
        "failureCode": failure_code,
        "measuredRunCount": field("measuredRunCount"),
        "candidateComplete": complete,
        "leaseReleaseConfirmed": field("leaseReleaseConfirmed"),
        "summary": summary,
        "runs": runs
    })
}

fn native_benchmark_diagnostics_path(state: &BackendState) -> Result<PathBuf, String> {
    state
        .app_language_pref_path
        .parent()
        .map(|parent| parent.join("native-benchmark-diagnostics-v1.json"))
        .ok_or_else(|| {
            "Could not locate the app support folder for benchmark diagnostics.".to_string()
        })
}

fn append_native_benchmark_diagnostic(state: &BackendState, record: Value) -> Result<(), String> {
    let _guard = NATIVE_BENCHMARK_DIAGNOSTICS_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let path = native_benchmark_diagnostics_path(state)?;
    let mut attempts = match fs::read(&path) {
        Ok(bytes) => {
            let existing: Value = serde_json::from_slice(&bytes).map_err(|_| {
                "Existing benchmark diagnostics are invalid; preserving the file.".to_string()
            })?;
            existing.get("attempts").and_then(Value::as_array).cloned()
                .ok_or_else(|| "Existing benchmark diagnostics have an invalid attempts list; preserving the file.".to_string())?
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(_) => return Err("Could not read existing benchmark diagnostics.".into()),
    };
    attempts.push(record);
    let excess = attempts.len().saturating_sub(20);
    if excess > 0 {
        attempts.drain(0..excess);
    }
    let document = json!({
        "schema": "rdt-connect-native-benchmark-diagnostics-v1",
        "updatedAtUnixMs": unix_epoch_millis(),
        "retention": { "maximumAttempts": 20, "storage": "local app support; private file" },
        "privacy": {
            "audioIncluded": false,
            "transcriptIncluded": false,
            "credentialsIncluded": false,
            "rawErrorsIncluded": false,
            "hardwareFingerprintIncluded": false
        },
        "attempts": attempts
    });
    let bytes = serde_json::to_vec_pretty(&document)
        .map_err(|_| "Could not encode benchmark diagnostics.".to_string())?;
    write_private_atomic(&path, &bytes)
}

#[tauri::command]
fn get_native_benchmark_diagnostics(state: State<'_, BackendState>) -> Result<Value, String> {
    let _guard = NATIVE_BENCHMARK_DIAGNOSTICS_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let path = native_benchmark_diagnostics_path(state.inner())?;
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map_err(|_| "Saved benchmark diagnostics could not be parsed.".to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(json!({
            "schema": "rdt-connect-native-benchmark-diagnostics-v1",
            "retention": { "maximumAttempts": 20, "storage": "local app support; private file" },
            "privacy": {
                "audioIncluded": false,
                "transcriptIncluded": false,
                "credentialsIncluded": false,
                "rawErrorsIncluded": false,
                "hardwareFingerprintIncluded": false
            },
            "attempts": []
        })),
        Err(_) => Err("Could not read benchmark diagnostics.".into()),
    }
}

#[cfg(test)]
mod native_benchmark_1d30_privacy_tests {
    use super::*;

    #[test]
    fn optional_segment_count_does_not_require_timing_data() {
        let with_segments = json!({"segments":[{"start":0.0},{"start":1.0}]});
        let without_segments = json!({"text":"valid transcript"});
        assert_eq!(
            with_segments
                .get("segments")
                .and_then(Value::as_array)
                .map(|v| v.len()),
            Some(2)
        );
        assert_eq!(
            without_segments
                .get("segments")
                .and_then(Value::as_array)
                .map(|v| v.len()),
            None
        );
    }

    #[test]
    fn diagnostic_record_excludes_transcript_and_hardware_fingerprint() {
        let profile = json!({
            "model":"mlx-community/whisper-tiny-mlx",
            "platform":"macos",
            "architecture":"aarch64",
            "buildId":"1D29",
            "appVersion":"0.2.0-dev.12.1D29",
            "hardwareFingerprint":"PRIVATE_DEVICE_FINGERPRINT",
            "candidateComplete":true,
            "leaseReleaseConfirmed":true,
            "measuredRunCount":1,
            "summary":{"provider":"mlx-metal-gpu","medianInferenceMs":12.0},
            "runs":[{
                "inferenceMs":12.0,
                "provider":{"provider":"mlx-metal-gpu","provider_verified":true},
                "transcript":"PRIVATE_TRANSCRIPT_SHOULD_NOT_APPEAR",
                "text":"PRIVATE_TRANSCRIPT_SHOULD_NOT_APPEAR"
            }]
        });
        let result: Result<Value, String> = Ok(profile);
        let record = native_benchmark_diagnostic_record(
            100,
            112,
            "manual",
            Some("gpu"),
            Some(10),
            Some(false),
            Some(1),
            Some(2),
            &result,
            "complete",
            None,
        );
        let encoded = serde_json::to_string(&record).unwrap();
        assert!(!encoded.contains("PRIVATE_TRANSCRIPT_SHOULD_NOT_APPEAR"));
        assert!(!encoded.contains("PRIVATE_DEVICE_FINGERPRINT"));
        assert_eq!(record["outcome"], "complete");
        assert_eq!(record["runs"][0]["providerName"], "mlx-metal-gpu");
    }

    #[test]
    fn diagnostic_record_preserves_partial_runs_when_timed_out() {
        let profile = json!({
            "candidateComplete": false,
            "leaseReleaseConfirmed": true,
            "measuredRunCount": 2,
            "summary": {"provider":"mlx-metal-gpu","medianInferenceMs":14.0},
            "runs": [
                {"inferenceMs":13.0,"provider":{"provider":"mlx-metal-gpu","provider_verified":true}},
                {"inferenceMs":15.0,"provider":{"provider":"mlx-metal-gpu","provider_verified":true}}
            ]
        });
        let result: Result<Value, String> = Ok(profile);
        let record = native_benchmark_diagnostic_record(
            100,
            245,
            "manual",
            Some("gpu"),
            Some(10),
            Some(false),
            Some(1),
            Some(2),
            &result,
            "measuring",
            Some("timed_out"),
        );

        assert_eq!(record["outcome"], "timed_out");
        assert_eq!(record["failureCategory"], "timeout");
        assert_eq!(record["lastPhase"], "measuring");
        assert_eq!(record["terminalOutcome"], "timed_out");
        assert_eq!(record["elapsedMs"], 145);
        assert_eq!(record["measuredRunCount"], 2);
        assert_eq!(record["runs"].as_array().unwrap().len(), 2);
        assert_eq!(record["candidateComplete"], false);
    }

    #[test]
    fn diagnostic_record_distinguishes_interruption_qualification_and_infrastructure() {
        let interrupted_profile = json!({
            "candidateComplete": false,
            "candidateFailure": "Benchmark cancelled; previous successful results were preserved.",
            "leaseReleaseConfirmed": true,
            "measuredRunCount": 1,
            "runs": [
                {"inferenceMs":13.0,"provider":{"provider":"mlx-metal-gpu","provider_verified":true}}
            ]
        });
        let interrupted_result: Result<Value, String> = Ok(interrupted_profile);
        let interrupted = native_benchmark_diagnostic_record(
            100,
            200,
            "manual",
            Some("gpu"),
            Some(10),
            Some(false),
            Some(1),
            Some(2),
            &interrupted_result,
            "measuring",
            Some("interrupted"),
        );
        assert_eq!(interrupted["outcome"], "interrupted");
        assert_eq!(interrupted["failureCategory"], "interrupted");
        assert_eq!(interrupted["lastPhase"], "measuring");
        assert_eq!(interrupted["runs"].as_array().unwrap().len(), 1);
        assert_eq!(interrupted["candidateComplete"], false);

        let qualification_profile = json!({
            "candidateComplete": false,
            "candidateFailure": "Provider verification failed.",
            "leaseReleaseConfirmed": true,
            "measuredRunCount": 1,
            "runs": []
        });
        let qualification_result: Result<Value, String> = Ok(qualification_profile);
        let qualification = native_benchmark_diagnostic_record(
            100,
            200,
            "manual",
            Some("gpu"),
            Some(10),
            Some(false),
            Some(1),
            Some(2),
            &qualification_result,
            "verification",
            None,
        );
        assert_eq!(qualification["outcome"], "qualification_failed");
        assert_eq!(qualification["failureCategory"], "qualification");
        assert_eq!(qualification["candidateComplete"], false);

        let infrastructure_result: Result<Value, String> =
            Err("Could not renew benchmark lease: connection refused".into());
        let infrastructure = native_benchmark_diagnostic_record(
            100,
            200,
            "manual",
            Some("gpu"),
            Some(10),
            Some(false),
            Some(1),
            Some(2),
            &infrastructure_result,
            "lease-renewal",
            None,
        );
        assert_eq!(infrastructure["outcome"], "failed");
        assert_eq!(infrastructure["failureCategory"], "infrastructure");
    }

    #[test]
    fn benchmark_candidate_timeout_policy_separates_mandatory_and_manual_modes() {
        assert_eq!(
            native_benchmark_candidate_timeout_ms("first-run", None, Some(180_000)).unwrap(),
            Some(60_000)
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("first-run", None, Some(24_000)).unwrap(),
            Some(24_000)
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("manual", Some("12s"), Some(90_000)).unwrap(),
            Some(60_000)
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("manual", Some("21s"), Some(180_000)).unwrap(),
            Some(120_000)
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("manual", Some("21s"), Some(30_000)).unwrap(),
            Some(30_000)
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("manual", Some("12s"), None).unwrap(),
            None
        );
        assert_eq!(
            native_benchmark_candidate_timeout_ms("manual", Some("21s"), None).unwrap(),
            None
        );
        assert!(
            native_benchmark_candidate_timeout_ms("first-run", Some("21s"), Some(60_000)).is_err()
        );
        assert!(native_benchmark_candidate_timeout_ms("first-run", None, None).is_err());
        assert!(native_benchmark_candidate_timeout_ms("manual", Some("invalid"), None).is_err());
    }
}

#[tauri::command]
async fn run_hardware_native_benchmark(
    app: AppHandle,
    fixture_mode: String,
    manual_fixture: Option<String>,
    candidate: Option<String>,
    optimization_session_id: Option<String>,
    backend_ready_ms: Option<u64>,
    backend_reused: Option<bool>,
    candidate_index: Option<u32>,
    candidate_count: Option<u32>,
    remaining_budget_ms: Option<u64>,
) -> Result<Value, String> {
    let started_at_ms = unix_epoch_millis();
    let diagnostic_fixture_mode = fixture_mode.clone();
    let diagnostic_candidate = candidate.clone();
    let diagnostic_backend_ready_ms = backend_ready_ms;
    let diagnostic_backend_reused = backend_reused;
    let diagnostic_candidate_index = candidate_index;
    let diagnostic_candidate_count = candidate_count;
    let diagnostic_last_phase = Arc::new(Mutex::new("starting".to_owned()));
    let diagnostic_terminal_outcome = Arc::new(Mutex::new(None::<String>));
    let worker_last_phase = Arc::clone(&diagnostic_last_phase);
    let worker_terminal_outcome = Arc::clone(&diagnostic_terminal_outcome);
    let task_app = app.clone();
    let worker_result = tauri::async_runtime::spawn_blocking(move || {
        let state = task_app.state::<BackendState>();
        run_hardware_native_benchmark_blocking(
            task_app.clone(),
            state.inner(),
            fixture_mode,
            manual_fixture,
            candidate,
            optimization_session_id,
            backend_ready_ms,
            backend_reused,
            candidate_index,
            candidate_count,
            remaining_budget_ms,
            worker_last_phase,
            worker_terminal_outcome,
        )
    })
    .await;
    let result = match worker_result {
        Ok(value) => value,
        Err(error) => Err(format!("Native benchmark worker failed: {error}")),
    };
    let last_phase = diagnostic_last_phase
        .lock()
        .map(|phase| phase.clone())
        .unwrap_or_else(|_| "unknown".to_owned());
    let terminal_outcome = diagnostic_terminal_outcome
        .lock()
        .ok()
        .and_then(|outcome| outcome.clone());
    let finished_at_ms = unix_epoch_millis();
    let state = app.state::<BackendState>();
    let record = native_benchmark_diagnostic_record(
        started_at_ms,
        finished_at_ms,
        &diagnostic_fixture_mode,
        diagnostic_candidate.as_deref(),
        diagnostic_backend_ready_ms,
        diagnostic_backend_reused,
        diagnostic_candidate_index,
        diagnostic_candidate_count,
        &result,
        &last_phase,
        terminal_outcome.as_deref(),
    );
    if let Err(error) = append_native_benchmark_diagnostic(state.inner(), record) {
        eprintln!("[RDT Connect] Could not save benchmark diagnostics: {error}");
    }
    result
}

fn run_hardware_native_benchmark_blocking(
    app: AppHandle,
    state: &BackendState,
    fixture_mode: String,
    manual_fixture: Option<String>,
    candidate: Option<String>,
    optimization_session_id: Option<String>,
    backend_ready_ms: Option<u64>,
    backend_reused: Option<bool>,
    candidate_index: Option<u32>,
    candidate_count: Option<u32>,
    remaining_budget_ms: Option<u64>,
    last_phase: Arc<Mutex<String>>,
    terminal_outcome: Arc<Mutex<Option<String>>>,
) -> Result<Value, String> {
    let fixture_spec = native_benchmark_fixture_spec(&fixture_mode, manual_fixture.as_deref())?;
    let native_budget_ms = native_benchmark_candidate_timeout_ms(
        &fixture_mode,
        manual_fixture.as_deref(),
        remaining_budget_ms,
    )?;
    let benchmark_deadline =
        native_budget_ms.map(|budget| Instant::now() + Duration::from_millis(budget));
    if let Some(candidate_name) = candidate.as_deref() {
        if !NATIVE_BENCHMARK_STARTING.load(Ordering::SeqCst) {
            return Err(
                "Candidate benchmarks require the guarded hardware optimization flow.".into(),
            );
        }
        if !matches!(candidate_name, "gpu" | "cpu") {
            return Err("Hardware benchmark candidate must be gpu or cpu.".into());
        }
        let session = optimization_session_id.as_deref().unwrap_or_default();
        if session.len() < 16
            || session.len() > 128
            || !session
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
        {
            return Err("The hardware optimization session identifier is invalid.".into());
        }
        let reused = backend_reused == Some(true);
        let ready_duration_valid = if reused {
            backend_ready_ms == Some(0)
        } else {
            matches!(backend_ready_ms, Some(1..=90_000))
        };
        if !ready_duration_valid {
            return Err("The candidate backend-ready duration is missing or exceeds the 90-second startup ceiling.".into());
        }
        let (Some(index), Some(count)) = (candidate_index, candidate_count) else {
            return Err("The native benchmark is missing its hardware progress position.".into());
        };
        if count == 0 || count > 20 || index == 0 || index > count {
            return Err("The native benchmark hardware progress position is invalid.".into());
        }
    }
    {
        let _control = NATIVE_BENCHMARK_CONTROL_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if NATIVE_BENCHMARK_RUNNING
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("A native hardware benchmark is already running.".into());
        }
        // Reset only after successfully claiming the idle worker, while cancel
        // requests are serialized behind the same lock.
        NATIVE_BENCHMARK_CANCEL_REQUESTED.store(false, Ordering::SeqCst);
    }
    let _run_guard = NativeBenchmarkRunGuard;

    if let (Some(candidate_name), Some(candidate_index), Some(candidate_count)) =
        (candidate.as_deref(), candidate_index, candidate_count)
    {
        let candidate_label = if candidate_name == "gpu" {
            "Apple GPU"
        } else {
            "Apple CPU"
        };
        let _ = app.emit(
            "rdt-native-benchmark-progress",
            json!({
                "phase": "candidate-start",
                "candidateIndex": candidate_index,
                "candidateCount": candidate_count,
                "candidateLabel": candidate_label,
                "stepCompleted": 0,
                "stepTotal": 8
            }),
        );
    }

    let initial_status = state.status();
    if !initial_status.running {
        return Err("Start the local RDT Voice server before benchmarking.".into());
    }
    let model = initial_status
        .model
        .clone()
        .ok_or_else(|| "The running backend did not report its loaded model.".to_string())?;

    if state
        .model_download_status
        .lock()
        .map_err(|_| "Could not inspect model-download state.".to_string())?
        .active
    {
        return Err("Finish or cancel the model download before benchmarking.".into());
    }

    let tls_config = load_local_tls_config(&state)?;
    let https_status = local_https_status_from_config(&tls_config);
    if !tls_config.mode.eq_ignore_ascii_case("https")
        || !https_status.configured
        || !https_status.trusted
    {
        return Err(
            "The benchmark requires configured and trusted local HTTPS. Complete Secure your local connection setup first."
                .into(),
        );
    }

    let local_url = initial_status
        .local_url
        .clone()
        .ok_or_else(|| "The running backend has no local endpoint.".to_string())?;
    let parsed_url = url::Url::parse(&local_url)
        .map_err(|error| format!("The local backend URL is invalid: {error}"))?;
    if parsed_url.scheme() != "https"
        || !matches!(
            parsed_url.host_str(),
            Some("127.0.0.1" | "localhost" | "::1")
        )
        || parsed_url.port().is_none()
        || parsed_url.query().is_some()
        || parsed_url.fragment().is_some()
        || !parsed_url.username().is_empty()
        || parsed_url.password().is_some()
    {
        return Err(
            "The benchmark only contacts the verified local HTTPS loopback endpoint.".into(),
        );
    }

    let origin = parsed_url.origin().ascii_serialization();
    let (_port, token) = state.api_credentials()?;
    let client_timeout = benchmark_deadline
        .as_ref()
        .map(|_| Duration::from_secs(260));
    let client = local_tls_client_with_optional_timeout(&tls_config, client_timeout)?;

    // The selected mode is fixed for the whole GPU/CPU comparison session.
    let fixture_dir = state.resource_dir.join(fixture_spec.directory);
    let metadata_bytes = fs::read(fixture_dir.join("fixture.json")).map_err(|error| {
        format!(
            "Could not read {} benchmark metadata: {error}",
            fixture_spec.mode
        )
    })?;
    let fixture: Value = serde_json::from_slice(&metadata_bytes).map_err(|error| {
        format!(
            "{} benchmark metadata is invalid: {error}",
            fixture_spec.mode
        )
    })?;
    let audio_bytes = fs::read(fixture_dir.join(fixture_spec.audio_filename)).map_err(|error| {
        format!(
            "Could not read the bundled {} benchmark WAV: {error}",
            fixture_spec.mode
        )
    })?;

    if audio_bytes.is_empty()
        || audio_bytes.len() > 5 * 1024 * 1024
        || audio_bytes.len() < 44
        || &audio_bytes[0..4] != b"RIFF"
        || &audio_bytes[8..12] != b"WAVE"
    {
        return Err("The bundled benchmark WAV is missing, oversized, or invalid.".into());
    }

    let actual_sha256 = format!("{:x}", Sha256::digest(&audio_bytes));
    let metadata_sha256 = fixture
        .get("sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| "Benchmark fixture SHA-256 is missing.".to_string())?;
    if metadata_sha256 != fixture_spec.expected_sha256
        || !actual_sha256.eq_ignore_ascii_case(fixture_spec.expected_sha256)
    {
        return Err(
            "The bundled benchmark WAV failed its pinned SHA-256 integrity verification.".into(),
        );
    }
    if fixture.get("schema").and_then(Value::as_str) != Some("rdt-connect-benchmark-fixture-v1")
        || fixture.get("fixture_id").and_then(Value::as_str) != Some(fixture_spec.fixture_id)
        || fixture.get("source").and_then(Value::as_str) != Some("User-provided voice recording")
        || fixture.get("sample_rate_hz").and_then(Value::as_u64) != Some(16_000)
    {
        return Err(
            "Benchmark fixture metadata, identity, sample rate, or attribution is invalid.".into(),
        );
    }

    let reference = fixture
        .get("transcript")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| "Benchmark fixture transcript is missing.".to_string())?
        .to_string();
    if reference.split_whitespace().count() != fixture_spec.expected_word_count {
        return Err("Benchmark fixture reference has an unexpected word count.".into());
    }
    if let (Some(reference_filename), Some(expected_reference_sha256)) = (
        fixture_spec.reference_filename,
        fixture_spec.expected_reference_sha256,
    ) {
        let reference_bytes = fs::read(fixture_dir.join(reference_filename)).map_err(|error| {
            format!("Could not read the first-run reference transcript: {error}")
        })?;
        let actual_reference_sha256 = format!("{:x}", Sha256::digest(&reference_bytes));
        let bundled_reference = String::from_utf8(reference_bytes)
            .map_err(|error| format!("The first-run reference transcript is not UTF-8: {error}"))?;
        if actual_reference_sha256 != expected_reference_sha256
            || bundled_reference.trim() != reference.trim()
        {
            return Err(
                "The first-run reference transcript failed its pinned integrity check.".into(),
            );
        }
    }
    let audio_duration = fixture
        .get("duration_seconds")
        .and_then(Value::as_f64)
        .filter(|seconds| {
            seconds.is_finite()
                && (*seconds >= fixture_spec.minimum_duration_seconds)
                && (*seconds <= fixture_spec.maximum_duration_seconds)
        })
        .ok_or_else(|| {
            "Benchmark fixture duration is outside its mode-specific range.".to_string()
        })?;

    let capability_response = request_builder(
        &client,
        "GET",
        format!("{origin}/v1/benchmark/capabilities"),
        &token,
        &origin,
    )
    .apply_benchmark_deadline(benchmark_deadline.as_ref())?
    .send()
    .map_err(|error| {
        format!("Could not verify benchmark support in the local transcription server: {error}")
    })?;
    let capabilities =
        native_benchmark_response(capability_response, "Benchmark capability check")?;
    if capabilities
        .get("benchmarkLeaseApiVersion")
        .and_then(Value::as_u64)
        != Some(1)
        || capabilities
            .get("supportsNoSaveTranscription")
            .and_then(Value::as_bool)
            != Some(true)
        || capabilities
            .get("supportsLeaseRenewal")
            .and_then(Value::as_bool)
            != Some(true)
        || capabilities
            .get("supportsLeaseRelease")
            .and_then(Value::as_bool)
            != Some(true)
    {
        return Err("The installed transcription server is missing the current benchmark safety API. Rebuild the macOS app with the latest bundled backend, then run the benchmark again.".into());
    }

    let lease_response = request_builder(
        &client,
        "POST",
        format!("{origin}/v1/benchmark/lease"),
        &token,
        &origin,
    )
    .apply_benchmark_deadline(benchmark_deadline.as_ref())?
    .send()
    .map_err(|error| format!("Could not acquire benchmark lease: {error}"))?;
    let lease = native_benchmark_response(lease_response, "Benchmark lease acquisition")?;
    let session_id = lease
        .get("session_id")
        .and_then(Value::as_str)
        .filter(|value| value.len() >= 32)
        .ok_or_else(|| "The backend did not issue a valid benchmark session.".to_string())?
        .to_string();

    let benchmark_result = (|| -> Result<Value, String> {
        let mut warmup: Option<Value> = None;
        let mut measured_runs: Vec<Value> = Vec::with_capacity(7);
        let mut provider_names: Vec<String> = Vec::with_capacity(8);
        let mut provider_details: Vec<Value> = Vec::with_capacity(8);

        // Exactly 8 transcription API requests: one warm-up + seven measured.
        let mut candidate_failure: Option<String> = None;
        for index in 0..8 {
            if let Ok(mut phase) = last_phase.lock() {
                *phase = if index == 0 {
                    "candidate-start"
                } else {
                    "lease-renewal"
                }
                .to_owned();
            }
            let iteration_result: Result<(), String> = (|| -> Result<(), String> {
                if NATIVE_BENCHMARK_CANCEL_REQUESTED.load(Ordering::SeqCst) {
                    return Err(
                        "Benchmark cancelled; previous successful results were preserved.".into(),
                    );
                }

                if index > 0 {
                    let response = request_builder(
                        &client,
                        "POST",
                        format!("{origin}/v1/benchmark/lease/renew"),
                        &token,
                        &origin,
                    )
                    .header("RDT-Benchmark-Session", session_id.as_str())
                    .apply_benchmark_deadline(benchmark_deadline.as_ref())?
                    .send()
                    .map_err(|error| format!("Could not renew benchmark lease: {error}"))?;
                    native_benchmark_response(response, "Benchmark lease renewal")?;
                }

                let current = state.status();
                if !current.running || current.model.as_deref() != Some(model.as_str()) {
                    return Err(
                        "The backend stopped or its loaded model changed. No result was saved."
                            .into(),
                    );
                }

                if NATIVE_BENCHMARK_CANCEL_REQUESTED.load(Ordering::SeqCst) {
                    return Err(
                        "Benchmark cancelled; previous successful results were preserved.".into(),
                    );
                }

                let part = reqwest::blocking::multipart::Part::bytes(audio_bytes.clone())
                    .file_name(fixture_spec.audio_filename)
                    .mime_str("audio/wav")
                    .map_err(|error| format!("Could not prepare benchmark WAV: {error}"))?;

                let form = reqwest::blocking::multipart::Form::new()
                    .text("model", "whisper-1")
                    .text("language", "en")
                    .text("save_recording", "false")
                    .part("file", part);

                let started = Instant::now();
                if let Ok(mut phase) = last_phase.lock() {
                    *phase = if index == 0 { "warmup" } else { "measuring" }.to_owned();
                }
                let response = request_builder(
                    &client,
                    "POST",
                    format!("{origin}/v1/audio/transcriptions"),
                    &token,
                    &origin,
                )
                .header("RDT-Benchmark-Session", session_id.as_str())
                .multipart(form)
                .apply_benchmark_deadline(benchmark_deadline.as_ref())?
                .send()
                .map_err(|error| {
                    format!("Benchmark transcription {} failed: {error}", index + 1)
                })?;
                let request_wall_ms = started.elapsed().as_secs_f64() * 1000.0;

                let value = native_benchmark_response(
                    response,
                    &format!("Benchmark transcription {}", index + 1),
                )?;

                let transcript = value
                    .get("text")
                    .and_then(Value::as_str)
                    .filter(|text| !text.trim().is_empty())
                    .ok_or_else(|| "Benchmark transcription returned empty text.".to_string())?;
                let (segment_count, segment_timing_available) =
                    native_benchmark_segment_metadata(&value);

                if value.get("saved_recording").and_then(Value::as_bool) != Some(false) {
                    return Err(
                        "Backend did not confirm that benchmark audio was not saved.".into(),
                    );
                }

                let inference_ms = value
                    .get("inference_ms")
                    .and_then(Value::as_f64)
                    .filter(|number| number.is_finite() && *number > 0.0)
                    .ok_or_else(|| {
                        "Backend did not return valid model-inference timing.".to_string()
                    })?;
                let audio_duration_seconds = value
                    .get("duration_original")
                    .and_then(Value::as_f64)
                    .filter(|number| number.is_finite() && *number > 0.0)
                    .ok_or_else(|| {
                        "Backend did not return a valid decoded audio duration.".to_string()
                    })?;
                if (audio_duration_seconds - audio_duration).abs() > 0.05 {
                    return Err(format!(
                        "Decoded audio duration ({audio_duration_seconds:.5}s) differs from the verified fixture ({audio_duration:.5}s) by more than 0.05 seconds."
                    ));
                }
                let inference_seconds = value
                    .get("metrics")
                    .and_then(|metrics| metrics.get("inference_seconds"))
                    .and_then(Value::as_f64)
                    .filter(|number| number.is_finite() && *number > 0.0)
                    .ok_or_else(|| {
                        "Backend did not return valid high-precision inference seconds.".to_string()
                    })?;
                if (inference_ms / 1000.0 - inference_seconds).abs() > 0.00000051 {
                    return Err("Backend inference_ms and metrics.inference_seconds disagree beyond rounding precision.".into());
                }
                let realtime_factor =
                    native_benchmark_realtime_factor(inference_seconds, audio_duration_seconds)
                        .ok_or_else(|| {
                            "Could not calculate a valid true real-time factor.".to_string()
                        })?;
                let provider = value.get("inference_provider").cloned().ok_or_else(|| {
                    "Backend did not identify the inference provider.".to_string()
                })?;
                let provider_name = provider
                    .get("provider")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "Backend returned invalid provider information.".to_string())?;
                let runtime_verified =
                    provider.get("provider_verified").and_then(Value::as_bool) == Some(true);
                let requested_device = provider.get("requested_device").and_then(Value::as_str);
                if !matches!(
                    provider_name,
                    "mlx-metal-gpu" | "mlx-cpu-fallback" | "mlx-cpu-selected"
                ) {
                    return Err(format!("Unsupported MLX provider: {provider_name}"));
                }
                if let Some(candidate_name) = candidate.as_deref() {
                    let provider_matches = match candidate_name {
                        "gpu" => provider_name == "mlx-metal-gpu",
                        "cpu" => provider_name == "mlx-cpu-selected",
                        _ => false,
                    };
                    if !provider_matches
                        || !runtime_verified
                        || requested_device != Some(candidate_name)
                    {
                        return Err(format!(
                            "The requested {candidate_name} candidate failed strict provider verification: provider={provider_name}, requested_device={}, provider_verified={runtime_verified}. This candidate is not eligible for comparison.",
                            requested_device.unwrap_or("missing")
                        ));
                    }
                }

                let word_error_rate = native_benchmark_word_error_rate(&reference, transcript)
                    .ok_or_else(|| {
                        "Could not calculate benchmark transcript quality.".to_string()
                    })?;
                let accuracy = native_benchmark_accuracy(transcript, &reference);
                let character_error_rate = accuracy
                    .get("normalized_cer")
                    .and_then(Value::as_f64)
                    .filter(|number| number.is_finite() && *number >= 0.0)
                    .ok_or_else(|| {
                        "Could not calculate normalized character error rate.".to_string()
                    })?;
                let strict_character_error_rate = accuracy
                    .get("strict_cer")
                    .and_then(Value::as_f64)
                    .filter(|number| number.is_finite() && *number >= 0.0)
                    .ok_or_else(|| {
                        "Could not calculate strict character error rate.".to_string()
                    })?;

                provider_names.push(provider_name.to_string());
                provider_details.push(provider.clone());

                let run = json!({
                    "iteration": index,
                    "inferenceMs": inference_ms,
                    "inferenceSeconds": inference_seconds,
                    "audioDurationSeconds": audio_duration_seconds,
                    "realtimeFactor": realtime_factor,
                    "requestWallMs": request_wall_ms,
                    "provider": provider,
                    "wordErrorRate": word_error_rate,
                    "characterErrorRate": character_error_rate,
                    "strictCharacterErrorRate": strict_character_error_rate,
                    "transcriptCharacters": transcript.chars().count(),
                    "segmentCount": segment_count,
                    "segmentTimingAvailable": segment_timing_available,
                });

                if index == 0 {
                    warmup = Some(run);
                } else {
                    measured_runs.push(run);
                }

                let _ = app.emit(
                    "rdt-native-benchmark-progress",
                    json!({
                        "phase": if index == 0 { "warmup" } else { "measuring" },
                        "completed": index + 1,
                        "total": 8,
                        "measuredCompleted": measured_runs.len(),
                        "measuredTotal": 7,
                        "candidateIndex": candidate_index,
                        "candidateCount": candidate_count,
                        "candidateLabel": if candidate.as_deref() == Some("gpu") { "Apple GPU" } else { "Apple CPU" },
                        "stepCompleted": index + 1,
                        "stepTotal": 8,
                    }),
                );
                Ok(())
            })();
            if let Err(error) = iteration_result {
                candidate_failure = Some(error);
                break;
            }
        }
        if NATIVE_BENCHMARK_CANCEL_REQUESTED.load(Ordering::SeqCst) && candidate_failure.is_none() {
            candidate_failure =
                Some("Benchmark cancelled after preserving completed measured runs.".into());
        }
        if measured_runs.is_empty() {
            return Err(candidate_failure.unwrap_or_else(|| {
                "No verified measured run completed; no candidate profile was saved.".into()
            }));
        }

        let mut timings: Vec<f64> = measured_runs
            .iter()
            .filter_map(|run| run.get("inferenceMs").and_then(Value::as_f64))
            .collect();
        if timings.len() != measured_runs.len() {
            return Err("A measured run lacked a valid inference time.".into());
        }
        timings.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let mut wers: Vec<f64> = measured_runs
            .iter()
            .filter_map(|run| run.get("wordErrorRate").and_then(Value::as_f64))
            .collect();
        if wers.len() != measured_runs.len() {
            return Err("A measured run lacked a valid transcript-quality result.".into());
        }
        wers.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let provider_name = provider_names
            .first()
            .map(String::as_str)
            .unwrap_or("unknown");
        let provider_stable = provider_names.iter().all(|name| name == provider_name);
        let median_ms = native_benchmark_median(&timings)
            .ok_or_else(|| "Could not calculate the measured-run inference median.".to_string())?;
        let collect_metric = |key: &str| -> Vec<f64> {
            measured_runs
                .iter()
                .filter_map(|run| run.get(key).and_then(Value::as_f64))
                .collect()
        };
        let audio_durations = collect_metric("audioDurationSeconds");
        let inference_seconds_values = collect_metric("inferenceSeconds");
        let realtime_factors = collect_metric("realtimeFactor");
        let cers = collect_metric("characterErrorRate");
        let strict_cers = collect_metric("strictCharacterErrorRate");
        if audio_durations.len() != measured_runs.len()
            || inference_seconds_values.len() != measured_runs.len()
            || realtime_factors.len() != measured_runs.len()
            || cers.len() != measured_runs.len()
            || strict_cers.len() != measured_runs.len()
        {
            return Err(
                "A measured run lacked actual duration, inference seconds, RTF, or CER.".into(),
            );
        }
        let median_audio_duration = native_benchmark_median(&audio_durations).ok_or_else(|| {
            "Could not calculate the seven-run audio-duration median.".to_string()
        })?;
        let median_inference_seconds = native_benchmark_median(&inference_seconds_values)
            .ok_or_else(|| {
                "Could not calculate the seven-run inference-seconds median.".to_string()
            })?;
        let median_rtf = native_benchmark_median(&realtime_factors)
            .ok_or_else(|| "Could not calculate the seven-run true RTF median.".to_string())?;
        let median_cer = native_benchmark_median(&cers)
            .ok_or_else(|| "Could not calculate the measured-run CER median.".to_string())?;
        let median_strict_cer = native_benchmark_median(&strict_cers)
            .ok_or_else(|| "Could not calculate the measured-run strict-CER median.".to_string())?;
        let median_wer = native_benchmark_median(&wers)
            .ok_or_else(|| "Could not calculate the measured-run WER median.".to_string())?;
        if let Err(error) = native_benchmark_remaining_timeout(benchmark_deadline.as_ref()) {
            candidate_failure.get_or_insert(error);
        }
        Ok(json!({
            "schema": "rdt-connect-native-benchmark-v1",
            "createdAtUnixMs": unix_epoch_millis(),
            "platform": std::env::consts::OS,
            "architecture": std::env::consts::ARCH,
            "model": model,
            "candidateDevice": candidate,
            "candidateIndex": candidate_index,
            "candidateCount": candidate_count,
            "optimizationSessionId": optimization_session_id,
            "backendReadyMs": backend_ready_ms,
            "backendReused": backend_reused.unwrap_or(false),
            "buildId": BUILD_ID,
            "appVersion": env!("CARGO_PKG_VERSION"),
            "hardwareFingerprint": hardware_optimization_fingerprint(state),
            "fixtureMode": fixture_spec.mode,
            "fixtureId": fixture_spec.fixture_id,
            // Persist provenance and measurement identity, never the private transcript.
            "fixture": {
                "schema": fixture["schema"],
                "fixture_id": fixture["fixture_id"],
                "source": fixture["source"],
                "duration_seconds": audio_duration,
                "sample_rate_hz": fixture["sample_rate_hz"],
                "sha256": actual_sha256
            },
            "warmup": warmup,
            "runs": measured_runs,
            "measuredRunCount": measured_runs.len(),
            "candidateComplete": measured_runs.len() == 7 && candidate_failure.is_none(),
            "candidateFailure": candidate_failure,
            "summary": {
                "provider": provider_name,
                "providerStableAcrossAllRequests": provider_stable,
                "medianInferenceMs": median_ms,
                "minimumInferenceMs": timings[0],
                "maximumInferenceMs": timings[timings.len() - 1],
                "medianWordErrorRate": median_wer,
                "medianCharacterErrorRate": median_cer,
                "medianStrictCharacterErrorRate": median_strict_cer,
                "medianInferenceSeconds": median_inference_seconds,
                "audioDurationSeconds": median_audio_duration,
                "medianRealtimeFactor": median_rtf,
                "measuredRunCount": measured_runs.len(),
                "gpuSelected": provider_name == "mlx-metal-gpu",
                "providerComparisonEligible": false
            }
        }))
    })();

    let cancellation_observed = NATIVE_BENCHMARK_CANCEL_REQUESTED.load(Ordering::SeqCst)
        || benchmark_result
            .as_ref()
            .err()
            .is_some_and(|error| error.to_ascii_lowercase().contains("cancelled"))
        || benchmark_result
            .as_ref()
            .ok()
            .and_then(|profile| profile.get("candidateFailure"))
            .and_then(Value::as_str)
            .is_some_and(|message| message.to_ascii_lowercase().contains("cancelled"));
    let deadline_expired = benchmark_deadline
        .as_ref()
        .is_some_and(|deadline| Instant::now() >= *deadline);
    if let Ok(mut terminal) = terminal_outcome.lock() {
        if cancellation_observed {
            *terminal = Some("interrupted".to_owned());
        } else if benchmark_result.is_err() && deadline_expired {
            *terminal = Some("timed_out".to_owned());
        }
    }

    // Release the lease even if the run failed or the user cancelled.
    let release_result = request_builder(
        &client,
        "POST",
        format!("{origin}/v1/benchmark/lease/release"),
        &token,
        &origin,
    )
    .header("RDT-Benchmark-Session", session_id.as_str())
    .timeout(Duration::from_secs(5))
    .send()
    .map_err(|error| format!("Could not release benchmark lease: {error}"))
    .and_then(|response| native_benchmark_response(response, "Benchmark lease release"));

    let (mut profile, lease_release_confirmed, lease_release_warning) = match (
        benchmark_result,
        release_result,
    ) {
        (Ok(profile), Ok(_)) => (profile, true, None),
        (Err(error), Ok(_)) => {
            let _ = app.emit(
                "rdt-native-benchmark-progress",
                json!({"phase": "failed", "message": error}),
            );
            return Err(error);
        }
        (Ok(profile), Err(error)) => (profile, false, Some(error)),
        (Err(error), Err(release_error)) => {
            return Err(format!(
                    "{error} Lease release also failed and will rely on bounded expiry: {release_error}"
                ));
        }
    };
    let Some(profile_object) = profile.as_object_mut() else {
        return Err("The native benchmark returned an invalid profile object.".into());
    };
    profile_object.insert(
        "leaseReleaseConfirmed".into(),
        Value::Bool(lease_release_confirmed),
    );
    if !lease_release_confirmed {
        profile_object.insert("candidateComplete".into(), Value::Bool(false));
        if let Some(warning) = lease_release_warning {
            profile_object.insert("leaseReleaseWarning".into(), Value::String(warning));
        }
    }

    // Persist verified partial measurements for provisional comparison; activation still requires seven runs and confirmed lease release.
    let profile_name = match candidate.as_deref() {
        Some("gpu") => "native-benchmark-profile-gpu.json",
        Some("cpu") => "native-benchmark-profile-cpu.json",
        _ => "native-benchmark-profile.json",
    };
    let profile_path = state
        .app_language_pref_path
        .parent()
        .ok_or_else(|| "Could not locate the RDT Connect settings directory.".to_string())?
        .join(profile_name);
    let profile_bytes = serde_json::to_vec_pretty(&profile)
        .map_err(|error| format!("Could not encode benchmark profile: {error}"))?;
    write_private_atomic(&profile_path, &profile_bytes)?;

    if let Ok(mut phase) = last_phase.lock() {
        *phase = "complete".to_owned();
    }
    let _ = app.emit(
        "rdt-native-benchmark-progress",
        json!({"phase": "complete", "profile": profile}),
    );
    Ok(profile)
}

fn native_benchmark_selected_devices_valid(selected_devices: &[String]) -> bool {
    match selected_devices {
        [device] => matches!(device.as_str(), "gpu" | "cpu"),
        [first, second] => {
            matches!(first.as_str(), "gpu" | "cpu")
                && matches!(second.as_str(), "gpu" | "cpu")
                && first != second
                && ((first == "gpu" && second == "cpu") || (first == "cpu" && second == "gpu"))
        }
        _ => false,
    }
}

#[tauri::command]
fn finalize_hardware_optimization(
    state: State<'_, BackendState>,
    model: String,
    optimization_session_id: String,
    selected_devices: Vec<String>,
) -> Result<Value, String> {
    validate_model(&model)?;
    if optimization_session_id.len() < 16
        || optimization_session_id.len() > 128
        || !optimization_session_id
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
    {
        return Err("The hardware optimization session identifier is invalid.".into());
    }
    if !native_benchmark_selected_devices_valid(&selected_devices) {
        return Err(
            "Select one Apple hardware candidate or compare both Apple GPU and Apple CPU.".into(),
        );
    }
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        return Err("The MLX hardware comparison is available only on Apple Silicon Macs.".into());
    }
    if NATIVE_BENCHMARK_RUNNING.load(Ordering::SeqCst)
        || !NATIVE_BENCHMARK_STARTING.load(Ordering::SeqCst)
    {
        return Err(
            "Finish the candidate runs inside the guarded optimization flow before comparison."
                .into(),
        );
    }
    let read_candidate = |device: &str| -> Result<Value, String> {
        let filename = format!("native-benchmark-profile-{device}.json");
        let path = hardware_optimization_profile_path(state.inner(), &filename)?;
        let bytes = fs::read(&path)
            .map_err(|error| format!("Could not read the {device} benchmark profile: {error}"))?;
        let profile: Value = serde_json::from_slice(&bytes)
            .map_err(|error| format!("The {device} benchmark profile is invalid JSON: {error}"))?;
        let summary = profile
            .get("summary")
            .ok_or_else(|| format!("The {device} benchmark summary is missing."))?;
        let provider = summary
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let expected_provider = if device == "gpu" {
            provider == "mlx-metal-gpu"
        } else {
            provider == "mlx-cpu-selected"
        };
        let median = summary
            .get("medianInferenceMs")
            .and_then(Value::as_f64)
            .filter(|value| value.is_finite() && *value > 0.0)
            .ok_or_else(|| format!("The {device} benchmark has no valid median inference time."))?;
        let wer = summary
            .get("medianWordErrorRate")
            .and_then(Value::as_f64)
            .filter(|value| value.is_finite() && (0.0..=1.0).contains(value))
            .ok_or_else(|| {
                format!("The {device} benchmark has no valid transcript-quality result.")
            })?;
        let reused = profile.get("backendReused").and_then(Value::as_bool) == Some(true);
        let backend_ready_ms = profile
            .get("backendReadyMs")
            .and_then(Value::as_u64)
            .filter(|value| {
                if reused {
                    *value == 0
                } else {
                    (1..=90_000).contains(value)
                }
            })
            .ok_or_else(|| {
                format!("The {device} backend-ready duration is missing or exceeds 90 seconds.")
            })?;
        let valid = profile.get("schema").and_then(Value::as_str)
            == Some("rdt-connect-native-benchmark-v1")
            && profile.get("candidateDevice").and_then(Value::as_str) == Some(device)
            && profile.get("optimizationSessionId").and_then(Value::as_str)
                == Some(optimization_session_id.as_str())
            && profile.get("buildId").and_then(Value::as_str) == Some(BUILD_ID)
            && profile.get("appVersion").and_then(Value::as_str) == Some(env!("CARGO_PKG_VERSION"))
            && profile.get("hardwareFingerprint").and_then(Value::as_str)
                == Some(hardware_optimization_fingerprint(state.inner()).as_str())
            && profile.get("model").and_then(Value::as_str) == Some(model.as_str())
            && matches!(
                profile.get("fixtureMode").and_then(Value::as_str),
                Some("first-run" | "manual")
            )
            && profile.get("fixtureId").and_then(Value::as_str)
                == profile
                    .get("fixture")
                    .and_then(|fixture| fixture.get("fixture_id"))
                    .and_then(Value::as_str)
            && profile.get("platform").and_then(Value::as_str) == Some(std::env::consts::OS)
            && profile.get("architecture").and_then(Value::as_str) == Some(std::env::consts::ARCH)
            && profile
                .get("fixture")
                .and_then(|fixture| fixture.get("sha256"))
                .and_then(Value::as_str)
                .is_some()
            && summary
                .get("providerStableAcrossAllRequests")
                .and_then(Value::as_bool)
                == Some(true)
            && summary
                .get("measuredRunCount")
                .and_then(Value::as_u64)
                .is_some_and(|count| (1..=7).contains(&count))
            && profile.get("measuredRunCount").and_then(Value::as_u64)
                == summary.get("measuredRunCount").and_then(Value::as_u64)
            && native_benchmark_profile_metrics_valid(&profile)
            && profile
                .get("warmup")
                .map(native_benchmark_run_qualification_valid)
                == Some(true)
            && expected_provider;
        if !valid {
            return Err(format!("The {device} candidate did not pass provider stability, identity, fixture, warm-up, and measured-sample qualification."));
        }
        if wer > 0.10 {
            return Err(format!("The {device} candidate failed the transcript-quality guard (median WER {wer:.4}; maximum 0.10)."));
        }
        let _ = (median, backend_ready_ms);
        Ok(profile)
    };

    // Read only selected candidates. Unselected hardware is not a benchmark failure.
    let mut candidate_failures = serde_json::Map::new();
    let gpu = if selected_devices.iter().any(|device| device == "gpu") {
        match read_candidate("gpu") {
            Ok(profile) => Some(profile),
            Err(error) => {
                candidate_failures.insert("gpu".into(), Value::String(error));
                None
            }
        }
    } else {
        None
    };
    let cpu = if selected_devices.iter().any(|device| device == "cpu") {
        match read_candidate("cpu") {
            Ok(profile) => Some(profile),
            Err(error) => {
                candidate_failures.insert("cpu".into(), Value::String(error));
                None
            }
        }
    } else {
        None
    };
    if gpu.is_none() && cpu.is_none() {
        return Err(format!(
            "No selected Apple hardware candidate produced a verified measured run. GPU: {}; CPU: {}",
            candidate_failures
                .get("gpu")
                .and_then(Value::as_str)
                .unwrap_or("not selected"),
            candidate_failures
                .get("cpu")
                .and_then(Value::as_str)
                .unwrap_or("not selected")
        ));
    }

    let fixture_for = |profile: &Value| -> String {
        profile
            .get("fixture")
            .and_then(|value| value.get("sha256"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    };
    let candidate_is_complete = |candidate: &Option<Value>| {
        candidate.as_ref().is_some_and(|profile| {
            profile.get("candidateComplete").and_then(Value::as_bool) == Some(true)
                && profile
                    .get("leaseReleaseConfirmed")
                    .and_then(Value::as_bool)
                    == Some(true)
                && profile.get("measuredRunCount").and_then(Value::as_u64) == Some(7)
                && profile
                    .get("summary")
                    .and_then(|summary| summary.get("providerStableAcrossAllRequests"))
                    .and_then(Value::as_bool)
                    == Some(true)
        })
    };
    let comparison_complete = candidate_is_complete(&gpu) && candidate_is_complete(&cpu);
    if let (Some(gpu_profile), Some(cpu_profile)) = (&gpu, &cpu) {
        let gpu_fixture = fixture_for(gpu_profile);
        let cpu_fixture = fixture_for(cpu_profile);
        if gpu_fixture.is_empty()
            || gpu_fixture != cpu_fixture
            || gpu_profile.get("fixtureMode") != cpu_profile.get("fixtureMode")
            || gpu_profile.get("fixtureId") != cpu_profile.get("fixtureId")
        {
            return Err(
                "GPU and CPU benchmark fixtures do not match. No optimization result was created."
                    .into(),
            );
        }
    }
    let winner_device = if let (Some(gpu_profile), Some(cpu_profile)) = (&gpu, &cpu) {
        let gpu_summary = &gpu_profile["summary"];
        let cpu_summary = &cpu_profile["summary"];
        let best_wer = gpu_summary["medianWordErrorRate"]
            .as_f64()
            .unwrap()
            .min(cpu_summary["medianWordErrorRate"].as_f64().unwrap());
        let quality_limit = best_wer + 0.01;
        let mut eligible = Vec::new();
        for (device, profile) in [("gpu", gpu_profile), ("cpu", cpu_profile)] {
            let summary = &profile["summary"];
            if summary["medianWordErrorRate"].as_f64().unwrap() <= quality_limit {
                let rtf = summary["medianRealtimeFactor"].as_f64().unwrap();
                let inference_ms = summary["medianInferenceMs"].as_f64().unwrap();
                eligible.push((device, rtf, inference_ms));
            }
        }
        eligible
            .into_iter()
            .min_by(|a, b| {
                a.1.partial_cmp(&b.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| a.2.partial_cmp(&b.2).unwrap_or(std::cmp::Ordering::Equal))
            })
            .map(|(device, _, _)| device)
            .ok_or_else(|| {
                "Neither candidate met the transcript-quality comparison limit.".to_string()
            })?
    } else if gpu.is_some() {
        "gpu"
    } else {
        "cpu"
    };

    let fixture_sha = gpu
        .as_ref()
        .map(fixture_for)
        .or_else(|| cpu.as_ref().map(fixture_for))
        .unwrap_or_default();
    let candidate_json = |profile: Option<Value>, fallback_provider: &str| -> Value {
        match profile {
            Some(profile) => json!({
                "provider": profile.get("summary").and_then(|v| v.get("provider")).and_then(Value::as_str).unwrap_or(fallback_provider),
                "summary": profile.get("summary"),
                "backendReadyMs": profile.get("backendReadyMs"),
                "backendReused": profile.get("backendReused").and_then(Value::as_bool).unwrap_or(false),
                "candidateComplete": profile.get("candidateComplete").and_then(Value::as_bool).unwrap_or(false),
                "candidateFailure": profile.get("candidateFailure"),
                "measuredRunCount": profile.get("measuredRunCount"),
                "leaseReleaseConfirmed": profile.get("leaseReleaseConfirmed").and_then(Value::as_bool).unwrap_or(false),
                "leaseReleaseWarning": profile.get("leaseReleaseWarning"),
                "warmup": profile.get("warmup"),
                "runs": profile.get("runs")
            }),
            None => Value::Null,
        }
    };
    let comparison = json!({
        "schema": "rdt-connect-hardware-optimization-v1",
        "createdAtUnixMs": unix_epoch_millis(),
        "platform": std::env::consts::OS,
        "architecture": std::env::consts::ARCH,
        "buildId": BUILD_ID,
        "appVersion": env!("CARGO_PKG_VERSION"),
        "hardwareFingerprint": hardware_optimization_fingerprint(state.inner()),
        "model": model,
        "selectedDevices": selected_devices,
        "fixtureSha256": fixture_sha,
        "optimizationEligible": gpu.is_some() || cpu.is_some(),
        "providerComparisonEligible": comparison_complete,
        "comparisonComplete": comparison_complete,
        "successfulCandidateCount": (gpu.is_some() as usize) + (cpu.is_some() as usize),
        "candidateFailures": candidate_failures,
        "evaluationScope": if comparison_complete {
            "Same MLX backend and model; one local fixture; one warm-up and seven measured runs per device. Engineering comparison, not broad corpus qualification."
        } else if gpu.is_some() && cpu.is_some() {
            "Both Apple GPU and Apple CPU produced verified partial measured profiles. This is an incomplete head-to-head comparison; only completed measured runs are included."
        } else {
            "One hardware candidate produced a verified measured profile. This is a provisional single-device result, not a head-to-head comparison."
        },
        "qualityAllowanceAbsoluteWer": if gpu.is_some() && cpu.is_some() {
            Value::from(0.01)
        } else {
            Value::Null
        },
        "winnerDevice": winner_device,
        "candidates": {
            "gpu": candidate_json(gpu, "mlx-metal-gpu"),
            "cpu": candidate_json(cpu, "mlx-cpu-selected")
        }
    });
    let path =
        hardware_optimization_model_path(state.inner(), "hardware-optimization-profile", &model)?;
    let bytes = serde_json::to_vec_pretty(&comparison)
        .map_err(|error| format!("Could not encode hardware comparison: {error}"))?;
    write_private_atomic(&path, &bytes)?;
    // Keep the historical single-profile filename updated for older builds and support tooling.
    let legacy_path =
        hardware_optimization_profile_path(state.inner(), "hardware-optimization-profile.json")?;
    write_private_atomic(&legacy_path, &bytes)?;
    Ok(comparison)
}

#[cfg(test)]
mod native_benchmark_selection_contract_tests {
    use super::native_benchmark_selected_devices_valid;

    fn devices(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn accepts_single_gpu() {
        assert!(native_benchmark_selected_devices_valid(&devices(&["gpu"])));
    }

    #[test]
    fn accepts_single_cpu() {
        assert!(native_benchmark_selected_devices_valid(&devices(&["cpu"])));
    }

    #[test]
    fn accepts_gpu_cpu_pair_in_either_order() {
        assert!(native_benchmark_selected_devices_valid(&devices(&[
            "gpu", "cpu"
        ])));
        assert!(native_benchmark_selected_devices_valid(&devices(&[
            "cpu", "gpu"
        ])));
    }

    #[test]
    fn rejects_empty_duplicate_unknown_and_more_than_two_devices() {
        assert!(!native_benchmark_selected_devices_valid(&devices(&[])));
        assert!(!native_benchmark_selected_devices_valid(&devices(&[
            "gpu", "gpu"
        ])));
        assert!(!native_benchmark_selected_devices_valid(&devices(&["npu"])));
        assert!(!native_benchmark_selected_devices_valid(&devices(&[
            "gpu", "npu"
        ])));
        assert!(!native_benchmark_selected_devices_valid(&devices(&[
            "gpu", "cpu", "npu"
        ])));
    }
}

fn hardware_optimization_candidate_record_valid(candidate: &Value, device: &str) -> bool {
    let expected_provider = if device == "gpu" {
        "mlx-metal-gpu"
    } else {
        "mlx-cpu-selected"
    };
    let Some(summary) = candidate.get("summary") else {
        return false;
    };
    let Some(count) = candidate.get("measuredRunCount").and_then(Value::as_u64) else {
        return false;
    };
    let Some(reused) = candidate.get("backendReused").and_then(Value::as_bool) else {
        return false;
    };
    let Some(lease_released) = candidate
        .get("leaseReleaseConfirmed")
        .and_then(Value::as_bool)
    else {
        return false;
    };
    let Some(complete) = candidate.get("candidateComplete").and_then(Value::as_bool) else {
        return false;
    };
    let ready_ms = candidate.get("backendReadyMs").and_then(Value::as_u64);
    let ready_valid = if reused {
        ready_ms == Some(0)
    } else {
        ready_ms.is_some_and(|value| (1..=90_000).contains(&value))
    };
    let failure_absent = candidate.get("candidateFailure").is_none_or(Value::is_null);
    let complete_valid = !complete || (count == 7 && lease_released && failure_absent);
    candidate.get("provider").and_then(Value::as_str) == Some(expected_provider)
        && summary.get("provider").and_then(Value::as_str) == Some(expected_provider)
        && summary
            .get("providerStableAcrossAllRequests")
            .and_then(Value::as_bool)
            == Some(true)
        && (1..=7).contains(&count)
        && summary.get("measuredRunCount").and_then(Value::as_u64) == Some(count)
        && ready_valid
        && complete_valid
        && candidate
            .get("warmup")
            .map(native_benchmark_run_qualification_valid)
            == Some(true)
        && native_benchmark_profile_metrics_valid(candidate)
        && native_benchmark_summary_metrics_valid(summary)
        && summary
            .get("medianWordErrorRate")
            .and_then(Value::as_f64)
            .is_some_and(|value| (0.0..=0.10).contains(&value))
}

fn hardware_optimization_comparison_valid(
    state: &BackendState,
    model: &str,
    profile: &Value,
) -> bool {
    let fingerprint = hardware_optimization_fingerprint(state);
    if profile.get("schema").and_then(Value::as_str) != Some("rdt-connect-hardware-optimization-v1")
        || profile.get("model").and_then(Value::as_str) != Some(model)
        || profile.get("platform").and_then(Value::as_str) != Some(std::env::consts::OS)
        || profile.get("architecture").and_then(Value::as_str) != Some(std::env::consts::ARCH)
        || profile.get("buildId").and_then(Value::as_str) != Some(BUILD_ID)
        || profile.get("appVersion").and_then(Value::as_str) != Some(env!("CARGO_PKG_VERSION"))
        || profile.get("hardwareFingerprint").and_then(Value::as_str) != Some(fingerprint.as_str())
        || profile
            .get("createdAtUnixMs")
            .and_then(Value::as_u64)
            .is_none_or(|value| value == 0)
    {
        return false;
    }
    if let Some(value) = profile.get("selectedDevices") {
        let Some(devices) = value.as_array() else {
            return false;
        };
        if devices.len() != 2 {
            return false;
        }
        let mut gpu_selected = false;
        let mut cpu_selected = false;
        for device in devices {
            match device.as_str() {
                Some("gpu") if !gpu_selected => gpu_selected = true,
                Some("cpu") if !cpu_selected => cpu_selected = true,
                _ => return false,
            }
        }
        if !gpu_selected || !cpu_selected {
            return false;
        }
    }
    let Some(candidates) = profile.get("candidates") else {
        return false;
    };
    let Some(gpu_slot) = candidates.get("gpu") else {
        return false;
    };
    let Some(cpu_slot) = candidates.get("cpu") else {
        return false;
    };
    let gpu = if gpu_slot.is_null() {
        None
    } else if hardware_optimization_candidate_record_valid(gpu_slot, "gpu") {
        Some(gpu_slot)
    } else {
        return false;
    };
    let cpu = if cpu_slot.is_null() {
        None
    } else if hardware_optimization_candidate_record_valid(cpu_slot, "cpu") {
        Some(cpu_slot)
    } else {
        return false;
    };
    let successful_count = gpu.is_some() as u64 + cpu.is_some() as u64;
    if successful_count == 0
        || profile.get("optimizationEligible").and_then(Value::as_bool) != Some(true)
        || profile
            .get("successfulCandidateCount")
            .and_then(Value::as_u64)
            != Some(successful_count)
        || !profile
            .get("candidateFailures")
            .is_some_and(Value::is_object)
    {
        return false;
    }
    let candidate_complete = |candidate: Option<&Value>| {
        candidate.is_some_and(|value| {
            value.get("candidateComplete").and_then(Value::as_bool) == Some(true)
                && value.get("leaseReleaseConfirmed").and_then(Value::as_bool) == Some(true)
                && value.get("measuredRunCount").and_then(Value::as_u64) == Some(7)
        })
    };
    let comparison_complete = candidate_complete(gpu) && candidate_complete(cpu);
    if profile.get("comparisonComplete").and_then(Value::as_bool) != Some(comparison_complete)
        || profile
            .get("providerComparisonEligible")
            .and_then(Value::as_bool)
            != Some(comparison_complete)
    {
        return false;
    }
    let fixture_sha = profile
        .get("fixtureSha256")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if fixture_sha.len() != 64 || !fixture_sha.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return false;
    }
    let winner = profile
        .get("winnerDevice")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match (gpu, cpu) {
        (Some(gpu_profile), Some(cpu_profile)) => {
            let allowance = profile
                .get("qualityAllowanceAbsoluteWer")
                .and_then(Value::as_f64);
            if !allowance.is_some_and(|value| (value - 0.01).abs() <= 1e-12) {
                return false;
            }
            let gpu_summary = &gpu_profile["summary"];
            let cpu_summary = &cpu_profile["summary"];
            let best_wer = gpu_summary["medianWordErrorRate"]
                .as_f64()
                .unwrap()
                .min(cpu_summary["medianWordErrorRate"].as_f64().unwrap());
            let quality_limit = best_wer + 0.01;
            let mut eligible = vec![("gpu", gpu_profile), ("cpu", cpu_profile)];
            eligible.retain(|(_, candidate)| {
                candidate["summary"]["medianWordErrorRate"]
                    .as_f64()
                    .is_some_and(|wer| wer <= quality_limit)
            });
            eligible.sort_by(|(_, left), (_, right)| {
                left["summary"]["medianRealtimeFactor"]
                    .as_f64()
                    .unwrap_or(f64::INFINITY)
                    .partial_cmp(
                        &right["summary"]["medianRealtimeFactor"]
                            .as_f64()
                            .unwrap_or(f64::INFINITY),
                    )
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| {
                        left["summary"]["medianInferenceMs"]
                            .as_f64()
                            .unwrap_or(f64::INFINITY)
                            .partial_cmp(
                                &right["summary"]["medianInferenceMs"]
                                    .as_f64()
                                    .unwrap_or(f64::INFINITY),
                            )
                            .unwrap_or(std::cmp::Ordering::Equal)
                    })
            });
            eligible
                .first()
                .is_some_and(|(device, _)| *device == winner)
        }
        (Some(_), None) => {
            winner == "gpu"
                && profile
                    .get("qualityAllowanceAbsoluteWer")
                    .is_some_and(Value::is_null)
        }
        (None, Some(_)) => {
            winner == "cpu"
                && profile
                    .get("qualityAllowanceAbsoluteWer")
                    .is_some_and(Value::is_null)
        }
        (None, None) => false,
    }
}

#[tauri::command]
fn get_hardware_optimization_profile(
    state: State<'_, BackendState>,
    model: String,
) -> Result<Option<Value>, String> {
    validate_model(&model)?;
    let Some(profile) = read_model_hardware_profile(state.inner(), &model)? else {
        return Ok(None);
    };
    let eligible = profile.get("optimizationEligible").and_then(Value::as_bool) == Some(true)
        && profile
            .get("successfulCandidateCount")
            .and_then(Value::as_u64)
            .is_some_and(|count| (1..=2).contains(&count));
    let valid = eligible
        && hardware_optimization_comparison_valid(state.inner(), &model, &profile)
        && profile.get("platform").and_then(Value::as_str) == Some(std::env::consts::OS)
        && profile.get("architecture").and_then(Value::as_str) == Some(std::env::consts::ARCH)
        && profile.get("buildId").and_then(Value::as_str) == Some(BUILD_ID)
        && profile.get("appVersion").and_then(Value::as_str) == Some(env!("CARGO_PKG_VERSION"))
        && profile.get("hardwareFingerprint").and_then(Value::as_str)
            == Some(hardware_optimization_fingerprint(state.inner()).as_str());
    Ok(if valid { Some(profile) } else { None })
}

#[tauri::command]

fn apply_hardware_optimization_profile(
    state: State<'_, BackendState>,
    model: String,
) -> Result<Value, String> {
    validate_model(&model)?;
    let profile = read_model_hardware_profile(state.inner(), &model)?.ok_or_else(|| {
        "No successful hardware comparison is available for this model.".to_string()
    })?;
    if !hardware_optimization_comparison_valid(state.inner(), &model, &profile) {
        return Err("The saved hardware comparison failed full candidate, metric, winner, or machine-identity validation.".into());
    }

    let winner = profile
        .get("winnerDevice")
        .and_then(Value::as_str)
        .filter(|device| matches!(*device, "gpu" | "cpu"))
        .ok_or_else(|| "The hardware comparison has no valid winner.".to_string())?;
    let provider = profile
        .get("candidates")
        .and_then(|v| v.get(winner))
        .and_then(|v| v.get("provider"))
        .and_then(Value::as_str)
        .ok_or_else(|| "The winning candidate has no verified provider identity.".to_string())?;
    let expected_provider = if winner == "gpu" {
        "mlx-metal-gpu"
    } else {
        "mlx-cpu-selected"
    };
    if provider != expected_provider {
        return Err(
            "The winning candidate's provider does not match its selected hardware.".into(),
        );
    }

    let fingerprint = hardware_optimization_fingerprint(state.inner());
    let source_path =
        hardware_optimization_model_path(state.inner(), "hardware-optimization-profile", &model)?;
    let created_at = profile
        .get("createdAtUnixMs")
        .and_then(Value::as_u64)
        .ok_or_else(|| "The benchmark profile has no valid creation identity.".to_string())?;
    let candidate = profile
        .get("candidates")
        .and_then(|v| v.get(winner))
        .ok_or_else(|| "The winning candidate record is missing.".to_string())?;
    let summary = candidate
        .get("summary")
        .ok_or_else(|| "The winning candidate summary is missing.".to_string())?;
    let count = candidate
        .get("measuredRunCount")
        .and_then(Value::as_u64)
        .filter(|count| (1..=7).contains(count))
        .ok_or_else(|| "The winning candidate has no valid measured sample count.".to_string())?;

    let valid = profile.get("schema").and_then(Value::as_str)
        == Some("rdt-connect-hardware-optimization-v1")
        && profile.get("optimizationEligible").and_then(Value::as_bool) == Some(true)
        && profile
            .get("successfulCandidateCount")
            .and_then(Value::as_u64)
            .is_some_and(|value| (1..=2).contains(&value))
        && profile.get("model").and_then(Value::as_str) == Some(model.as_str())
        && profile.get("platform").and_then(Value::as_str) == Some(std::env::consts::OS)
        && profile.get("architecture").and_then(Value::as_str) == Some(std::env::consts::ARCH)
        && profile.get("buildId").and_then(Value::as_str) == Some(BUILD_ID)
        && profile.get("appVersion").and_then(Value::as_str) == Some(env!("CARGO_PKG_VERSION"))
        && profile.get("hardwareFingerprint").and_then(Value::as_str) == Some(fingerprint.as_str())
        && candidate.get("provider").and_then(Value::as_str) == Some(provider)
        && summary.get("provider").and_then(Value::as_str) == Some(provider)
        && summary
            .get("providerStableAcrossAllRequests")
            .and_then(Value::as_bool)
            == Some(true)
        && candidate.get("candidateComplete").and_then(Value::as_bool) == Some(true)
        && count == 7
        && candidate
            .get("leaseReleaseConfirmed")
            .and_then(Value::as_bool)
            == Some(true)
        && candidate
            .get("warmup")
            .map(native_benchmark_run_qualification_valid)
            == Some(true)
        && native_benchmark_profile_metrics_valid(candidate)
        && candidate.get("measuredRunCount").and_then(Value::as_u64) == Some(7);

    if !valid {
        return Err(
            "The winning candidate's provider, device, samples, lease release, or hardware identity could not be verified. The previous active recommendation was left unchanged."
                .into(),
        );
    }

    // Bind the active record to this exact model-specific source profile and
    // candidate. The fingerprint distinguishes this machine's Apple GPU/CPU.
    let device_id = format!("{winner}:{provider}:{fingerprint}");
    let active = json!({
        "schema": "rdt-connect-hardware-optimization-active-v1",
        "appliedAtUnixMs": unix_epoch_millis(),
        "platform": std::env::consts::OS,
        "architecture": std::env::consts::ARCH,
        "buildId": BUILD_ID,
        "appVersion": env!("CARGO_PKG_VERSION"),
        "hardwareFingerprint": fingerprint,
        "model": model,
        "winnerDevice": winner,
        "candidateDeviceId": device_id,
        "provider": provider,
        "measuredRunCount": count,
        "candidateComplete": candidate.get("candidateComplete").and_then(Value::as_bool).unwrap_or(false),
        "leaseReleaseConfirmed": true,
        "sourceProfile": source_path.file_name().and_then(|name| name.to_str()).unwrap_or("hardware-optimization-profile"),
        "sourceProfileCreatedAtUnixMs": created_at
    });

    if validated_active_hardware_optimization_device(state.inner(), &model, &active).as_deref()
        != Some(winner)
    {
        return Err(
            "The active recommendation failed its source-profile revalidation. The previous active recommendation was left unchanged."
                .into(),
        );
    }

    let active_path =
        hardware_optimization_model_path(state.inner(), "hardware-optimization-active", &model)?;
    let active_bytes = serde_json::to_vec_pretty(&active)
        .map_err(|error| format!("Could not encode active hardware profile: {error}"))?;
    write_private_atomic(&active_path, &active_bytes)?;
    Ok(active)
}

fn validated_active_hardware_optimization_device(
    state: &BackendState,
    model: &str,
    active: &Value,
) -> Option<String> {
    let winner = active.get("winnerDevice").and_then(Value::as_str)?;
    if !matches!(winner, "gpu" | "cpu") {
        return None;
    }

    let provider = if winner == "gpu" {
        "mlx-metal-gpu"
    } else {
        "mlx-cpu-selected"
    };
    let fingerprint = hardware_optimization_fingerprint(state);
    let expected_device_id = format!("{winner}:{provider}:{fingerprint}");

    let identity_valid = active.get("schema").and_then(Value::as_str)
        == Some("rdt-connect-hardware-optimization-active-v1")
        && active.get("model").and_then(Value::as_str) == Some(model)
        && active.get("platform").and_then(Value::as_str) == Some(std::env::consts::OS)
        && active.get("architecture").and_then(Value::as_str) == Some(std::env::consts::ARCH)
        && active.get("buildId").and_then(Value::as_str) == Some(BUILD_ID)
        && active.get("appVersion").and_then(Value::as_str) == Some(env!("CARGO_PKG_VERSION"))
        && active.get("hardwareFingerprint").and_then(Value::as_str) == Some(fingerprint.as_str())
        && active.get("candidateDeviceId").and_then(Value::as_str)
            == Some(expected_device_id.as_str())
        && active.get("provider").and_then(Value::as_str) == Some(provider)
        && active.get("leaseReleaseConfirmed").and_then(Value::as_bool) == Some(true);

    if !identity_valid {
        return None;
    }

    let profile = read_model_hardware_profile(state, model).ok()??;
    if !hardware_optimization_comparison_valid(state, model, &profile) {
        return None;
    }
    let source_path =
        hardware_optimization_model_path(state, "hardware-optimization-profile", model).ok()?;
    let source_name = source_path.file_name().and_then(|name| name.to_str())?;
    let created_at = profile.get("createdAtUnixMs").and_then(Value::as_u64)?;

    if active.get("sourceProfile").and_then(Value::as_str) != Some(source_name)
        || active
            .get("sourceProfileCreatedAtUnixMs")
            .and_then(Value::as_u64)
            != Some(created_at)
        || profile.get("schema").and_then(Value::as_str)
            != Some("rdt-connect-hardware-optimization-v1")
        || profile.get("optimizationEligible").and_then(Value::as_bool) != Some(true)
        || profile
            .get("successfulCandidateCount")
            .and_then(Value::as_u64)
            .is_none_or(|count| !(1..=2).contains(&count))
        || profile.get("model").and_then(Value::as_str) != Some(model)
        || profile.get("platform").and_then(Value::as_str) != Some(std::env::consts::OS)
        || profile.get("architecture").and_then(Value::as_str) != Some(std::env::consts::ARCH)
        || profile.get("buildId").and_then(Value::as_str) != Some(BUILD_ID)
        || profile.get("appVersion").and_then(Value::as_str) != Some(env!("CARGO_PKG_VERSION"))
        || profile.get("hardwareFingerprint").and_then(Value::as_str) != Some(fingerprint.as_str())
        || profile.get("winnerDevice").and_then(Value::as_str) != Some(winner)
    {
        return None;
    }

    let candidate = profile.get("candidates")?.get(winner)?;
    let summary = candidate.get("summary")?;
    let count = candidate.get("measuredRunCount").and_then(Value::as_u64)?;
    let complete = candidate
        .get("candidateComplete")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    if count != 7
        || !complete
        || candidate.get("provider").and_then(Value::as_str) != Some(provider)
        || summary.get("provider").and_then(Value::as_str) != Some(provider)
        || summary
            .get("providerStableAcrossAllRequests")
            .and_then(Value::as_bool)
            != Some(true)
        || candidate
            .get("leaseReleaseConfirmed")
            .and_then(Value::as_bool)
            != Some(true)
        || candidate.get("measuredRunCount").and_then(Value::as_u64)
            != active.get("measuredRunCount").and_then(Value::as_u64)
        || active.get("candidateComplete").and_then(Value::as_bool) != Some(complete)
        || candidate
            .get("warmup")
            .map(native_benchmark_run_qualification_valid)
            != Some(true)
        || !native_benchmark_profile_metrics_valid(candidate)
    {
        return None;
    }

    Some(winner.to_string())
}
#[tauri::command]
fn clear_hardware_optimization_active_profile(
    state: State<'_, BackendState>,
    model: String,
) -> Result<(), String> {
    validate_model(&model)?;
    let path =
        hardware_optimization_model_path(state.inner(), "hardware-optimization-active", &model)?;
    let disabled = json!({
        "schema": "rdt-connect-hardware-optimization-active-disabled-v1",
        "disabledAtUnixMs": unix_epoch_millis(),
        "model": model,
        "reason": "A first-run comparison did not complete; use the default Automatic device policy."
    });
    let bytes = serde_json::to_vec_pretty(&disabled)
        .map_err(|error| format!("Could not encode the default hardware policy: {error}"))?;
    write_private_atomic(&path, &bytes)
}

/* RDT_INFERENCE_DEVICE_PREF_BEGIN */
fn hardware_candidate_priority_path(state: &BackendState) -> Result<PathBuf, String> {
    hardware_optimization_profile_path(state, "hardware-candidate-priority.json")
}

fn read_hardware_candidate_demotions(state: &BackendState) -> Result<Vec<String>, String> {
    let path = hardware_candidate_priority_path(state)?;
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(format!(
                "Could not read hardware candidate priority history: {error}"
            ))
        }
    };
    let metadata = fs::metadata(&path).map_err(|error| {
        format!("Could not inspect hardware candidate priority permissions: {error}")
    })?;
    if !metadata.is_file() || metadata.len() > 16_384 {
        return Err(
            "Hardware candidate priority history is not a valid small settings file.".into(),
        );
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err("Hardware candidate priority history has unsafe file permissions; it was not used or changed.".into());
        }
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("Hardware candidate priority history is invalid JSON: {error}"))?;
    if value.get("schema").and_then(Value::as_str)
        != Some("rdt-connect-hardware-candidate-priority-v1")
    {
        return Err("Hardware candidate priority history has an unsupported schema.".into());
    }
    if value
        .get("updatedAtUnixMs")
        .and_then(Value::as_u64)
        .is_none_or(|value| value == 0)
    {
        return Err("Hardware candidate priority history has no valid update timestamp.".into());
    }
    if value.get("platform").and_then(Value::as_str) != Some(std::env::consts::OS)
        || value.get("architecture").and_then(Value::as_str) != Some(std::env::consts::ARCH)
        || value.get("hardwareFingerprint").and_then(Value::as_str)
            != Some(hardware_optimization_fingerprint(state).as_str())
    {
        return Ok(Vec::new());
    }
    let stored = value
        .get("demotedDevices")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            "Hardware candidate priority history has no valid demotion list.".to_string()
        })?;
    let mut result: Vec<String> = Vec::new();
    for item in stored {
        let identity = item.as_str().ok_or_else(|| {
            "Hardware candidate priority history contains a non-string device identity.".to_string()
        })?;
        if !matches!(identity, "gpu:mlx-metal-gpu" | "cpu:mlx-cpu-selected") {
            return Err(
                "Hardware candidate priority history contains an unsupported device identity."
                    .into(),
            );
        }
        if !result.iter().any(|existing| existing == identity) {
            result.push(identity.to_string());
        }
    }
    Ok(result)
}

#[tauri::command]
fn get_hardware_candidate_demotions(state: State<'_, BackendState>) -> Result<Vec<String>, String> {
    let _guard = HARDWARE_CANDIDATE_PRIORITY_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    read_hardware_candidate_demotions(state.inner())
}

#[tauri::command]
fn record_hardware_candidate_outcome(
    state: State<'_, BackendState>,
    device: String,
    successful: bool,
) -> Result<Vec<String>, String> {
    let identity = match device.as_str() {
        "gpu" => "gpu:mlx-metal-gpu",
        "cpu" => "cpu:mlx-cpu-selected",
        _ => return Err("Hardware candidate outcome must identify gpu or cpu.".into()),
    };
    let _guard = HARDWARE_CANDIDATE_PRIORITY_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let mut demoted = read_hardware_candidate_demotions(state.inner())?;
    if successful {
        demoted.retain(|item| item != identity);
    } else if !demoted.iter().any(|item| item == identity) {
        demoted.push(identity.to_string());
    }
    let value = json!({
        "schema": "rdt-connect-hardware-candidate-priority-v1",
        "platform": std::env::consts::OS,
        "architecture": std::env::consts::ARCH,
        "hardwareFingerprint": hardware_optimization_fingerprint(state.inner()),
        "updatedAtUnixMs": unix_epoch_millis(),
        "demotedDevices": demoted
    });
    let bytes = serde_json::to_vec_pretty(&value).map_err(|error| {
        format!("Could not encode hardware candidate priority history: {error}")
    })?;
    write_private_atomic(&hardware_candidate_priority_path(state.inner())?, &bytes)?;
    Ok(demoted)
}

fn read_inference_device_preference(state: &BackendState) -> String {
    match fs::read_to_string(&state.inference_device_pref_path) {
        Ok(value) => match value.trim() {
            "gpu" => "gpu".into(),
            "cpu" => "cpu".into(),
            _ => "auto".into(),
        },
        Err(_) => "auto".into(),
    }
}

fn hardware_optimization_profile_path(state: &BackendState, name: &str) -> Result<PathBuf, String> {
    Ok(state
        .app_language_pref_path
        .parent()
        .ok_or_else(|| "Could not locate the RDT Connect settings directory.".to_string())?
        .join(name))
}

fn hardware_optimization_model_key(model: &str) -> String {
    let digest = Sha256::digest(model.as_bytes());
    digest
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn hardware_optimization_model_path(
    state: &BackendState,
    stem: &str,
    model: &str,
) -> Result<PathBuf, String> {
    validate_model(model)?;
    hardware_optimization_profile_path(
        state,
        &format!("{stem}-{}.json", hardware_optimization_model_key(model)),
    )
}

fn read_model_hardware_profile(state: &BackendState, model: &str) -> Result<Option<Value>, String> {
    let path = hardware_optimization_model_path(state, "hardware-optimization-profile", model)?;
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            // Migrate access to the previous single-profile format without deleting it.
            let legacy =
                hardware_optimization_profile_path(state, "hardware-optimization-profile.json")?;
            match fs::read(legacy) {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                Err(error) => {
                    return Err(format!(
                        "Could not read the saved hardware comparison: {error}"
                    ))
                }
            }
        }
        Err(error) => {
            return Err(format!(
                "Could not read the saved hardware comparison: {error}"
            ))
        }
    };
    let profile: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("The saved hardware comparison is invalid JSON: {error}"))?;
    if profile.get("schema").and_then(Value::as_str) != Some("rdt-connect-hardware-optimization-v1")
        || profile.get("model").and_then(Value::as_str) != Some(model)
    {
        return Ok(None);
    }
    Ok(Some(profile))
}

fn read_sysctl_value(name: &str) -> String {
    Command::new("/usr/sbin/sysctl")
        .args(["-n", name])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

fn read_command_value(program: &str, args: &[&str]) -> String {
    Command::new(program)
        .args(args)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

fn hardware_optimization_fingerprint(state: &BackendState) -> String {
    let machine = if cfg!(target_os = "macos") {
        format!(
            "{}|{}|{}|macOS-{}-{}",
            read_sysctl_value("hw.model"),
            read_sysctl_value("hw.ncpu"),
            read_sysctl_value("hw.memsize"),
            read_command_value("/usr/bin/sw_vers", &["-productVersion"]),
            read_command_value("/usr/bin/sw_vers", &["-buildVersion"])
        )
    } else {
        format!("{}|{}", std::env::consts::OS, std::env::consts::ARCH)
    };
    let backend_root = state.resource_dir.join("rdt_voice_backend");
    let runtime_files = [
        backend_root.join("rdt_voice_backend"),
        backend_root.join("_internal").join("libmlx.dylib"),
        backend_root
            .join("_internal")
            .join("mlx")
            .join("mlx.metallib"),
        backend_root
            .join("_internal")
            .join("mlx")
            .join("lib")
            .join("libmlx.dylib"),
    ];
    let runtime = runtime_files
        .iter()
        .map(|path| {
            let identity = fs::metadata(path)
                .ok()
                .map(|metadata| {
                    let modified = metadata
                        .modified()
                        .ok()
                        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                        .map(|duration| duration.as_nanos().to_string())
                        .unwrap_or_else(|| "unknown".into());
                    format!("{}:{}", metadata.len(), modified)
                })
                .unwrap_or_else(|| "missing".into());
            format!(
                "{}={identity}",
                path.file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("runtime")
            )
        })
        .collect::<Vec<_>>()
        .join("|");
    format!("{machine}|{runtime}")
}

fn read_effective_inference_device_preference(state: &BackendState, model: &str) -> String {
    // An explicit user choice always wins over a benchmark recommendation.
    let manual = read_inference_device_preference(state);
    if manual != "auto" {
        return manual;
    }

    let model_path =
        match hardware_optimization_model_path(state, "hardware-optimization-active", model) {
            Ok(path) => path,
            Err(_) => return "auto".into(),
        };
    let legacy_path =
        match hardware_optimization_profile_path(state, "hardware-optimization-active.json") {
            Ok(path) => path,
            Err(_) => return "auto".into(),
        };
    let path = if model_path.exists() {
        model_path
    } else {
        legacy_path
    };
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return "auto".into(),
    };
    let active: Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => return "auto".into(),
    };

    if active.get("schema").and_then(Value::as_str)
        == Some("rdt-connect-hardware-optimization-active-disabled-v1")
    {
        return "auto".into();
    }

    match validated_active_hardware_optimization_device(state, model, &active).as_deref() {
        // Unlike Automatic GPU-first selection, a saved recommendation is explicit:
        // do not silently fall back to another provider if its selected provider fails.
        Some("gpu") => "gpu".into(),
        Some("cpu") => "cpu".into(),
        _ => "auto".into(),
    }
}
#[tauri::command]
fn get_apple_gpu_display_label() -> String {
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "machdep.cpu.brand_string"])
            .output()
        {
            if output.status.success() {
                let brand = String::from_utf8_lossy(&output.stdout);
                let name = brand.split_whitespace().collect::<Vec<_>>().join(" ");
                if let Some(rest) = name.strip_prefix("Apple M") {
                    let digits = rest.chars().take_while(|c| c.is_ascii_digit()).count();
                    if digits > 0
                        && rest[digits..]
                            .trim()
                            .split_whitespace()
                            .all(|s| matches!(s, "Pro" | "Max" | "Ultra"))
                    {
                        return format!("{name} GPU · Metal");
                    }
                }
            }
        }
    }
    "Apple GPU · Metal".to_string()
}

fn get_or_create_member_since_millis(app_language_pref_path: &Path) -> Option<u64> {
    let parent = app_language_pref_path.parent()?;
    let path = parent.join("member-since.json");

    match fs::symlink_metadata(&path) {
        Ok(metadata) => {
            if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 4096 {
                return None;
            }
            let bytes = fs::read(&path).ok()?;
            let value = serde_json::from_slice::<Value>(&bytes).ok()?;
            if value.get("schema").and_then(Value::as_str) != Some("rdt-connect-member-since-v1") {
                return None;
            }
            value.get("firstUsedAtMillis").and_then(Value::as_u64)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let timestamp = u64::try_from(unix_epoch_millis()).ok()?;
            let bytes = serde_json::to_vec(&json!({
                "schema": "rdt-connect-member-since-v1",
                "firstUsedAtMillis": timestamp
            }))
            .ok()?;
            write_private_atomic(&path, &bytes).ok()?;
            Some(timestamp)
        }
        Err(_) => None,
    }
}

#[tauri::command]
fn get_lifetime_statistics(state: State<'_, BackendState>) -> Value {
    let member_since = get_or_create_member_since_millis(&state.app_language_pref_path);
    let default_value = || {
        json!({
            "schema": "rdt-connect-lifetime-statistics-v1",
            "memberSinceAtMillis": member_since,
            "firstTranscriptionAt": null,
            "lastTranscriptionAt": null,
            "totalTranscriptions": 0,
            "totalAudioSeconds": 0.0,
            "totalUploadBytes": 0,
            "totalInferenceMilliseconds": 0.0,
            "hardwareUsage": {},
            "modelUsage": {}
        })
    };
    let Some(parent) = state.app_language_pref_path.parent() else {
        return default_value();
    };
    let path = parent.join("lifetime-statistics.json");
    let Ok(metadata) = fs::symlink_metadata(&path) else {
        return default_value();
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 1_048_576 {
        return default_value();
    }
    let Ok(bytes) = fs::read(&path) else {
        return default_value();
    };
    let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
        return default_value();
    };
    if value.get("schema").and_then(Value::as_str) == Some("rdt-connect-lifetime-statistics-v1") {
        let mut value = value;
        if let Some(object) = value.as_object_mut() {
            object.insert("memberSinceAtMillis".into(), json!(member_since));
        }
        value
    } else {
        default_value()
    }
}

#[tauri::command]
fn get_inference_device_preference(state: State<'_, BackendState>) -> String {
    read_inference_device_preference(state.inner())
}

#[tauri::command]
fn set_inference_device_preference(
    state: State<'_, BackendState>,
    device: String,
) -> Result<String, String> {
    let normalized = match device.trim() {
        "auto" => "auto",
        "gpu" => "gpu",
        "cpu" => "cpu",
        _ => return Err("Inference device must be auto, gpu, or cpu.".into()),
    };
    write_private_atomic(&state.inference_device_pref_path, normalized.as_bytes())?;
    Ok(normalized.into())
}
/* RDT_INFERENCE_DEVICE_PREF_END */

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

const UPDATE_TRAY_IDLE: u8 = 0;
const UPDATE_TRAY_AVAILABLE: u8 = 1;
const UPDATE_TRAY_RESTART: u8 = 2;
const UPDATE_TRAY_CHECKING: u8 = 3;
const UPDATE_TRAY_DOWNLOADING: u8 = 4;
const UPDATE_TRAY_UP_TO_DATE: u8 = 5;
const UPDATE_TRAY_ERROR: u8 = 6;
const UPDATE_TRAY_OFFLINE: u8 = 7;
const UPDATE_TRAY_UP_TO_DATE_AUTO: u8 = 8;
const UPDATE_TRAY_UP_TO_DATE_MANUAL: u8 = 9;

static UPDATE_TRAY_STATE: AtomicU8 = AtomicU8::new(UPDATE_TRAY_IDLE);
static UPDATE_TRAY_MESSAGE: OnceLock<Mutex<String>> = OnceLock::new();
static UPDATE_TRAY_MENU_ITEM: OnceLock<Mutex<Option<MenuItem<tauri::Wry>>>> = OnceLock::new();

fn update_tray_menu_label(app: &AppHandle, text: String, enabled: bool) {
    let item_cell = UPDATE_TRAY_MENU_ITEM.get_or_init(|| Mutex::new(None));

    let item = item_cell.lock().ok().and_then(|guard| guard.clone());
    if let Some(item) = item {
        let app = app.clone();
        let _ = app.run_on_main_thread(move || {
            if let Err(error) = item.set_text(&text) {
                eprintln!("[RDT Connect] Could not update tray label: {error}");
            }
            if let Err(error) = item.set_enabled(enabled) {
                eprintln!("[RDT Connect] Could not update tray action state: {error}");
            }
        });
    }
}

#[tauri::command]
fn set_update_indicator(
    app: AppHandle,
    state: Option<String>,
    display_text: Option<String>,
) -> Result<(), String> {
    let state_code = match state.as_deref() {
        Some("available") => UPDATE_TRAY_AVAILABLE,
        Some("restart") => UPDATE_TRAY_RESTART,
        Some("checking") => UPDATE_TRAY_CHECKING,
        Some("downloading") => UPDATE_TRAY_DOWNLOADING,
        Some("up-to-date") => UPDATE_TRAY_UP_TO_DATE,
        Some("up-to-date-auto") => UPDATE_TRAY_UP_TO_DATE_AUTO,
        Some("up-to-date-manual") => UPDATE_TRAY_UP_TO_DATE_MANUAL,
        Some("error") => UPDATE_TRAY_ERROR,
        Some("offline") => UPDATE_TRAY_OFFLINE,
        _ => UPDATE_TRAY_IDLE,
    };

    if let Some(text) = display_text {
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        let text: String = text.chars().take(100).collect();
        if let Ok(mut message) = UPDATE_TRAY_MESSAGE
            .get_or_init(|| Mutex::new(String::new()))
            .lock()
        {
            *message = text;
        }
    }

    let previous = UPDATE_TRAY_STATE.swap(state_code, Ordering::SeqCst);
    let message = UPDATE_TRAY_MESSAGE
        .get()
        .and_then(|value| value.lock().ok().map(|text| text.clone()))
        .unwrap_or_default();
    let enabled = !matches!(state_code, UPDATE_TRAY_CHECKING | UPDATE_TRAY_DOWNLOADING);

    if previous == state_code {
        let fallback = match state_code {
            UPDATE_TRAY_CHECKING => "Checking for Updates…",
            UPDATE_TRAY_AVAILABLE => "Download Update",
            UPDATE_TRAY_DOWNLOADING => "Downloading Update…",
            UPDATE_TRAY_RESTART => "Restart to Update",
            UPDATE_TRAY_UP_TO_DATE => "Up to Date",
            UPDATE_TRAY_UP_TO_DATE_AUTO => "RDT Connect is up to date",
            UPDATE_TRAY_UP_TO_DATE_MANUAL => "Check for Update",
            UPDATE_TRAY_ERROR => "Update Check Failed",
            UPDATE_TRAY_OFFLINE => "No internet connection. Try again when online.",
            _ => "Check for Updates",
        };
        let label = if message.is_empty() {
            fallback.to_string()
        } else {
            message
        };
        update_tray_menu_label(&app, label, enabled);
    } else {
        refresh_tray_menu(&app);
    }

    // Update arrow lifecycle:
    // available = downward arrow; downloading/checking = normal icon;
    // restart = upward arrow after the update has been installed.
    #[cfg(target_os = "macos")]
    {
        if let Some(tray) = app.tray_by_id("rdt-connect-menu-bar") {
            let dark_appearance = std::process::Command::new("/usr/bin/defaults")
                .args(["read", "-g", "AppleInterfaceStyle"])
                .output()
                .map(|output| {
                    output.status.success()
                        && String::from_utf8_lossy(&output.stdout)
                            .trim()
                            .eq_ignore_ascii_case("dark")
                })
                .unwrap_or(false);

            let (image, is_template) = match state_code {
                UPDATE_TRAY_AVAILABLE if dark_appearance => (
                    tauri::include_image!("icons/rdt-menu-bar-update-light-down-arrow.png"),
                    true,
                ),
                UPDATE_TRAY_AVAILABLE => (
                    tauri::include_image!("icons/rdt-menu-bar-update-dark-down-arrow.png"),
                    true,
                ),
                UPDATE_TRAY_RESTART if dark_appearance => (
                    tauri::include_image!("icons/rdt-menu-bar-update-light-arrow-upward.png"),
                    true,
                ),
                UPDATE_TRAY_RESTART => (
                    tauri::include_image!("icons/rdt-menu-bar-update-dark-arrow-upward.png"),
                    true,
                ),
                _ => (
                    tauri::include_image!("icons/rdt-menu-bar-template.png"),
                    true,
                ),
            };
            tray.set_icon_with_as_template(Some(image), is_template)
                .map_err(|error| error.to_string())?;
        }
    }

    Ok(())
}

#[cfg(target_os = "macos")]
#[derive(Clone)]
struct ConnectMemoryProcess {
    pid: u32,
    ppid: u32,
    pgid: u32,
    rss_kb: u64,
    command: String,
}

#[cfg(target_os = "macos")]
static CONNECT_MEMORY_PEAK_KB: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

#[cfg(target_os = "macos")]
fn connect_memory_process_snapshot() -> Result<Vec<ConnectMemoryProcess>, String> {
    let output = Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,pgid=,rss=,command="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|e| format!("Could not sample process memory: {e}"))?;
    if !output.status.success() {
        return Err("Process memory sampling failed.".into());
    }
    let rows: Vec<_> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let f: Vec<_> = line.split_whitespace().collect();
            if f.len() < 5 {
                return None;
            }
            Some(ConnectMemoryProcess {
                pid: f[0].parse().ok()?,
                ppid: f[1].parse().ok()?,
                pgid: f[2].parse().ok()?,
                rss_kb: f[3].parse().ok()?,
                command: f[4..].join(" "),
            })
        })
        .collect();
    if rows.is_empty() {
        return Err("No process memory data was returned.".into());
    }
    Ok(rows)
}

#[tauri::command]
fn get_connect_memory(state: State<'_, BackendState>) -> Result<serde_json::Value, String> {
    #[cfg(target_os = "macos")]
    {
        let rows = connect_memory_process_snapshot()?;
        let app_pid = std::process::id();
        if !rows.iter().any(|p| p.pid == app_pid) {
            return Err("Could not identify the RDT Connect process.".into());
        }
        let (backend_group, backend_pid) = {
            let inner = state
                .inner
                .lock()
                .map_err(|_| "Backend process state is unavailable.".to_string())?;
            (
                inner.process_group,
                inner.child.as_ref().map(|child| child.id()),
            )
        };
        let mut owned = std::collections::HashSet::new();
        owned.insert(app_pid);
        if let Some(pid) = backend_pid {
            owned.insert(pid);
        }
        loop {
            let before = owned.len();
            for p in &rows {
                if owned.contains(&p.ppid) {
                    owned.insert(p.pid);
                }
            }
            if owned.len() == before {
                break;
            }
        }
        let current_kb = rows
            .iter()
            .filter(|p| {
                let exe = p.command.split_whitespace().next().unwrap_or("");
                let command = p.command.to_ascii_lowercase();
                let sampler = exe == "ps" || exe == "/bin/ps";
                let external_browser = ["google chrome", "chrome helper", "firefox", "chromium"]
                    .iter()
                    .any(|name| command.contains(name));
                let app_webview = p.command.contains("com.rolandoristech.rdtconnect");
                !sampler
                    && !external_browser
                    && (owned.contains(&p.pid) || backend_group == Some(p.pgid) || app_webview)
            })
            .fold(0u64, |sum, p| sum.saturating_add(p.rss_kb));
        if current_kb == 0 {
            return Err("RDT Connect memory measurement was empty.".into());
        }
        let peak_kb = CONNECT_MEMORY_PEAK_KB
            .fetch_max(current_kb, Ordering::Relaxed)
            .max(current_kb);
        Ok(json!({
            "currentMb": current_kb as f64 / 1024.0,
            "peakMb": peak_kb as f64 / 1024.0
        }))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = state;
        Err("The RDT Connect RAM meter is currently available on macOS only.".into())
    }
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
        .plugin(
            tauri_plugin_updater::Builder::new()
                .default_version_comparator(|current, remote| {
                    updater_version_policy::candidate_is_acceptable(
                        &current.to_string(),
                        BUILD_ID,
                        &remote.version.to_string(),
                    )
                })
                .build(),
        )
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
                let menu_locale = read_app_language(&state);
                let app_updates_label = tray_text(
                    &menu_locale, "msg.check_for_updates.f26f3275",
                    "Check for updates", &[],
                );
                let app_quit_label = tray_text(
                    &menu_locale, "msg.quit_rdt_connect.9dcc650d",
                    "Quit RDT Connect", &[],
                );
                let close_label = tray_text(
                    &menu_locale, "msg.close.7d9eb7ac", "Close", &[],
                );

                let app_about = MenuItem::with_id(
                    app,
                    "about-app",
                    &tray_text(&menu_locale, "msg.about_rdt_connect.1d22", "About RDT Connect", &[]),
                    true,
                    None::<&str>,
                )?;
                let app_updates = MenuItem::with_id(
                    app,
                    "app-menu-check-updates",
                    &app_updates_label,
                    true,
                    None::<&str>,
                )?;
                let app_separator = PredefinedMenuItem::separator(app)?;
                let app_quit =
                    PredefinedMenuItem::quit(app, Some(&app_quit_label))?;
                let app_submenu = Submenu::with_items(
                    app,
                    "RDT Connect",
                    true,
                    &[&app_about, &app_updates, &app_separator, &app_quit],
                )?;

                let file_close =
                    PredefinedMenuItem::close_window(app, Some(&close_label))?;
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
                        if let Err(error) = show_update_window(app, true) {
                            eprintln!("[RDT Connect] Could not open updater window: {error}");
                        }
                    }
                    "about-app" => {
                        if let Err(error) = app.run_on_main_thread(|| show_about_panel_on_top()) {
                            eprintln!("[RDT Connect] Could not raise About panel: {error}");
                        }
                    },
                    _ => {}
                }
            });

            let menu = build_tray_menu(app.handle(), &state).map_err(std::io::Error::other)?;
            // macOS: black-on-transparent template image so the menu bar tints it for
            // light/dark appearance; the Dock icon stays full color. Other platforms keep
            // the existing white icon unchanged.
            #[cfg(target_os = "macos")]
            let (tray_icon, tray_icon_is_template) = (
                tauri::include_image!("icons/rdt-menu-bar-template.png"),
                true,
            );
            #[cfg(not(target_os = "macos"))]
            let (tray_icon, tray_icon_is_template) = (
                tauri::include_image!("icons/rdt-menu-bar-icon.png"),
                false,
            );
            TrayIconBuilder::with_id("rdt-connect-menu-bar")
                .icon(tray_icon)
                .icon_as_template(tray_icon_is_template)
                .menu(&menu)
                .tooltip("RDT Connect")
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "tray-about-app" => {
                        if let Err(error) = app.run_on_main_thread(|| show_about_panel_on_top()) {
                            eprintln!("[RDT Connect] Could not raise About panel: {error}");
                        }
                    },
                    "open-window" => show_main_window(app),
                    "server-status" | "stop-server" => {
                        if let Some(state) = app.try_state::<BackendState>() {
                            if let Err(error) = stop_owned_and_orphan_backends(&state) {
                                eprintln!("[RDT Connect] Menu-bar Stop could not confirm cleanup: {error}");
                                let _ = app.emit("rdt-connect-stop-failed", error);
                            } else {
                                let _ = app.emit("rdt-backend-state-changed", "stopped");
                            }
                        }
                        refresh_tray_menu(app);
                    }
                    "open-voice" => {
                        // Use the native secure handoff without activating the main window.
                        if let Some(state) = app.try_state::<BackendState>() {
                            let offline_only = read_offline_only_mode(&state);
                            match open_voice_window(
                                app.clone(),
                                state,
                                None,
                                None,
                                Some(offline_only),
                            ) {
                                Ok(offline) => eprintln!(
                                    "[RDT Connect] RDT Voice browser launch requested; offline copy: {offline}"
                                ),
                                Err(error) => eprintln!(
                                    "[RDT Connect] Could not open RDT Voice from tray: {error}"
                                ),
                            }
                        } else {
                            eprintln!(
                                "[RDT Connect] Backend state unavailable for tray browser launch."
                            );
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
                                        let _ = start_app.emit("rdt-tray-start-failed", error);
                                    } else {
                                        let _ = start_app.emit("rdt-backend-state-changed", "running");
                                    }
                                }
                                Ok((choice, Ok(_))) => {
                                    let _ = start_app.emit("rdt-tray-model-needs-download", choice.model);
                                }
                                Ok((_choice, Err(error))) => {
                                    let _ = start_app.emit("rdt-tray-start-failed", error);
                                }
                                Err(error) => {
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
                    "update-app" => {
                        if let Err(error) = show_update_window(app, true) {
                            eprintln!("[RDT Connect] Could not open updater popup: {error}");
                        }
                    },
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
            get_app_build_info, get_backend_status, get_connect_memory, get_platform_model_catalog, get_backend_connection_status, open_backend_log, get_recent_backend_log, get_app_diagnostics, get_orphaned_backends, stop_orphaned_backend,
            get_tray_location_name,
            get_keep_in_menu_bar, set_keep_in_menu_bar, set_app_language, set_update_indicator, remember_recent_server_choice, start_backend, stop_backend, list_available_browsers, get_preferred_browser, set_preferred_browser, set_offline_only_mode,
            record_fireworks_activation, open_voice_window,
            get_model_storage_status, set_model_storage_locations, reveal_model_storage_folder, pick_model_storage_directory,
            get_model_download_status, start_model_download, cancel_model_download, delete_rdt_model_caches, open_easywhisper_model_manager,
            get_local_https_status, pick_tls_file, setup_local_https_certificate, link_local_https_certificate, trust_local_https_ca, remove_rdt_https_trust, set_local_transport,
            get_tailscale_status, get_remote_access_diagnostics, enable_private_access, generate_pairing_qr,
            list_pair_requests, approve_pair_request, deny_pair_request,
            list_paired_devices, revoke_paired_device, open_about_link, open_windows_installation_guide, open_youtube_channel, get_apple_gpu_display_label, get_lifetime_statistics, get_server_token, get_inference_device_preference, set_inference_device_preference, set_native_benchmark_starting, is_hardware_optimization_supported, is_first_run_hardware_optimization_required, start_hardware_candidate_backend, stop_hardware_candidate_backend, run_hardware_native_benchmark, get_native_benchmark_diagnostics, finalize_hardware_optimization, get_hardware_optimization_profile, apply_hardware_optimization_profile, clear_hardware_optimization_active_profile, cancel_native_benchmark, get_hardware_candidate_demotions, record_hardware_candidate_outcome
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
#[cfg(test)]
mod transcription_diagnostics_v2_tests {
    use super::*;

    #[test]
    fn accepts_v2_and_excludes_unapproved_private_fields() {
        let payload = json!({
            "schema": "rdt-transcription-diagnostics-v2",
            "runtime": {"provider": "mlx", "model": "mlx-community/whisper-small-mlx", "device": "gpu", "secret": "DO_NOT_EXPORT"},
            "events": [{"job_id": "job-123", "stage": "inference", "error_type": "MlxRuntimeError", "raw_error": "DO_NOT_EXPORT"}],
            "jobs": []
        });
        let report = format_transcription_diagnostics_v2(200, Some(&payload)).join("\n");
        assert!(report.contains("rdt-transcription-diagnostics-v2"));
        assert!(report.contains("provider=mlx"));
        assert!(report.contains("stage=inference"));
        assert!(!report.contains("DO_NOT_EXPORT"));
    }

    #[test]
    fn identifies_auth_and_missing_route_failures() {
        assert!(format_transcription_diagnostics_v2(401, None)
            .join(" ")
            .contains("authentication"));
        assert!(format_transcription_diagnostics_v2(404, None)
            .join(" ")
            .contains("does not expose"));
    }
}

#[cfg(test)]
mod native_benchmark_tests {
    use super::*;

    #[test]
    fn benchmark_segment_timing_is_optional_for_text_scoring() {
        let absent = json!({"text": "recognized words"});
        assert_eq!(native_benchmark_segment_metadata(&absent), (0, false));

        let untimed = json!({"segments": [{"text": "recognized words"}]});
        assert_eq!(native_benchmark_segment_metadata(&untimed), (1, false));

        let timed = json!({"segments": [
            {"text": "recognized words", "start": 0.0, "end": 2.0}
        ]});
        assert_eq!(native_benchmark_segment_metadata(&timed), (1, true));
    }

    #[test]
    fn accuracy_matches_nfc_casefold_and_punctuation_policy() {
        let result = native_benchmark_accuracy("Straße, CAFÉ!", "STRASSE CAFÉ");
        assert_eq!(result["normalized_cer"], json!(0.0));
        assert_eq!(result["wer"], json!(0.0));
        assert!(result["strict_cer"].as_f64().unwrap() > 0.0);
    }

    #[test]
    fn supplementary_cjk_reference_disables_wer() {
        let result = native_benchmark_accuracy("𠀀 test", "𠀀 test");
        assert!(result["wer"].is_null());
        assert_eq!(result["normalized_cer"], json!(0.0));
    }

    #[test]
    fn report_timestamp_is_utc_iso8601() {
        let timestamp = native_benchmark_utc_timestamp();
        assert!(timestamp.ends_with('Z'));
        assert_eq!(timestamp.len(), 20);
        assert_eq!(&timestamp[4..5], "-");
        assert_eq!(&timestamp[7..8], "-");
        assert_eq!(&timestamp[10..11], "T");
    }

    #[test]
    fn fixture_modes_select_their_own_user_voice_recordings() {
        let first = native_benchmark_fixture_spec("first-run", None).unwrap();
        assert_eq!(first.directory, "first-run-benchmark");
        assert_eq!(first.audio_filename, "audio.wav");
        assert_eq!(first.fixture_id, "rdt-user-voice-first-run-12s-v1");
        assert_eq!(first.expected_word_count, 28);
        assert!(
            (first.minimum_duration_seconds..=first.maximum_duration_seconds).contains(&12.0294375)
        );

        let manual_short = native_benchmark_fixture_spec("manual", Some("12s")).unwrap();
        assert_eq!(manual_short.directory, "first-run-benchmark");
        assert_eq!(manual_short.audio_filename, "audio.wav");
        assert_eq!(manual_short.expected_sha256, first.expected_sha256);

        let manual = native_benchmark_fixture_spec("manual", Some("21s")).unwrap();
        assert_eq!(manual.directory, "benchmark");
        assert_eq!(manual.audio_filename, "user-voice-21s.wav");
        assert_eq!(manual.fixture_id, "rdt-user-voice-manual-21s-v1");
        assert_eq!(manual.expected_word_count, 50);
        assert!(
            (manual.minimum_duration_seconds..=manual.maximum_duration_seconds).contains(&20.72575)
        );
        assert_ne!(first.expected_sha256, manual.expected_sha256);
        assert!(native_benchmark_fixture_spec("", None).is_err());
        assert!(native_benchmark_fixture_spec("preview", None).is_err());
        assert!(native_benchmark_fixture_spec("librispeech", None).is_err());
    }
}

#[cfg(test)]
mod windows_console_window_tests {
    const SOURCE: &str = include_str!("main.rs");

    fn function_body<'a>(source: &'a str, signature: &str) -> &'a str {
        let start = source
            .find(signature)
            .unwrap_or_else(|| panic!("{signature} not found in main.rs"));
        let rest = &source[start..];
        let end = rest.find("\n}\n").expect("end of function not found");
        &rest[..end]
    }

    #[test]
    fn taskkill_is_launched_only_through_the_hidden_helper() {
        let needle = concat!("Command::new(\"", "taskkill.exe", "\")");
        let helper = function_body(SOURCE, "fn windows_taskkill_command(");
        assert_eq!(SOURCE.matches(needle).count(), 1);
        assert!(helper.contains(needle));
        assert!(helper.contains(".creation_flags(WINDOWS_CREATE_NO_WINDOW)"));
        assert!(SOURCE.contains("const WINDOWS_CREATE_NO_WINDOW: u32 = 0x0800_0000;"));
        let caller = function_body(SOURCE, "fn send_signal_checked(target: i32, signal: i32)");
        assert!(caller.contains("windows_taskkill_command(pid, is_tree, signal == SIGNAL_KILL)"));
    }

    #[test]
    fn windows_loopback_san_regex_is_correct() {
        assert!(
            SOURCE.contains(r#"IP\s*Address\s*[=:]\s*127\.0\.0\.1"#),
            "Windows SAN validator must match the loopback IP correctly"
        );
    }
}

#[cfg(test)]
mod native_benchmark_guard_tests {
    use super::*;

    fn process(pid: u32, pgid: u32, started: &str) -> ProcessSnapshot {
        ProcessSnapshot {
            pid,
            ppid: 1,
            pgid,
            stat: "S".into(),
            started: started.into(),
            command: format!("/bundled/rdt_voice_backend --worker {pid}"),
        }
    }

    #[test]
    fn candidate_listener_accepts_only_its_captured_group() {
        let leader = process(410, 410, "Sat Nov 7 21:00:00 2026");
        let worker = process(411, 410, "Sat Nov 7 21:00:01 2026");
        assert_eq!(
            candidate_listener_group_ownership(&leader, &[410, 411], &[leader.clone(), worker]),
            Ok(true)
        );
    }

    #[test]
    fn candidate_listener_rejects_foreign_or_unseen_owner() {
        let leader = process(410, 410, "Sat Nov 7 21:00:00 2026");
        let foreign = process(900, 900, "Sat Nov 7 21:00:02 2026");
        let error = candidate_listener_group_ownership(&leader, &[900], &[leader.clone(), foreign])
            .unwrap_err();
        assert!(error.contains("outside candidate process group"));
        assert!(candidate_listener_group_ownership(&leader, &[999], &[leader.clone()]).is_err());
    }

    #[test]
    fn candidate_listener_rejects_pid_reuse_and_empty_listener_is_not_ready() {
        let identity = process(410, 410, "Sat Nov 7 21:00:00 2026");
        let reused = process(410, 410, "Sat Nov 7 22:00:00 2026");
        assert!(candidate_listener_group_ownership(&identity, &[410], &[reused]).is_err());
        assert_eq!(
            candidate_listener_group_ownership(&identity, &[], &[identity.clone()]),
            Ok(false)
        );
    }

    #[test]
    fn true_rtf_is_inference_over_actual_audio_duration() {
        assert!((native_benchmark_realtime_factor(2.0, 20.0).unwrap() - 0.1).abs() < 1e-12);
        assert!(native_benchmark_realtime_factor(2.0, 0.0).is_none());
        assert!(native_benchmark_realtime_factor(0.0, 20.0).is_none());
        assert!(native_benchmark_realtime_factor(f64::NAN, 20.0).is_none());
        assert!(native_benchmark_realtime_factor(2.0, f64::INFINITY).is_none());
    }

    #[test]
    fn seven_value_median_is_independent_of_run_order() {
        assert_eq!(
            native_benchmark_median_seven(&[0.9, 0.4, 0.2, 0.8, 0.5, 0.7, 0.6]),
            Some(0.6)
        );
        assert!(native_benchmark_median_seven(&[0.1, 0.2]).is_none());
    }

    #[test]
    fn normalized_and_strict_cer_are_both_available() {
        let quality = native_benchmark_accuracy("hello world", "hello, world!");
        assert_eq!(quality["normalized_cer"].as_f64(), Some(0.0));
        assert!(quality["strict_cer"].as_f64().unwrap() > 0.0);
    }

    #[test]
    fn profile_validator_recomputes_every_seven_run_median() {
        let runs: Vec<Value> = (0..7)
            .map(|index| {
                let i = index as f64;
                let duration = 20.0 + i * 0.1;
                let inference = 1.0 + i * 0.01;
                serde_json::json!({
                    "inferenceMs": inference * 1000.0,
                    "inferenceSeconds": inference,
                    "audioDurationSeconds": duration,
                    "realtimeFactor": inference / duration,
                    "wordErrorRate": 0.01 + i * 0.001,
                    "characterErrorRate": 0.02 + i * 0.001,
                    "strictCharacterErrorRate": 0.03 + i * 0.001
                })
            })
            .collect();

        let metric =
            |key: &str| -> Vec<f64> { runs.iter().map(|run| run[key].as_f64().unwrap()).collect() };
        let median = |key: &str| native_benchmark_median_seven(&metric(key)).unwrap();
        let mut times = metric("inferenceMs");
        times.sort_by(|a, b| a.partial_cmp(b).unwrap());

        let profile = serde_json::json!({
            "runs": runs.clone(),
            "measuredRunCount": 7,
            "summary": {
                "measuredRunCount": 7,
                "medianInferenceMs": median("inferenceMs"),
                "minimumInferenceMs": times[0],
                "maximumInferenceMs": times[6],
                "medianWordErrorRate": median("wordErrorRate"),
                "medianCharacterErrorRate": median("characterErrorRate"),
                "medianStrictCharacterErrorRate": median("strictCharacterErrorRate"),
                "medianInferenceSeconds": median("inferenceSeconds"),
                "audioDurationSeconds": median("audioDurationSeconds"),
                "medianRealtimeFactor": median("realtimeFactor")
            }
        });
        assert!(native_benchmark_profile_metrics_valid(&profile));

        for (key, wrong) in [
            ("medianInferenceMs", 2000.0),
            ("medianInferenceSeconds", 2.0),
            ("audioDurationSeconds", 25.0),
            ("medianRealtimeFactor", 0.9),
            ("medianWordErrorRate", 0.09),
            ("medianCharacterErrorRate", 0.5),
            ("medianStrictCharacterErrorRate", 0.5),
            ("minimumInferenceMs", 900.0),
            ("maximumInferenceMs", 2000.0),
        ] {
            let mut tampered = profile.clone();
            tampered["summary"][key] = serde_json::json!(wrong);
            assert!(
                !native_benchmark_profile_metrics_valid(&tampered),
                "tampered summary field {key} was accepted"
            );
        }

        let mut bad_run = profile.clone();
        bad_run["runs"][3]["realtimeFactor"] = serde_json::json!(0.9);
        assert!(!native_benchmark_profile_metrics_valid(&bad_run));

        let mut bad_precision = profile.clone();
        bad_precision["runs"][3]["inferenceMs"] = serde_json::json!(3000.0);
        assert!(!native_benchmark_profile_metrics_valid(&bad_precision));
    }
}

#[cfg(test)]
mod model_storage_atomic_write_tests {
    use super::*;
    #[test]
    fn running_server_allows_appending_locations() {
        let old = vec!["/models/primary".to_string()];
        let new = vec![
            "/models/primary".to_string(),
            "/models/secondary".to_string(),
        ];
        assert!(model_storage_append_only_change_allowed(
            &old,
            "/models/primary",
            &new,
            "/models/primary"
        ));
    }

    #[test]
    fn running_server_rejects_unchanged_and_removed_locations() {
        let old = vec![
            "/models/primary".to_string(),
            "/models/secondary".to_string(),
        ];
        assert!(!model_storage_append_only_change_allowed(
            &old,
            "/models/primary",
            &old,
            "/models/primary"
        ));
        let removed = vec!["/models/primary".to_string()];
        assert!(!model_storage_append_only_change_allowed(
            &old,
            "/models/primary",
            &removed,
            "/models/primary"
        ));
    }

    #[test]
    fn running_server_rejects_reordering_existing_locations() {
        let old = vec![
            "/models/primary".to_string(),
            "/models/secondary".to_string(),
        ];
        let reordered = vec![
            "/models/secondary".to_string(),
            "/models/primary".to_string(),
            "/models/third".to_string(),
        ];
        assert!(!model_storage_append_only_change_allowed(
            &old,
            "/models/primary",
            &reordered,
            "/models/primary"
        ));
    }

    #[test]
    fn running_server_rejects_download_destination_changes() {
        let old = vec!["/models/primary".to_string()];
        let new = vec![
            "/models/primary".to_string(),
            "/models/secondary".to_string(),
        ];
        assert!(!model_storage_append_only_change_allowed(
            &old,
            "/models/primary",
            &new,
            "/models/secondary"
        ));
    }

    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "rdt-storage-write-{}-{nonce}-{label}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn creates_and_reloads_ordered_storage_config() {
        let base = fixture("create");
        let config = base.join("model-storage.json");
        let payload = br#"{"locations":["/tmp/first","/tmp/second"]}"#;
        write_model_storage_config_atomic(&config, payload).unwrap();
        let loaded: ModelStorageConfig =
            serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
        assert_eq!(
            loaded.locations,
            vec!["/tmp/first".to_string(), "/tmp/second".to_string()]
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn replaces_existing_storage_config() {
        let base = fixture("replace");
        let config = base.join("model-storage.json");
        fs::write(&config, br#"{"locations":["/old"]}"#).unwrap();
        let payload = br#"{"locations":["/new"]}"#;
        write_model_storage_config_atomic(&config, payload).unwrap();
        assert_eq!(fs::read(&config).unwrap(), payload);
        let leftovers: Vec<_> = fs::read_dir(&base)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temporary files remain: {leftovers:?}"
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn rename_failure_preserves_destination_and_cleans_temp() {
        let base = fixture("rename-failure");
        let destination = base.join("existing-directory");
        fs::create_dir(&destination).unwrap();
        fs::write(destination.join("keep.txt"), b"preserve").unwrap();

        assert!(write_model_storage_config_atomic(&destination, b"new-config").is_err());
        assert_eq!(fs::read(destination.join("keep.txt")).unwrap(), b"preserve");
        let leftovers: Vec<_> = fs::read_dir(&base)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temporary files remain: {leftovers:?}"
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn saved_config_has_private_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let base = fixture("permissions");
        let config = base.join("model-storage.json");
        write_model_storage_config_atomic(&config, br#"{"locations":[]}"#).unwrap();
        assert_eq!(
            fs::metadata(&config).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(base).unwrap();
    }
}

#[cfg(test)]
mod model_cache_deletion_path_tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "rdt-mlx-delete-{}-{nonce}-{label}",
            std::process::id()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn accepts_a_real_direct_repository_directory() {
        let base = fixture("valid");
        let hub = base.join("hub");
        let repo = hub.join("models--org--model");
        fs::create_dir_all(&repo).unwrap();
        assert_eq!(
            canonical_mlx_cache_repository(&hub, &repo).unwrap(),
            fs::canonicalize(&repo).unwrap()
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn rejects_a_repository_outside_the_hub() {
        let base = fixture("outside");
        let hub = base.join("hub");
        let repo = base.join("outside-repo");
        fs::create_dir_all(&hub).unwrap();
        fs::create_dir_all(&repo).unwrap();
        assert!(canonical_mlx_cache_repository(&hub, &repo).is_err());
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn rejects_a_non_directory_repository_entry() {
        let base = fixture("file");
        let hub = base.join("hub");
        fs::create_dir_all(&hub).unwrap();
        let repo = hub.join("not-a-directory");
        fs::write(&repo, b"fixture").unwrap();
        assert!(canonical_mlx_cache_repository(&hub, &repo).is_err());
        fs::remove_dir_all(base).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn rejects_a_symlinked_repository_and_preserves_its_target() {
        use std::os::unix::fs::symlink;
        let base = fixture("symlink");
        let hub = base.join("hub");
        let target = base.join("outside-target");
        let repo = hub.join("models--org--model");
        fs::create_dir_all(&hub).unwrap();
        fs::create_dir_all(&target).unwrap();
        fs::write(target.join("keep.txt"), b"keep").unwrap();
        symlink(&target, &repo).unwrap();

        assert!(canonical_mlx_cache_repository(&hub, &repo).is_err());
        assert!(target.join("keep.txt").is_file());
        fs::remove_dir_all(base).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn rejects_a_symlinked_hub() {
        use std::os::unix::fs::symlink;
        let base = fixture("hub-link");
        let target = base.join("outside-hub");
        let hub = base.join("hub");
        let repo = hub.join("models--org--model");
        fs::create_dir_all(&target).unwrap();
        fs::create_dir_all(target.join("models--org--model")).unwrap();
        symlink(&target, &hub).unwrap();

        assert!(canonical_mlx_cache_repository(&hub, &repo).is_err());
        assert!(target.join("models--org--model").is_dir());
        fs::remove_dir_all(base).unwrap();
    }
}

#[cfg(test)]
mod ggml_companion_cleanup_tests {
    use super::remove_regular_model_companion;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "rdt-ggml-companion-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    #[test]
    fn removes_regular_companion_file() {
        let path = fixture("regular");
        fs::write(&path, b"verified companion").unwrap();
        assert_eq!(remove_regular_model_companion(&path).unwrap(), true);
        assert!(!path.exists());
    }

    #[test]
    fn missing_companion_is_a_safe_noop() {
        let path = fixture("missing");
        assert_eq!(remove_regular_model_companion(&path).unwrap(), false);
    }

    #[test]
    fn refuses_directory_and_preserves_it() {
        let path = fixture("directory");
        fs::create_dir(&path).unwrap();
        assert!(remove_regular_model_companion(&path).is_err());
        assert!(path.is_dir());
        fs::remove_dir(&path).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn refuses_symlink_and_preserves_target() {
        use std::os::unix::fs::symlink;

        let target = fixture("target");
        let entry = fixture("symlink");
        fs::write(&target, b"do not delete target").unwrap();
        symlink(&target, &entry).unwrap();

        assert!(remove_regular_model_companion(&entry).is_err());
        assert!(fs::symlink_metadata(&entry)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read(&target).unwrap(), b"do not delete target");

        fs::remove_file(&entry).unwrap();
        fs::remove_file(&target).unwrap();
    }
}

#[cfg(test)]
mod model_download_group_ownership_tests {
    use super::*;

    const STARTED: &str = "Sat Nov 21 01:00:00 2026";
    const LEADER_COMMAND: &str = "/bundle/rdt_voice_backend --download";

    fn process(pid: u32, pgid: u32, stat: &str, started: &str, command: &str) -> ProcessSnapshot {
        ProcessSnapshot {
            pid,
            ppid: 1,
            pgid,
            stat: stat.into(),
            started: started.into(),
            command: command.into(),
        }
    }

    fn identity() -> ProcessSnapshot {
        process(410, 410, "S", STARTED, LEADER_COMMAND)
    }

    fn expected(command: &str) -> bool {
        command.contains("/bundle/rdt_voice_backend")
    }

    #[test]
    fn accepts_captured_leader_and_resource_tracker_helper() {
        let leader = identity();
        let helper = process(
            411,
            410,
            "S",
            STARTED,
            "/usr/bin/python multiprocessing.resource_tracker",
        );
        assert_eq!(
            model_download_group_snapshot_is_owned(
                &leader,
                &[leader.clone(), helper],
                false,
                expected
            ),
            Ok(true)
        );
    }

    #[test]
    fn accepts_remaining_isolated_helper_after_leader_exit() {
        let leader = identity();
        let helper = process(
            411,
            410,
            "S",
            STARTED,
            "/usr/bin/python multiprocessing.resource_tracker",
        );
        assert_eq!(
            model_download_group_snapshot_is_owned(&leader, &[helper], false, expected),
            Ok(true)
        );
    }

    #[test]
    fn rejects_pid_reuse_even_when_group_matches() {
        let leader = identity();
        let reused = process(410, 410, "S", "Sat Nov 21 02:00:00 2026", LEADER_COMMAND);
        assert!(
            model_download_group_snapshot_is_owned(&leader, &[reused], false, expected)
                .unwrap_err()
                .contains("different process")
        );
    }

    #[test]
    fn reused_leader_pid_outside_original_group_does_not_block_cleanup() {
        let leader = identity();
        let reused_pid = process(410, 900, "S", STARTED, "/tmp/unrelated-process");
        let owned_helper = process(411, 410, "S", STARTED, "/usr/bin/python helper");
        assert_eq!(
            model_download_group_snapshot_is_owned(
                &leader,
                &[reused_pid, owned_helper],
                false,
                expected
            ),
            Ok(true)
        );
    }

    #[test]
    fn changed_leader_identity_inside_original_group_is_rejected() {
        let leader = identity();
        let replaced = process(410, 410, "S", "Sat Nov 21 02:00:00 2026", LEADER_COMMAND);
        let helper = process(411, 410, "S", STARTED, "/usr/bin/python helper");
        let error =
            model_download_group_snapshot_is_owned(&leader, &[replaced, helper], false, expected)
                .unwrap_err();
        assert!(error.contains("different process inside its original process group"));
    }

    #[test]
    fn rejects_changed_command_for_captured_leader() {
        let captured = identity();
        let replacement = process(410, 410, "S", STARTED, "/tmp/replacement-process");
        let error =
            model_download_group_snapshot_is_owned(&captured, &[replacement], false, expected)
                .unwrap_err();
        assert!(error.contains("different process"), "{error}");
    }

    #[test]
    fn rejects_wrong_executable_for_original_leader() {
        let mut leader = identity();
        let wrong = process(410, 410, "S", STARTED, "/tmp/unrelated-process");
        leader.command = wrong.command.clone();
        assert!(
            model_download_group_snapshot_is_owned(&leader, &[wrong], false, expected)
                .unwrap_err()
                .contains("no longer matches the packaged backend")
        );
    }

    #[test]
    fn foreign_process_group_is_not_owned() {
        let leader = identity();
        let foreign = process(900, 900, "S", STARTED, "/tmp/unrelated-process");
        assert_eq!(
            model_download_group_snapshot_is_owned(&leader, &[foreign], false, expected),
            Ok(false)
        );
    }

    #[test]
    fn empty_process_snapshot_fails_closed() {
        let leader = identity();
        assert!(
            model_download_group_snapshot_is_owned(&leader, &[], false, expected)
                .unwrap_err()
                .contains("Could not inspect the process table")
        );
    }

    #[test]
    fn remaining_descendants_without_group_members_fail_closed() {
        let leader = identity();
        let foreign = process(900, 900, "S", STARTED, "/tmp/unrelated-process");
        assert!(
            model_download_group_snapshot_is_owned(&leader, &[foreign], true, expected)
                .unwrap_err()
                .contains("descendant processes remain")
        );
    }

    #[test]
    fn rejects_nonisolated_identity() {
        let mut leader = identity();
        leader.pgid = 900;
        assert!(model_download_group_snapshot_is_owned(
            &leader,
            &[leader.clone()],
            false,
            expected
        )
        .unwrap_err()
        .contains("not an isolated process group"));
    }

    #[test]
    fn zombie_only_group_is_not_live_or_owned() {
        let leader = identity();
        let zombie = process(410, 410, "Z", STARTED, LEADER_COMMAND);
        assert_eq!(
            model_download_group_snapshot_is_owned(&leader, &[zombie], false, expected),
            Ok(false)
        );
    }

    #[test]
    fn cancellation_attempt_guard_requires_exact_identity_and_active_request() {
        let captured = identity();

        assert!(model_download_process_identity_matches(
            &captured, &captured
        ));
        assert!(model_download_cancel_attempt_is_current(
            true,
            Some(&captured),
            &captured,
            true
        ));

        let mut changed = captured.clone();
        changed.pid += 1;
        assert!(!model_download_process_identity_matches(
            &changed, &captured
        ));

        changed = captured.clone();
        changed.pgid += 1;
        assert!(!model_download_process_identity_matches(
            &changed, &captured
        ));

        changed = captured.clone();
        changed.started = "different-start-time".into();
        assert!(!model_download_process_identity_matches(
            &changed, &captured
        ));

        changed = captured.clone();
        changed.command = "/tmp/replacement-process".into();
        assert!(!model_download_process_identity_matches(
            &changed, &captured
        ));

        assert!(!model_download_cancel_attempt_is_current(
            false,
            Some(&captured),
            &captured,
            true
        ));
        assert!(!model_download_cancel_attempt_is_current(
            true, None, &captured, true
        ));
        assert!(!model_download_cancel_attempt_is_current(
            true,
            Some(&captured),
            &captured,
            false
        ));
    }

    #[test]
    fn finish_stop_does_not_signal_unowned_group() {
        let leader = identity();
        let mut signals = Vec::new();
        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(false),
            |group, signal| {
                signals.push((group, signal));
                Ok(())
            },
            |_| false,
            |_, _| Ok(true),
        );
        assert!(result.is_ok());
        assert!(signals.is_empty());
    }

    #[test]
    fn finish_stop_accepts_group_gone_during_term_grace() {
        let leader = identity();
        let mut signals = Vec::new();
        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |group, signal| {
                signals.push((group, signal));
                Ok(())
            },
            |_| false,
            |_, timeout| {
                assert_eq!(timeout, Duration::from_millis(700));
                Ok(true)
            },
        );
        assert!(result.is_ok());
        assert_eq!(signals, vec![(leader.pgid, SIGNAL_TERM)]);
    }

    #[test]
    fn finish_stop_revalidates_before_kill() {
        let leader = identity();
        let mut owned_checks = 0usize;
        let mut waits = 0usize;
        let mut signals = Vec::new();

        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| {
                owned_checks += 1;
                Ok(true)
            },
            |group, signal| {
                signals.push((group, signal));
                Ok(())
            },
            |_| true,
            |_, timeout| {
                waits += 1;
                if waits == 1 {
                    assert_eq!(timeout, Duration::from_millis(700));
                    Ok(false)
                } else {
                    assert_eq!(timeout, Duration::from_secs(10));
                    Ok(true)
                }
            },
        );

        assert!(result.is_ok());
        assert_eq!(owned_checks, 2);
        assert_eq!(
            signals,
            vec![(leader.pgid, SIGNAL_TERM), (leader.pgid, SIGNAL_KILL)]
        );
    }

    #[test]
    fn finish_stop_refuses_kill_after_identity_error() {
        let leader = identity();
        let mut owned_checks = 0usize;
        let mut signals = Vec::new();

        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| {
                owned_checks += 1;
                if owned_checks == 1 {
                    Ok(true)
                } else {
                    Err("captured identity changed".into())
                }
            },
            |group, signal| {
                signals.push((group, signal));
                Ok(())
            },
            |_| true,
            |_, _| Ok(false),
        );

        assert!(result.unwrap_err().contains("identity changed"));
        assert_eq!(signals, vec![(leader.pgid, SIGNAL_TERM)]);
    }

    #[test]
    fn finish_stop_retains_failure_when_term_fails_and_group_lives() {
        let leader = identity();
        let mut signals = Vec::new();

        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |group, signal| {
                signals.push((group, signal));
                Err("TERM denied".into())
            },
            |_| true,
            |_, _| panic!("must not wait after failed TERM"),
        );

        assert!(result.unwrap_err().contains("graceful cleanup"));
        assert_eq!(signals, vec![(leader.pgid, SIGNAL_TERM)]);
    }

    #[test]
    fn finish_stop_accepts_term_error_if_group_is_already_gone() {
        let leader = identity();
        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |_, _| Err("TERM raced with exit".into()),
            |_| false,
            |_, _| panic!("group was already gone"),
        );
        assert!(result.is_ok());
    }

    #[test]
    fn finish_stop_retains_failure_when_kill_fails_and_group_lives() {
        let leader = identity();
        let mut signals = Vec::new();

        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |group, signal| {
                signals.push((group, signal));
                if signal == SIGNAL_KILL {
                    Err("KILL denied".into())
                } else {
                    Ok(())
                }
            },
            |_| true,
            |_, timeout| {
                assert_eq!(timeout, Duration::from_millis(700));
                Ok(false)
            },
        );

        assert!(result.unwrap_err().contains("force-stop"));
        assert_eq!(
            signals,
            vec![(leader.pgid, SIGNAL_TERM), (leader.pgid, SIGNAL_KILL)]
        );
    }

    #[test]
    fn finish_stop_accepts_kill_error_if_group_is_already_gone() {
        let leader = identity();
        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |_, signal| {
                if signal == SIGNAL_KILL {
                    Err("KILL raced with exit".into())
                } else {
                    Ok(())
                }
            },
            |_| false,
            |_, _| Ok(false),
        );
        assert!(result.is_ok());
    }

    #[test]
    fn finish_stop_fails_when_group_survives_final_deadline() {
        let leader = identity();
        let mut waits = 0usize;
        let mut signals = Vec::new();

        let result = finish_stopping_model_download_group_with(
            &leader,
            |_| Ok(true),
            |group, signal| {
                signals.push((group, signal));
                Ok(())
            },
            |_| true,
            |_, timeout| {
                waits += 1;
                if waits == 1 {
                    assert_eq!(timeout, Duration::from_millis(700));
                } else {
                    assert_eq!(timeout, Duration::from_secs(10));
                }
                Ok(false)
            },
        );

        assert!(result.unwrap_err().contains("still active"));
        assert_eq!(waits, 2);
        assert_eq!(
            signals,
            vec![(leader.pgid, SIGNAL_TERM), (leader.pgid, SIGNAL_KILL)]
        );
    }
}

#[cfg(test)]
mod model_download_terminal_finalize_tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    fn identity() -> ProcessSnapshot {
        ProcessSnapshot {
            pid: 731,
            ppid: 1,
            pgid: 731,
            stat: "S".into(),
            started: "Sat Nov 21 02:00:00 2026".into(),
            command: "/bundle/rdt_voice_backend --download".into(),
        }
    }

    fn active_status(phase: &str) -> ModelDownloadStatus {
        let mut status = ModelDownloadStatus::default();
        status.active = true;
        status.model = Some("mlx-community/whisper-small-mlx".into());
        status.destination_root = Some("/models".into());
        status.phase = phase.into();
        status.message = "Downloading".into();
        status.progress_percent = 64;
        status.downloaded_bytes = 64;
        status.total_bytes = Some(100);
        status
    }

    #[test]
    fn inactive_cancel_is_a_noop() {
        assert!(model_download_cancel_transition(&ModelDownloadStatus::default()).is_none());
    }

    #[test]
    fn repeated_cancel_during_one_attempt_is_idempotent() {
        let first = model_download_cancel_transition(&active_status("downloading")).unwrap();
        assert_eq!(first.phase, "canceling");
        assert!(model_download_cancel_transition(&first).is_none());

        let retry = model_download_cancel_transition(&active_status("failed")).unwrap();
        assert_eq!(retry.phase, "canceling");
    }

    #[test]
    fn finalization_commits_pid_status_and_cancel_flag_together() {
        let id = identity();
        let gate = Mutex::new(());
        let pid = Mutex::new(Some(id.clone()));
        let status = Mutex::new(active_status("downloading"));
        let cancel = AtomicBool::new(false);
        let events = AtomicUsize::new(0);
        let pid_check = &pid;
        let status_check = &status;

        finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &cancel,
            &id,
            false,
            |requested, current| {
                assert!(!requested);
                assert_eq!(current.phase, "downloading");
                (
                    "complete".into(),
                    "Verified files; inference not yet checked.".into(),
                )
            },
            |snapshot| {
                assert!(pid_check.lock().unwrap().is_none());
                let committed = status_check.lock().unwrap();
                assert!(!committed.active);
                assert_eq!(committed.phase, "complete");
                assert!(!snapshot.active);
                events.fetch_add(1, Ordering::SeqCst);
            },
        )
        .unwrap();

        assert!(pid.lock().unwrap().is_none());
        let committed = status.lock().unwrap();
        assert!(!committed.active);
        assert_eq!(committed.phase, "complete");
        assert_eq!(committed.progress_percent, 100);
        assert_eq!(committed.downloaded_bytes, 100);
        assert!(!cancel.load(Ordering::SeqCst));
        assert_eq!(events.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn identity_mismatch_keeps_pid_active_status_and_cancel_flag() {
        let id = identity();
        let foreign = ProcessSnapshot {
            pid: 732,
            pgid: 732,
            command: "/bundle/rdt_voice_backend --different-download".into(),
            ..id.clone()
        };
        let gate = Mutex::new(());
        let pid = Mutex::new(Some(foreign));
        let status = Mutex::new(active_status("canceling"));
        let cancel = AtomicBool::new(true);
        let events = AtomicUsize::new(0);

        let result = finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &cancel,
            &id,
            true,
            |_, _| ("cancelled".into(), "Canceled".into()),
            |_| {
                events.fetch_add(1, Ordering::SeqCst);
            },
        );

        assert!(result.is_err());
        assert_eq!(pid.lock().unwrap().as_ref().unwrap().pid, 732);
        assert!(status.lock().unwrap().active);
        assert!(cancel.load(Ordering::SeqCst));
        assert_eq!(events.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn cancellation_finalizer_requires_same_active_canceling_attempt() {
        let id = identity();
        let gate = Mutex::new(());
        let pid = Mutex::new(Some(id.clone()));
        let status = Mutex::new(active_status("failed"));
        let cancel = AtomicBool::new(true);

        let result = finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &cancel,
            &id,
            true,
            |_, _| ("cancelled".into(), "Canceled".into()),
            |_| panic!("failed/non-canceling state must not be overwritten"),
        );

        assert!(result.is_err());
        assert_eq!(pid.lock().unwrap().as_ref().unwrap().pid, id.pid);
        assert!(status.lock().unwrap().active);
        assert_eq!(status.lock().unwrap().phase, "failed");
        assert!(cancel.load(Ordering::SeqCst));
    }

    #[test]
    fn poisoned_pid_lock_fails_closed() {
        let id = identity();
        let gate = Mutex::new(());
        let pid = Arc::new(Mutex::new(Some(id.clone())));
        let poison = Arc::clone(&pid);
        let _ = std::thread::spawn(move || {
            let _guard = poison.lock().unwrap();
            panic!("intentional PID mutex poison fixture");
        })
        .join();

        let status = Mutex::new(active_status("canceling"));
        let cancel = AtomicBool::new(true);
        let result = finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &cancel,
            &id,
            true,
            |_, _| ("cancelled".into(), "Canceled".into()),
            |_| panic!("must not publish with poisoned PID mutex"),
        );

        assert!(result.is_err());
        assert!(status.lock().unwrap().active);
        assert!(cancel.load(Ordering::SeqCst));
    }

    #[test]
    fn poisoned_status_lock_preserves_pid_and_cancel_flag() {
        let id = identity();
        let gate = Mutex::new(());
        let pid = Mutex::new(Some(id.clone()));
        let status = Arc::new(Mutex::new(active_status("canceling")));
        let poison = Arc::clone(&status);
        let _ = std::thread::spawn(move || {
            let _guard = poison.lock().unwrap();
            panic!("intentional status mutex poison fixture");
        })
        .join();

        let cancel = AtomicBool::new(true);
        let result = finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &cancel,
            &id,
            true,
            |_, _| ("cancelled".into(), "Canceled".into()),
            |_| panic!("must not publish with poisoned status mutex"),
        );

        assert!(result.is_err());
        assert_eq!(pid.lock().unwrap().as_ref().unwrap().pid, id.pid);
        assert!(cancel.load(Ordering::SeqCst));
    }

    #[test]
    fn already_terminal_status_does_not_clear_stale_pid() {
        let id = identity();
        let gate = Mutex::new(());
        let pid = Mutex::new(Some(id.clone()));
        let mut terminal = active_status("complete");
        terminal.active = false;
        let status = Mutex::new(terminal);

        let result = finalize_model_download_state_with(
            &gate,
            &pid,
            &status,
            &AtomicBool::new(false),
            &id,
            false,
            |_, _| ("cancelled".into(), "Canceled".into()),
            |_| panic!("terminal status must not be republished"),
        );

        assert!(result.is_err());
        assert_eq!(pid.lock().unwrap().as_ref().unwrap().pid, id.pid);
        assert!(!status.lock().unwrap().active);
    }

    #[test]
    fn worker_and_cancel_race_publish_only_one_terminal_event() {
        let id = identity();
        let gate = Arc::new(Mutex::new(()));
        let pid = Arc::new(Mutex::new(Some(id.clone())));
        let status = Arc::new(Mutex::new(active_status("canceling")));
        let cancel = Arc::new(AtomicBool::new(true));
        let events = Arc::new(AtomicUsize::new(0));
        let mut handles = Vec::new();

        for require_canceling in [false, true] {
            let gate = Arc::clone(&gate);
            let pid = Arc::clone(&pid);
            let status = Arc::clone(&status);
            let cancel = Arc::clone(&cancel);
            let events = Arc::clone(&events);
            let expected = id.clone();
            handles.push(std::thread::spawn(move || {
                finalize_model_download_state_with(
                    &gate,
                    &pid,
                    &status,
                    &cancel,
                    &expected,
                    require_canceling,
                    |_, _| ("cancelled".into(), "One terminal result only.".into()),
                    move |_| {
                        events.fetch_add(1, Ordering::SeqCst);
                    },
                )
            }));
        }

        let results: Vec<_> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(events.load(Ordering::SeqCst), 1);
        assert!(pid.lock().unwrap().is_none());
        assert!(!status.lock().unwrap().active);
        assert!(!cancel.load(Ordering::SeqCst));
    }
}

#[cfg(test)]
mod model_download_log_cleanup_tests {
    use super::*;
    use std::io::{Error, ErrorKind};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};

    #[test]
    fn poisoned_log_mutex_still_attempts_unlink_and_reports_poison() {
        let expected = PathBuf::from("/tmp/rdt-log-poison-fixture.log");
        let store = Arc::new(Mutex::new(Some(expected.clone())));
        let poison_store = Arc::clone(&store);

        let poison_result = std::thread::spawn(move || {
            let _guard = poison_store.lock().unwrap();
            panic!("intentional log mutex poison fixture");
        })
        .join();
        assert!(poison_result.is_err());

        let unlink_attempted = AtomicBool::new(false);
        let result = clear_model_download_log_path_with(store.as_ref(), &expected, |_| {
            unlink_attempted.store(true, Ordering::SeqCst);
            Ok(())
        });

        assert!(result.is_err());
        assert!(result.unwrap_err().contains("poisoned"));
        assert!(unlink_attempted.load(Ordering::SeqCst));
    }

    #[test]
    fn unlink_failure_does_not_clear_a_newer_attempt_log_path() {
        let old_path = PathBuf::from("/tmp/rdt-old-download.log");
        let new_path = PathBuf::from("/tmp/rdt-new-download.log");
        let store = Mutex::new(Some(new_path.clone()));

        let result = clear_model_download_log_path_with(&store, &old_path, |_| {
            Err(Error::new(
                ErrorKind::PermissionDenied,
                "fixture unlink failure",
            ))
        });

        assert!(result.is_err());
        assert_eq!(store.lock().unwrap().as_deref(), Some(new_path.as_path()));
    }

    #[test]
    fn missing_log_is_success_and_exact_path_is_cleared() {
        let expected = PathBuf::from("/tmp/rdt-download-already-removed.log");
        let store = Mutex::new(Some(expected.clone()));

        let result = clear_model_download_log_path_with(&store, &expected, |_| {
            Err(Error::new(ErrorKind::NotFound, "fixture already absent"))
        });

        assert!(result.is_ok());
        assert!(store.lock().unwrap().is_none());
    }
}

#[cfg(test)]
mod model_download_process_snapshot_tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn unix_process_parser_accepts_complete_identity_rows() {
        let output = "4242 1 4242 S Sat Nov 21 02:58:00 2026 /opt/rdt_voice_backend --worker\n";
        let snapshot = parse_unix_process_snapshot(output).unwrap();

        assert_eq!(snapshot.len(), 1);
        assert_eq!(snapshot[0].pid, 4242);
        assert_eq!(snapshot[0].ppid, 1);
        assert_eq!(snapshot[0].pgid, 4242);
        assert_eq!(snapshot[0].started, "Sat Nov 21 02:58:00 2026");
        assert_eq!(snapshot[0].command, "/opt/rdt_voice_backend --worker");
    }

    #[test]
    fn unix_process_parser_rejects_empty_and_malformed_tables() {
        assert!(parse_unix_process_snapshot("").is_err());
        assert!(parse_unix_process_snapshot("malformed process row\n").is_err());
        assert!(
            parse_unix_process_snapshot("not-a-pid 1 1 S Sat Nov 21 02:58:00 2026 /worker\n")
                .is_err()
        );
    }

    #[test]
    fn windows_process_parser_accepts_complete_identity_rows() {
        let value = serde_json::json!([{
            "ProcessId": 77,
            "ParentProcessId": 1,
            "CreationDate": "20261121025800.000000-360",
            "ExecutablePath": "C:\\RDT\\rdt_voice_backend.exe",
            "CommandLine": "--worker"
        }]);

        let snapshot = parse_windows_process_snapshot(value).unwrap();

        assert_eq!(snapshot.len(), 1);
        assert_eq!(snapshot[0].pid, 77);
        assert_eq!(snapshot[0].ppid, 1);
        assert_eq!(snapshot[0].pgid, 77);
        assert!(snapshot[0].command.contains("rdt_voice_backend.exe"));
    }

    #[test]
    fn windows_process_parser_rejects_empty_and_incomplete_tables() {
        assert!(parse_windows_process_snapshot(serde_json::json!([])).is_err());
        assert!(parse_windows_process_snapshot(serde_json::json!({
            "ProcessId": 77,
            "CreationDate": "20261121025800.000000-360"
        }))
        .is_err());
        assert!(parse_windows_process_snapshot(serde_json::json!("not a process table")).is_err());
    }

    #[test]
    fn process_table_launch_exit_and_empty_errors_fail_closed() {
        let launch_error = checked_process_snapshot_output(
            "fixture-process-list",
            Err("permission denied".into()),
            |_: &str| Ok(Vec::new()),
        );
        assert!(launch_error.is_err());

        let exit_error = checked_process_snapshot_output(
            "fixture-process-list",
            Ok((false, String::new())),
            |_: &str| Ok(Vec::new()),
        );
        assert!(exit_error.is_err());

        let empty_result = checked_process_snapshot_output(
            "fixture-process-list",
            Ok((true, String::new())),
            |_: &str| Ok(Vec::new()),
        );
        assert!(empty_result.is_err());
    }

    #[test]
    fn poisoned_worker_status_read_returns_error() {
        let store = Arc::new(Mutex::new(true));
        let poison_store = Arc::clone(&store);

        let poison_result = std::thread::spawn(move || {
            let _guard = poison_store.lock().unwrap();
            panic!("intentional worker status mutex poison fixture");
        })
        .join();

        assert!(poison_result.is_err());

        let result = inspect_model_download_status(store.as_ref(), |value| *value);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("poisoned"));
    }

    #[test]
    fn worker_status_inspection_returns_real_predicate() {
        let store = Mutex::new(true);
        let result = inspect_model_download_status(&store, |value| *value);
        assert!(result.unwrap());
    }
}

#[cfg(test)]
mod model_download_registration_lock_tests {
    use super::*;

    #[test]
    fn poisoned_pid_store_prevents_registration() {
        let pid_store = std::sync::Arc::new(std::sync::Mutex::new(None::<ProcessSnapshot>));
        let log_store = std::sync::Mutex::new(None::<PathBuf>);
        let poison_target = std::sync::Arc::clone(&pid_store);
        let _ = std::thread::spawn(move || {
            let _guard = poison_target.lock().unwrap();
            panic!("intentional downloader PID-store poison fixture");
        })
        .join();

        assert!(lock_model_download_registration_stores(pid_store.as_ref(), &log_store,).is_err());
    }

    #[test]
    fn poisoned_log_store_prevents_registration() {
        let pid_store = std::sync::Mutex::new(None::<ProcessSnapshot>);
        let log_store = std::sync::Arc::new(std::sync::Mutex::new(None::<PathBuf>));
        let poison_target = std::sync::Arc::clone(&log_store);
        let _ = std::thread::spawn(move || {
            let _guard = poison_target.lock().unwrap();
            panic!("intentional downloader log-store poison fixture");
        })
        .join();

        assert!(lock_model_download_registration_stores(&pid_store, log_store.as_ref(),).is_err());
    }
}

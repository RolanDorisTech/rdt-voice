//! Linux trusted local HTTPS for RDT Connect (compiled only on Linux).
//!
//! Design summary
//! * Only app-generated certificates are accepted on Linux. A short-lived root CA
//!   (name-constrained to localhost/127.0.0.1/::1) signs one server certificate; the
//!   CA private key is deleted immediately after signing.
//! * Material lives in `<https_dir>/generated-linux-*` (0700, owned by the user) and is
//!   pinned by SHA-256 in `local-https.json`.
//! * System trust is installed with a fixed script run through `pkexec` (or directly when
//!   already root) for Debian-family (`update-ca-certificates`) and RHEL-family
//!   (`update-ca-trust`) systems. Anything else fails closed.
//! * "Trusted" means `openssl verify` accepts the server certificate against the system
//!   CA bundle for localhost, 127.0.0.1 and ::1 — never merely that a file exists.
//! * Browsers with their own stores (Firefox, Chrome/Chromium NSS) are NOT modified.

use super::{
    canonical_file, load_local_tls_config, save_local_tls_config, set_private_permissions,
    sha256_file_hex, BackendState, LocalTlsConfig,
};
use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::net::IpAddr;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const OPENSSL: &str = "/usr/bin/openssl";
const PKEXEC: &str = "/usr/bin/pkexec";
const MANAGED_DIR_PREFIX: &str = "generated-linux-";
const STAGING_PREFIX: &str = ".staging-linux-";
const TRUST_FILE_PREFIX: &str = "rdt-connect-local-https-";
const CA_DAYS: &str = "1095";
const LEAF_DAYS: &str = "825";
static COUNTER: AtomicU64 = AtomicU64::new(0);

// ---------------------------------------------------------------------------
// Pure helpers (unit-tested)
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq, Clone)]
pub(crate) enum SanEntry {
    Dns(String),
    Ip(IpAddr),
    Other,
}

/// Parse the output of `openssl x509 -noout -ext subjectAltName` into exact entries.
pub(crate) fn parse_san_entries(text: &str) -> Vec<SanEntry> {
    let mut out = Vec::new();
    for line in text.lines() {
        if line.contains("Subject Alternative Name") {
            continue;
        }
        for piece in line.split(',') {
            let piece = piece.trim();
            if piece.is_empty() {
                continue;
            }
            if let Some(name) = piece.strip_prefix("DNS:") {
                out.push(SanEntry::Dns(name.trim().to_ascii_lowercase()));
            } else if let Some(ip) = piece.strip_prefix("IP Address:") {
                match ip.trim().parse::<IpAddr>() {
                    Ok(parsed) => out.push(SanEntry::Ip(parsed)),
                    Err(_) => out.push(SanEntry::Other),
                }
            } else {
                out.push(SanEntry::Other);
            }
        }
    }
    out
}

/// The certificate must name localhost, 127.0.0.1 and ::1 exactly (no substring matching).
pub(crate) fn san_covers_loopback(entries: &[SanEntry]) -> Result<(), String> {
    let v4: IpAddr = "127.0.0.1".parse().unwrap();
    let v6: IpAddr = "::1".parse().unwrap();
    let has_dns = entries.iter().any(|e| *e == SanEntry::Dns("localhost".into()));
    let has_v4 = entries.iter().any(|e| *e == SanEntry::Ip(v4));
    let has_v6 = entries.iter().any(|e| *e == SanEntry::Ip(v6));
    if has_dns && has_v4 && has_v6 {
        return Ok(());
    }
    let mut missing = Vec::new();
    if !has_dns { missing.push("DNS:localhost"); }
    if !has_v4 { missing.push("IP:127.0.0.1"); }
    if !has_v6 { missing.push("IP:::1"); }
    Err(format!(
        "The server certificate's Subject Alternative Name is missing {}. A certificate for another host or IP address is not accepted.",
        missing.join(", ")
    ))
}

/// Normalize a SHA-256 fingerprint ("AA:BB:…" or hex, any case) to 64 lowercase hex digits.
pub(crate) fn normalize_fingerprint(raw: &str) -> Option<String> {
    let cleaned: String = raw
        .chars()
        .filter(|c| *c != ':' && !c.is_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    if cleaned.len() == 64 && cleaned.chars().all(|c| c.is_ascii_hexdigit()) {
        Some(cleaned)
    } else {
        None
    }
}

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub(crate) enum TrustStore {
    DebianCaCertificates,
    RhelCaTrust,
}

impl TrustStore {
    fn label(self) -> &'static str {
        match self {
            TrustStore::DebianCaCertificates => "Debian/Ubuntu (update-ca-certificates)",
            TrustStore::RhelCaTrust => "Fedora/RHEL (update-ca-trust)",
        }
    }
    fn anchor_dir(self) -> &'static str {
        match self {
            TrustStore::DebianCaCertificates => "/usr/local/share/ca-certificates",
            TrustStore::RhelCaTrust => "/etc/pki/ca-trust/source/anchors",
        }
    }
    fn install_refresh(self) -> &'static str {
        match self {
            TrustStore::DebianCaCertificates => "/usr/sbin/update-ca-certificates",
            TrustStore::RhelCaTrust => "/usr/bin/update-ca-trust extract",
        }
    }
    fn remove_refresh(self) -> &'static str {
        match self {
            TrustStore::DebianCaCertificates => "/usr/sbin/update-ca-certificates --fresh",
            TrustStore::RhelCaTrust => "/usr/bin/update-ca-trust extract",
        }
    }
    fn bundle(self) -> &'static str {
        match self {
            TrustStore::DebianCaCertificates => "/etc/ssl/certs/ca-certificates.crt",
            TrustStore::RhelCaTrust => "/etc/pki/tls/certs/ca-bundle.crt",
        }
    }
}

pub(crate) fn detect_trust_store_with(exists: &dyn Fn(&str) -> bool) -> Option<TrustStore> {
    if exists("/usr/sbin/update-ca-certificates") {
        Some(TrustStore::DebianCaCertificates)
    } else if exists("/usr/bin/update-ca-trust") {
        Some(TrustStore::RhelCaTrust)
    } else {
        None
    }
}

fn detect_trust_store() -> Option<TrustStore> {
    detect_trust_store_with(&|p| Path::new(p).is_file())
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PrivilegedResult {
    Done,
    /// The operation failed and the trust store is known to be unchanged.
    Unchanged(String),
    /// The outcome is not certain (e.g. killed by a signal); a record must be kept.
    Unknown(String),
}

/// Map the privileged script / pkexec exit status to an honest result.
pub(crate) fn map_privileged_exit(code: Option<i32>, detail: &str) -> PrivilegedResult {
    let tail = detail.trim();
    let suffix = if tail.is_empty() { String::new() } else { format!(" Detail: {}", tail.chars().take(300).collect::<String>()) };
    match code {
        Some(0) => PrivilegedResult::Done,
        Some(126) | Some(127) => PrivilegedResult::Unchanged(format!(
            "Administrator authorization was cancelled, denied or unavailable (no authentication agent?). No trust change was made.{suffix}"
        )),
        Some(41) => PrivilegedResult::Unchanged("The staged CA certificate did not match the expected fingerprint. Nothing was installed.".into()),
        Some(44) => PrivilegedResult::Unchanged("A file or symlink with RDT Connect's trust-entry name already exists with different content. It was left untouched and nothing was changed.".into()),
        Some(43) => PrivilegedResult::Unchanged(format!("The system trust-store refresh failed; the change was rolled back where possible.{suffix}")),
        Some(42) => PrivilegedResult::Unchanged(format!("The trust file could not be written. No trust change was recorded as complete.{suffix}")),
        Some(50) => PrivilegedResult::Unchanged("RDT Connect refused an invalid trust request.".into()),
        Some(other) => PrivilegedResult::Unknown(format!("The privileged trust helper exited with unexpected status {other}.{suffix}")),
        None => PrivilegedResult::Unknown("The privileged trust helper was terminated by a signal; trust-store state is unknown.".into()),
    }
}

/// Fixed privileged script. Only compile-time constants for the detected store are
/// interpolated; all runtime values arrive as positional parameters ($1 op, $2 fingerprint,
/// $3 staged file) and are validated inside before use.
pub(crate) fn privileged_script(store: TrustStore) -> String {
    format!(
        r#"set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
op="$1"; fp="$2"; src="$3"
dir='{dir}'
case "$fp" in ''|*[!0-9a-f]*) exit 50;; esac
[ "${{#fp}}" -eq 64 ] || exit 50
case "$op" in install|remove) ;; *) exit 50;; esac
dest="$dir/{prefix}$fp.crt"
der_fp() {{ openssl x509 -in "$1" -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1; }}
if [ "$op" = install ]; then
  [ -f "$src" ] && [ ! -L "$src" ] || exit 41
  tmp=$(mktemp) || exit 42
  trap 'rm -f "$tmp" "$dest.rdt-new"' EXIT
  head -c 65536 < "$src" > "$tmp" || exit 41
  [ "$(der_fp "$tmp")" = "$fp" ] || exit 41
  [ ! -L "$dest" ] || exit 44
  if [ -e "$dest" ]; then [ "$(der_fp "$dest")" = "$fp" ] || exit 44; fi
  install -d -m 0755 "$dir" || exit 42
  install -m 0644 "$tmp" "$dest.rdt-new" || exit 42
  mv -f "$dest.rdt-new" "$dest" || exit 42
  if ! {install_refresh}; then
    rm -f "$dest"
    {install_refresh} >/dev/null 2>&1 || true
    exit 43
  fi
  exit 0
fi
[ ! -L "$dest" ] || exit 44
if [ -e "$dest" ]; then
  [ "$(der_fp "$dest")" = "$fp" ] || exit 44
  rm -f -- "$dest" || exit 42
fi
{remove_refresh} || exit 43
exit 0
"#,
        dir = store.anchor_dir(),
        prefix = TRUST_FILE_PREFIX,
        install_refresh = store.install_refresh(),
        remove_refresh = store.remove_refresh(),
    )
}

/// Build the argument vector for `sh -c <script> rdt-trust <op> <fp> <src>`.
pub(crate) fn privileged_args(store: TrustStore, op: &str, fingerprint: &str, staged: Option<&Path>) -> Result<Vec<std::ffi::OsString>, String> {
    if op != "install" && op != "remove" {
        return Err("Internal error: unknown trust operation.".into());
    }
    let fp = normalize_fingerprint(fingerprint)
        .ok_or_else(|| "A malformed certificate fingerprint was refused.".to_string())?;
    let mut args: Vec<std::ffi::OsString> = vec!["-c".into(), privileged_script(store).into(), "rdt-trust".into(), op.into(), fp.into()];
    match staged {
        Some(path) => args.push(path.as_os_str().to_owned()),
        None => args.push("-".into()),
    }
    Ok(args)
}

// ---------------------------------------------------------------------------
// OpenSSL / filesystem helpers
// ---------------------------------------------------------------------------

fn openssl<I, S>(args: I) -> Result<Output, String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    if !Path::new(OPENSSL).is_file() {
        return Err("OpenSSL was not found at /usr/bin/openssl. Install the 'openssl' package, then retry.".into());
    }
    Command::new(OPENSSL)
        .args(args)
        .output()
        .map_err(|e| format!("RDT Connect could not run OpenSSL: {e}"))
}

fn openssl_ok<I, S>(args: I, what: &str) -> Result<Output, String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let output = openssl(args)?;
    if output.status.success() {
        Ok(output)
    } else {
        Err(format!("Could not {what}: {}", String::from_utf8_lossy(&output.stderr).trim()))
    }
}

fn euid() -> u32 {
    unsafe { libc::geteuid() }
}

fn ensure_private_dir(path: &Path) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if meta.file_type().is_symlink() || !meta.is_dir() {
                return Err(format!("{} is not a real directory (symlinks are refused).", path.display()));
            }
            if meta.uid() != euid() {
                return Err(format!("{} is not owned by the current user.", path.display()));
            }
            if meta.mode() & 0o077 != 0 {
                set_private_permissions(path, 0o700).map_err(|e| format!("Could not restrict {}: {e}", path.display()))?;
            }
            Ok(())
        }
        Err(_) => {
            fs::create_dir(path).map_err(|e| format!("Could not create {}: {e}", path.display()))?;
            set_private_permissions(path, 0o700).map_err(|e| format!("Could not restrict {}: {e}", path.display()))
        }
    }
}

fn write_new_private(path: &Path, contents: &str) -> Result<(), String> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|e| format!("Could not create {}: {e}", path.display()))?;
    file.write_all(contents.as_bytes()).map_err(|e| format!("Could not write {}: {e}", path.display()))
}

struct StagingGuard {
    path: PathBuf,
    armed: bool,
}
impl Drop for StagingGuard {
    fn drop(&mut self) {
        if self.armed {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

fn unique_suffix() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    format!("{secs}-{}-{}", std::process::id(), COUNTER.fetch_add(1, Ordering::SeqCst))
}

fn remove_stale_staging(https_dir: &Path) {
    let Ok(entries) = fs::read_dir(https_dir) else { return };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if !name.starts_with(STAGING_PREFIX) { continue; }
        // Never follow symlinks: only real directories we own are removed.
        if let Ok(meta) = fs::symlink_metadata(entry.path()) {
            if meta.is_dir() && !meta.file_type().is_symlink() && meta.uid() == euid() {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
}

pub(crate) struct GeneratedTls {
    pub dir: PathBuf,
    pub cert: PathBuf,
    pub key: PathBuf,
    pub ca: PathBuf,
}

const CA_CNF: &str = "[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = v3_ca\n[dn]\nCN = RDT Connect Local HTTPS CA\n[v3_ca]\nbasicConstraints = critical,CA:TRUE,pathlen:0\nkeyUsage = critical,keyCertSign,cRLSign\nsubjectKeyIdentifier = hash\nnameConstraints = critical,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255,permitted;IP:0:0:0:0:0:0:0:1/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff\n";
const SERVER_EXT: &str = "basicConstraints = critical,CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth\nsubjectAltName = DNS:localhost,IP:127.0.0.1,IP:::1\nsubjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid,issuer\n";

/// Generate CA + server certificate in a staging directory, delete the CA key, validate,
/// then atomically move the directory into place. Nothing is reported unless every step passed.
pub(crate) fn generate_tls_files(https_dir: &Path) -> Result<GeneratedTls, String> {
    ensure_private_dir(https_dir)?;
    remove_stale_staging(https_dir);
    let suffix = unique_suffix();
    let staging = https_dir.join(format!("{STAGING_PREFIX}{suffix}"));
    let final_dir = https_dir.join(format!("{MANAGED_DIR_PREFIX}{suffix}"));
    fs::create_dir(&staging).map_err(|e| format!("Could not create the certificate staging folder: {e}"))?;
    let mut guard = StagingGuard { path: staging.clone(), armed: true };
    set_private_permissions(&staging, 0o700).map_err(|e| format!("Could not secure the certificate staging folder: {e}"))?;

    let ca_key = staging.join("ca-key.pem");
    let ca = staging.join("rdt-local-ca.pem");
    let key = staging.join("rdt-local-server-key.pem");
    let csr = staging.join("server.csr");
    let cert = staging.join("rdt-local-server.pem");
    let ca_cnf = staging.join("ca.cnf");
    let ext = staging.join("server.ext");
    let serial = staging.join("rdt-local-ca.srl");
    write_new_private(&ca_cnf, CA_CNF)?;
    write_new_private(&ext, SERVER_EXT)?;

    openssl_ok(
        [
            OsStr::new("req"), OsStr::new("-new"), OsStr::new("-x509"), OsStr::new("-newkey"), OsStr::new("rsa:3072"),
            OsStr::new("-nodes"), OsStr::new("-days"), OsStr::new(CA_DAYS), OsStr::new("-sha256"),
            OsStr::new("-keyout"), ca_key.as_os_str(), OsStr::new("-out"), ca.as_os_str(),
            OsStr::new("-subj"), OsStr::new("/CN=RDT Connect Local HTTPS CA"),
            OsStr::new("-config"), ca_cnf.as_os_str(), OsStr::new("-extensions"), OsStr::new("v3_ca"),
        ],
        "generate the loopback-only local certificate authority",
    )?;
    openssl_ok(
        [
            OsStr::new("req"), OsStr::new("-new"), OsStr::new("-newkey"), OsStr::new("rsa:2048"), OsStr::new("-nodes"),
            OsStr::new("-keyout"), key.as_os_str(), OsStr::new("-out"), csr.as_os_str(),
            OsStr::new("-subj"), OsStr::new("/CN=localhost"),
        ],
        "generate the local HTTPS server key",
    )?;
    openssl_ok(
        [
            OsStr::new("x509"), OsStr::new("-req"), OsStr::new("-in"), csr.as_os_str(),
            OsStr::new("-CA"), ca.as_os_str(), OsStr::new("-CAkey"), ca_key.as_os_str(),
            OsStr::new("-CAserial"), serial.as_os_str(), OsStr::new("-CAcreateserial"),
            OsStr::new("-out"), cert.as_os_str(), OsStr::new("-days"), OsStr::new(LEAF_DAYS),
            OsStr::new("-sha256"), OsStr::new("-extfile"), ext.as_os_str(),
        ],
        "sign the local HTTPS server certificate",
    )?;
    // The CA key is never needed again: remove it before anything is trusted.
    fs::remove_file(&ca_key).map_err(|e| format!("Could not delete the temporary CA private key: {e}"))?;
    for temp in [&csr, &ca_cnf, &ext, &serial] {
        let _ = fs::remove_file(temp);
    }
    set_private_permissions(&key, 0o600).map_err(|e| format!("Could not secure the server private key: {e}"))?;
    set_private_permissions(&cert, 0o644).map_err(|e| format!("Could not set certificate permissions: {e}"))?;
    set_private_permissions(&ca, 0o644).map_err(|e| format!("Could not set CA permissions: {e}"))?;

    verify_chain_and_names(&ca, &cert, None)?;
    check_key_matches(&cert, &key)?;

    if final_dir.exists() {
        return Err("The certificate folder name is already in use; retry setup.".into());
    }
    fs::rename(&staging, &final_dir).map_err(|e| format!("Could not finalize the certificate folder: {e}"))?;
    guard.armed = false;
    Ok(GeneratedTls {
        cert: final_dir.join("rdt-local-server.pem"),
        key: final_dir.join("rdt-local-server-key.pem"),
        ca: final_dir.join("rdt-local-ca.pem"),
        dir: final_dir,
    })
}

fn verify_failure_message(text: &str) -> String {
    let lower = text.to_ascii_lowercase();
    if lower.contains("has expired") {
        "The HTTPS certificate or its CA is expired. Run Set up trusted local HTTPS again to regenerate it.".into()
    } else if lower.contains("not yet valid") {
        "The HTTPS certificate or its CA is not yet valid (check the system clock), so it was rejected.".into()
    } else if lower.contains("hostname mismatch") || lower.contains("ip address mismatch") {
        "The certificate does not match localhost / 127.0.0.1 / ::1 exactly.".into()
    } else if lower.contains("unable to get local issuer") || lower.contains("self-signed certificate") || lower.contains("self signed certificate") {
        "The certificate does not chain to a trusted CA.".into()
    } else if lower.contains("purpose") {
        "The certificate is not valid for TLS server authentication.".into()
    } else {
        format!("Certificate verification failed: {}", text.trim().lines().last().unwrap_or("unknown error"))
    }
}

/// `openssl verify` with an explicit bundle. Checks chain, validity dates, serverAuth purpose
/// and exact hostname/IP matching for localhost, 127.0.0.1 and ::1.
fn verify_leaf_against_bundle(bundle: &Path, cert: &Path, at: Option<i64>) -> Result<(), String> {
    let checks: [(&str, &str); 3] = [("-verify_hostname", "localhost"), ("-verify_ip", "127.0.0.1"), ("-verify_ip", "::1")];
    for (flag, value) in checks {
        let mut args: Vec<std::ffi::OsString> = vec!["verify".into(), "-CAfile".into(), bundle.as_os_str().to_owned(), "-purpose".into(), "sslserver".into(), flag.into(), value.into()];
        if let Some(ts) = at {
            args.push("-attime".into());
            args.push(ts.to_string().into());
        }
        args.push(cert.as_os_str().to_owned());
        let output = openssl(args)?;
        if !output.status.success() {
            let text = format!("{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
            return Err(verify_failure_message(&text));
        }
    }
    Ok(())
}

/// The CA must verify against itself (self-signed root) and be within its validity dates.
fn verify_self_signed_root(ca: &Path, at: Option<i64>) -> Result<(), String> {
    let mut args: Vec<std::ffi::OsString> = vec!["verify".into(), "-CAfile".into(), ca.as_os_str().to_owned()];
    if let Some(ts) = at {
        args.push("-attime".into());
        args.push(ts.to_string().into());
    }
    args.push(ca.as_os_str().to_owned());
    let output = openssl(args)?;
    if output.status.success() {
        return Ok(());
    }
    let text = format!("{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
    Err(verify_failure_message(&text))
}

fn verify_chain_and_names(ca: &Path, cert: &Path, at: Option<i64>) -> Result<(), String> {
    verify_leaf_against_bundle(ca, cert, at)
}

fn check_key_matches(cert: &Path, key: &Path) -> Result<(), String> {
    let cert_pub = openssl_ok([OsStr::new("x509"), OsStr::new("-in"), cert.as_os_str(), OsStr::new("-pubkey"), OsStr::new("-noout")], "read the certificate public key")?;
    let key_pub = openssl([OsStr::new("pkey"), OsStr::new("-in"), key.as_os_str(), OsStr::new("-pubout"), OsStr::new("-passin"), OsStr::new("pass:")])?;
    if !key_pub.status.success() {
        return Err("The private key could not be read without a passphrase.".into());
    }
    if cert_pub.stdout != key_pub.stdout {
        return Err("The server certificate and private key do not match.".into());
    }
    Ok(())
}

fn der_sha256(cert: &Path) -> Result<String, String> {
    let output = openssl_ok([OsStr::new("x509"), OsStr::new("-in"), cert.as_os_str(), OsStr::new("-noout"), OsStr::new("-fingerprint"), OsStr::new("-sha256")], "calculate the CA fingerprint")?;
    let text = String::from_utf8_lossy(&output.stdout).to_string();
    let value = text.split('=').nth(1).unwrap_or("");
    normalize_fingerprint(value).ok_or_else(|| "OpenSSL returned an invalid CA fingerprint.".to_string())
}

fn check_owned_file(raw: &str, label: &str, private: bool) -> Result<PathBuf, String> {
    let configured = Path::new(raw.trim());
    let meta = fs::symlink_metadata(configured).map_err(|e| format!("Could not inspect the {label}: {e}"))?;
    if meta.file_type().is_symlink() {
        return Err(format!("The {label} path is a symlink; it was refused."));
    }
    if !meta.is_file() || meta.uid() != euid() {
        return Err(format!("The {label} is not a regular file owned by the current user."));
    }
    let forbidden = if private { 0o077 } else { 0o022 };
    if meta.mode() & forbidden != 0 {
        return Err(format!("The {label} has unsafe permissions; run Set up trusted local HTTPS again."));
    }
    canonical_file(raw, label)
}

pub(crate) struct ValidatedTls {
    pub ca_fingerprint: String,
    pub cert: PathBuf,
    pub ca: PathBuf,
}

fn pin_matches(expected: &Option<String>, path: &Path, label: &str) -> Result<(), String> {
    let expected = expected.as_deref().ok_or_else(|| format!("The generated {label} has no integrity record. Run Set up trusted local HTTPS again."))?;
    let actual = sha256_file_hex(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        return Err(format!("The generated {label} changed after setup. Run Set up trusted local HTTPS again; HTTPS will not start with altered TLS material."));
    }
    Ok(())
}

pub(crate) fn validate_material(config: &LocalTlsConfig, at: Option<i64>) -> Result<ValidatedTls, String> {
    let cert_raw = config.certificate_path.as_deref().ok_or("HTTPS is selected, but no server certificate is configured. Select Set up trusted local HTTPS; RDT Connect will not silently fall back to HTTP.")?;
    let key_raw = config.private_key_path.as_deref().ok_or("HTTPS is selected, but no private key is configured. Select Set up trusted local HTTPS; RDT Connect will not silently fall back to HTTP.")?;
    let ca_raw = config.ca_certificate_path.as_deref().ok_or("The generated Linux HTTPS setup has no recorded root CA. Run Set up trusted local HTTPS again.")?;
    if !config.generated {
        return Err("Linux trusted local HTTPS accepts only RDT-generated certificates. Run Set up trusted local HTTPS.".into());
    }
    let cert = check_owned_file(cert_raw, "server certificate", false)?;
    let key = check_owned_file(key_raw, "private key", true)?;
    let ca = check_owned_file(ca_raw, "root CA certificate", false)?;
    let dir = cert.parent().ok_or("The certificate has no parent folder.")?.to_path_buf();
    if key.parent() != Some(dir.as_path()) || ca.parent() != Some(dir.as_path()) {
        return Err("The certificate, key and CA must live together in the RDT-managed folder.".into());
    }
    let dir_name = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if !dir_name.starts_with(MANAGED_DIR_PREFIX) {
        return Err("The certificate is not inside an RDT-managed Linux HTTPS folder.".into());
    }
    let dir_meta = fs::symlink_metadata(&dir).map_err(|e| format!("Could not inspect the certificate folder: {e}"))?;
    if dir_meta.file_type().is_symlink() || !dir_meta.is_dir() || dir_meta.uid() != euid() || dir_meta.mode() & 0o077 != 0 {
        return Err("The RDT certificate folder is not private to the current user.".into());
    }
    pin_matches(&config.linux_certificate_sha256, &cert, "server certificate")?;
    pin_matches(&config.linux_private_key_sha256, &key, "private key")?;
    pin_matches(&config.linux_ca_sha256, &ca, "root CA certificate")?;

    let san = openssl_ok([OsStr::new("x509"), OsStr::new("-in"), cert.as_os_str(), OsStr::new("-noout"), OsStr::new("-ext"), OsStr::new("subjectAltName")], "read the certificate's Subject Alternative Name")?;
    san_covers_loopback(&parse_san_entries(&String::from_utf8_lossy(&san.stdout)))?;
    check_key_matches(&cert, &key)?;
    let bc = openssl_ok([OsStr::new("x509"), OsStr::new("-in"), ca.as_os_str(), OsStr::new("-noout"), OsStr::new("-ext"), OsStr::new("basicConstraints")], "read the CA basic constraints")?;
    let is_ca = String::from_utf8_lossy(&bc.stdout).lines().any(|l| l.split(',').any(|t| t.trim() == "CA:TRUE"));
    if !is_ca {
        return Err("The recorded root CA is not a CA certificate.".into());
    }
    verify_self_signed_root(&ca, at)?;
    verify_chain_and_names(&ca, &cert, at)?;
    let fingerprint = der_sha256(&ca)?;
    Ok(ValidatedTls { ca_fingerprint: fingerprint, cert, ca })
}

pub(crate) fn validate_config(config: &LocalTlsConfig) -> Result<(), String> {
    validate_material(config, None).map(|_| ())
}

// ---------------------------------------------------------------------------
// Trust verification (never inferred from file existence)
// ---------------------------------------------------------------------------

pub(crate) fn certificate_trusted(config: &LocalTlsConfig) -> bool {
    trust_error(config).is_none()
}

/// None when the system trust store verifies the certificate; otherwise the precise reason.
fn trust_error(config: &LocalTlsConfig) -> Option<String> {
    let validated = match validate_material(config, None) {
        Ok(v) => v,
        Err(e) => return Some(e),
    };
    let Some(store) = detect_trust_store() else {
        return Some("No supported Linux trust-store tool was found (supported: Debian/Ubuntu update-ca-certificates, Fedora/RHEL update-ca-trust).".into());
    };
    let bundle = Path::new(store.bundle());
    if !bundle.is_file() {
        return Some(format!("The system CA bundle {} was not found.", store.bundle()));
    }
    match verify_leaf_against_bundle(bundle, &validated.cert, None) {
        Ok(()) => None,
        Err(e) => Some(e),
    }
}

pub(crate) fn untrusted_message(config: &LocalTlsConfig) -> String {
    let reason = trust_error(config).unwrap_or_else(|| "unknown reason".into());
    format!("The certificate is valid but this system does not trust it yet ({reason}) Use Set up trusted local HTTPS and approve the administrator prompt.")
}

pub(crate) fn trusted_message(config: &LocalTlsConfig) -> String {
    let ca = config.ca_certificate_path.as_deref().unwrap_or("the RDT CA file");
    format!("HTTPS is configured: the certificate is valid for localhost, 127.0.0.1 and ::1 and the system trust store verifies it. Browsers with their own certificate store (Firefox, Chrome/Chromium) may still need this CA imported manually: {ca}")
}

// ---------------------------------------------------------------------------
// Privileged trust install / removal
// ---------------------------------------------------------------------------

fn run_privileged(store: TrustStore, op: &str, fingerprint: &str, staged: Option<&Path>) -> PrivilegedResult {
    let args = match privileged_args(store, op, fingerprint, staged) {
        Ok(a) => a,
        Err(e) => return PrivilegedResult::Unchanged(e),
    };
    let mut command = if euid() == 0 {
        Command::new("/bin/sh")
    } else {
        if !Path::new(PKEXEC).is_file() {
            return PrivilegedResult::Unchanged("pkexec (polkit) was not found, so RDT Connect cannot request administrator permission. Install polkit or run the app with sufficient privileges. No trust change was made.".into());
        }
        let mut c = Command::new(PKEXEC);
        c.arg("/bin/sh");
        c
    };
    command.args(&args);
    match command.output() {
        Ok(output) => map_privileged_exit(output.status.code(), &String::from_utf8_lossy(&output.stderr)),
        Err(e) => PrivilegedResult::Unchanged(format!("Could not start the privileged trust helper: {e}")),
    }
}

pub(crate) fn trust_ca(state: &BackendState, config: &mut LocalTlsConfig) -> Result<(), String> {
    let validated = validate_material(config, None)?;
    let store = detect_trust_store().ok_or_else(|| "This Linux system has no supported trust-store tool (supported: Debian/Ubuntu update-ca-certificates, Fedora/RHEL update-ca-trust). No system trust store was modified.".to_string())?;
    let fingerprint = validated.ca_fingerprint.clone();
    if certificate_trusted(config) {
        // Already trusted (by RDT or by the user); never adopt entries RDT did not record.
        return Ok(());
    }
    // Record intent first so an interrupted install can still be cleaned up by Remove.
    let was_tracked = config.managed_trust_fingerprints.iter().any(|f| f.eq_ignore_ascii_case(&fingerprint));
    if !was_tracked {
        config.managed_trust_fingerprints.push(fingerprint.clone());
        if let Err(e) = save_local_tls_config(state, config) {
            config.managed_trust_fingerprints.retain(|f| !f.eq_ignore_ascii_case(&fingerprint));
            return Err(format!("Could not record the trust entry before installing it; nothing was installed. Detail: {e}"));
        }
    }
    let forget = |config: &mut LocalTlsConfig| {
        if !was_tracked {
            config.managed_trust_fingerprints.retain(|f| !f.eq_ignore_ascii_case(&fingerprint));
            let _ = save_local_tls_config(state, config);
        }
    };
    match run_privileged(store, "install", &fingerprint, Some(&validated.ca)) {
        PrivilegedResult::Done => {}
        PrivilegedResult::Unchanged(msg) => {
            forget(config);
            return Err(msg);
        }
        PrivilegedResult::Unknown(msg) => {
            return Err(format!("{msg} The trust entry stays recorded so Remove RDT trust can clean it up."));
        }
    }
    if let Some(reason) = trust_error(config) {
        // Installed but not verified: undo it and say so.
        let _ = run_privileged(store, "remove", &fingerprint, None);
        forget(config);
        return Err(format!("The CA was installed via {} but the system still does not verify the certificate, so the change was rolled back. Detail: {reason}", store.label()));
    }
    Ok(())
}

pub(crate) fn remove_trust(state: &BackendState, config: &mut LocalTlsConfig) -> Result<(), String> {
    if config.managed_trust_fingerprints.is_empty() {
        return Err("RDT Connect has no tracked CA trust entry to remove. It will not remove certificates installed independently by you or another app.".into());
    }
    let store = detect_trust_store().ok_or_else(|| "No supported Linux trust-store tool was found, so tracked entries cannot be removed safely. Nothing was changed.".to_string())?;
    let mut remaining = Vec::new();
    let mut errors = Vec::new();
    for entry in config.managed_trust_fingerprints.drain(..) {
        match normalize_fingerprint(&entry) {
            None => {
                errors.push(format!("{entry}: not an RDT Linux entry; left untouched"));
                remaining.push(entry);
            }
            Some(fp) => match run_privileged(store, "remove", &fp, None) {
                PrivilegedResult::Done => {}
                PrivilegedResult::Unchanged(msg) | PrivilegedResult::Unknown(msg) => {
                    errors.push(format!("{fp}: {msg}"));
                    remaining.push(entry);
                }
            },
        }
    }
    config.managed_trust_fingerprints = remaining;
    save_local_tls_config(state, config)?;
    if errors.is_empty() {
        Ok(())
    } else {
        Err(format!("Some tracked RDT trust entries were not removed and remain tracked. {}", errors.join("; ")))
    }
}

// ---------------------------------------------------------------------------
// Setup orchestration
// ---------------------------------------------------------------------------

fn remove_old_managed_dir(https_dir: &Path, old_cert: Option<&str>, new_dir: &Path) {
    let Some(old_cert) = old_cert else { return };
    let Some(old_dir) = Path::new(old_cert).parent() else { return };
    if old_dir == new_dir || old_dir.parent() != Some(https_dir) { return; }
    let named = old_dir.file_name().and_then(|n| n.to_str()).map(|n| n.starts_with(MANAGED_DIR_PREFIX)).unwrap_or(false);
    if !named { return; }
    if let Ok(meta) = fs::symlink_metadata(old_dir) {
        if meta.is_dir() && !meta.file_type().is_symlink() && meta.uid() == euid() {
            let _ = fs::remove_dir_all(old_dir);
        }
    }
}

pub(crate) fn setup(state: &BackendState) -> Result<LocalTlsConfig, String> {
    let existing = load_local_tls_config(state)?;
    if validate_material(&existing, None).is_ok() {
        // Reuse valid material; only (re)try trust. Never mint a new CA on every click.
        let mut config = existing;
        config.mode = "https".into();
        save_local_tls_config(state, &config)?;
        trust_ca(state, &mut config).map_err(|e| format!("The existing local certificate files are valid, but system trust was not completed: {e}"))?;
        return Ok(config);
    }
    let generated = generate_tls_files(&state.https_dir)?;
    let mut config = existing.clone();
    config.mode = "https".into();
    config.generated = true;
    config.certificate_path = Some(generated.cert.display().to_string());
    config.private_key_path = Some(generated.key.display().to_string());
    config.ca_certificate_path = Some(generated.ca.display().to_string());
    config.linux_certificate_sha256 = Some(sha256_file_hex(&generated.cert)?);
    config.linux_private_key_sha256 = Some(sha256_file_hex(&generated.key)?);
    config.linux_ca_sha256 = Some(sha256_file_hex(&generated.ca)?);
    if let Err(e) = validate_material(&config, None) {
        let _ = fs::remove_dir_all(&generated.dir);
        return Err(format!("The newly generated certificate failed validation and was discarded: {e}"));
    }
    if let Err(e) = save_local_tls_config(state, &config) {
        let _ = fs::remove_dir_all(&generated.dir);
        return Err(format!("Could not save the HTTPS settings; the new certificate was discarded: {e}"));
    }
    remove_old_managed_dir(&state.https_dir, existing.certificate_path.as_deref(), &generated.dir);
    trust_ca(state, &mut config).map_err(|e| format!("The certificate files were created securely, but system trust was not installed. Use Set up trusted local HTTPS to retry; the files will be reused. Detail: {e}"))?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    struct TempDir(PathBuf);
    impl TempDir {
        fn new(tag: &str) -> Self {
            let path = std::env::temp_dir().join(format!("rdt-linux-tls-test-{tag}-{}", unique_suffix()));
            fs::create_dir_all(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            TempDir(path)
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    fn require_openssl() {
        assert!(Path::new(OPENSSL).is_file(), "these tests need /usr/bin/openssl");
    }

    fn config_for(g: &GeneratedTls) -> LocalTlsConfig {
        let mut c = LocalTlsConfig::default();
        c.mode = "https".into();
        c.generated = true;
        c.certificate_path = Some(g.cert.display().to_string());
        c.private_key_path = Some(g.key.display().to_string());
        c.ca_certificate_path = Some(g.ca.display().to_string());
        c.linux_certificate_sha256 = Some(sha256_file_hex(&g.cert).unwrap());
        c.linux_private_key_sha256 = Some(sha256_file_hex(&g.key).unwrap());
        c.linux_ca_sha256 = Some(sha256_file_hex(&g.ca).unwrap());
        c
    }

    fn generated(tag: &str) -> (TempDir, GeneratedTls) {
        require_openssl();
        let tmp = TempDir::new(tag);
        let g = generate_tls_files(&tmp.0).expect("generation must succeed");
        (tmp, g)
    }

    fn now() -> i64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64 }

    #[test]
    fn san_requires_exact_loopback_names() {
        let ok = parse_san_entries("X509v3 Subject Alternative Name: \n    DNS:localhost, IP Address:127.0.0.1, IP Address:0:0:0:0:0:0:0:1\n");
        assert!(san_covers_loopback(&ok).is_ok());
        let lookalike_ip = parse_san_entries("DNS:localhost, IP Address:127.0.0.10, IP Address:0:0:0:0:0:0:0:1");
        assert!(san_covers_loopback(&lookalike_ip).unwrap_err().contains("IP:127.0.0.1"));
        let lookalike_host = parse_san_entries("DNS:notlocalhost, DNS:localhost.evil.test, IP Address:127.0.0.1, IP Address:0:0:0:0:0:0:0:1");
        assert!(san_covers_loopback(&lookalike_host).unwrap_err().contains("DNS:localhost"));
        let no_v6 = parse_san_entries("DNS:localhost, IP Address:127.0.0.1");
        assert!(san_covers_loopback(&no_v6).unwrap_err().contains("IP:::1"));
        let wrong_v6 = parse_san_entries("DNS:localhost, IP Address:127.0.0.1, IP Address:0:0:0:0:0:0:0:2");
        assert!(san_covers_loopback(&wrong_v6).is_err());
    }

    #[test]
    fn fingerprints_normalize_and_reject_garbage() {
        let colon = "AB:".repeat(31) + "AB";
        assert_eq!(normalize_fingerprint(&colon).unwrap(), "ab".repeat(32));
        assert_eq!(normalize_fingerprint(&"AB".repeat(32)).unwrap(), "ab".repeat(32));
        assert!(normalize_fingerprint("../../etc/passwd").is_none());
        assert!(normalize_fingerprint(&"ab".repeat(20)).is_none());
        assert!(normalize_fingerprint(&"zz".repeat(32)).is_none());
    }

    #[test]
    fn trust_store_detection_is_bounded() {
        assert_eq!(detect_trust_store_with(&|p| p == "/usr/sbin/update-ca-certificates"), Some(TrustStore::DebianCaCertificates));
        assert_eq!(detect_trust_store_with(&|p| p == "/usr/bin/update-ca-trust"), Some(TrustStore::RhelCaTrust));
        assert_eq!(detect_trust_store_with(&|_| false), None);
    }

    #[test]
    fn cancelled_or_denied_install_is_never_success() {
        for code in [126, 127] {
            match map_privileged_exit(Some(code), "") {
                PrivilegedResult::Unchanged(msg) => assert!(msg.contains("No trust change")),
                other => panic!("expected Unchanged, got {other:?}"),
            }
        }
        assert!(matches!(map_privileged_exit(Some(43), "boom"), PrivilegedResult::Unchanged(_)));
        assert!(matches!(map_privileged_exit(Some(44), ""), PrivilegedResult::Unchanged(_)));
        assert!(matches!(map_privileged_exit(None, ""), PrivilegedResult::Unknown(_)));
        assert!(matches!(map_privileged_exit(Some(99), ""), PrivilegedResult::Unknown(_)));
        assert_eq!(map_privileged_exit(Some(0), ""), PrivilegedResult::Done);
    }

    #[test]
    fn privileged_args_reject_untrusted_values() {
        let store = TrustStore::DebianCaCertificates;
        assert!(privileged_args(store, "install; rm -rf /", &"ab".repeat(32), None).is_err());
        assert!(privileged_args(store, "remove", "$(touch /tmp/x)", None).is_err());
        assert!(privileged_args(store, "remove", "../../etc", None).is_err());
        let args = privileged_args(store, "install", &"AB".repeat(32), Some(Path::new("/tmp/a b;c.pem"))).unwrap();
        // Runtime values are separate argv entries, never part of the script text.
        let script = args[1].to_string_lossy().to_string();
        assert!(!script.contains("a b;c.pem") && !script.contains(&"ab".repeat(32)));
        assert_eq!(args[4].to_string_lossy(), "ab".repeat(32));
    }

    #[test]
    fn privileged_script_is_valid_shell_for_both_stores() {
        for store in [TrustStore::DebianCaCertificates, TrustStore::RhelCaTrust] {
            let out = Command::new("/bin/sh").arg("-n").arg("-c").arg(privileged_script(store)).output().unwrap();
            assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        }
    }

    /// Run the real privileged script as the current user against a scratch anchor dir and a
    /// fake refresh command, so install/remove/idempotency/foreign-file logic is exercised.
    fn run_script(dir: &Path, refresh_ok: bool, op: &str, fp: &str, src: &str) -> i32 {
        let script = privileged_script(TrustStore::DebianCaCertificates)
            .replace("/usr/local/share/ca-certificates", &dir.display().to_string())
            .replace("/usr/sbin/update-ca-certificates --fresh", if refresh_ok { "/usr/bin/true" } else { "/bin/false" })
            .replace("/usr/sbin/update-ca-certificates", if refresh_ok { "/usr/bin/true" } else { "/bin/false" });
        Command::new("/bin/sh").arg("-c").arg(script).arg("rdt-trust").arg(op).arg(fp).arg(src).status().unwrap().code().unwrap_or(-1)
    }

    #[test]
    fn privileged_script_install_remove_semantics() {
        let (tmp, g) = generated("script");
        let fp = der_sha256(&g.ca).unwrap();
        let anchors = tmp.0.join("anchors");
        let dest = anchors.join(format!("{TRUST_FILE_PREFIX}{fp}.crt"));
        let ca = g.ca.display().to_string();
        // Wrong fingerprint is refused and nothing is written.
        assert_eq!(run_script(&anchors, true, "install", &"00".repeat(32), &ca), 41);
        assert!(!dest.exists());
        // Failed refresh rolls the file back.
        assert_eq!(run_script(&anchors, false, "install", &fp, &ca), 43);
        assert!(!dest.exists());
        // Install, then idempotent re-install.
        assert_eq!(run_script(&anchors, true, "install", &fp, &ca), 0);
        assert!(dest.is_file());
        assert_eq!(run_script(&anchors, true, "install", &fp, &ca), 0);
        // A foreign file at the managed name is never overwritten or removed.
        fs::remove_file(&dest).unwrap();
        fs::write(&dest, "not ours").unwrap();
        assert_eq!(run_script(&anchors, true, "install", &fp, &ca), 44);
        assert_eq!(run_script(&anchors, true, "remove", &fp, "-"), 44);
        assert_eq!(fs::read_to_string(&dest).unwrap(), "not ours");
        // A symlink at the managed name is refused.
        fs::remove_file(&dest).unwrap();
        symlink("/etc/hostname", &dest).unwrap();
        assert_eq!(run_script(&anchors, true, "remove", &fp, "-"), 44);
        fs::remove_file(&dest).unwrap();
        // Remove own entry; removing an absent entry is idempotent.
        assert_eq!(run_script(&anchors, true, "install", &fp, &ca), 0);
        assert_eq!(run_script(&anchors, true, "remove", &fp, "-"), 0);
        assert!(!dest.exists());
        assert_eq!(run_script(&anchors, true, "remove", &fp, "-"), 0);
        // Malformed fingerprint / op are rejected.
        assert_eq!(run_script(&anchors, true, "install", "../x", &ca), 50);
        assert_eq!(run_script(&anchors, true, "wipe", &fp, &ca), 50);
    }

    #[test]
    fn generated_material_is_private_validated_and_has_no_ca_key() {
        let (tmp, g) = generated("gen");
        let config = config_for(&g);
        let v = validate_material(&config, None).expect("fresh material validates");
        assert_eq!(v.ca_fingerprint.len(), 64);
        assert!(!g.dir.join("ca-key.pem").exists(), "CA private key must be deleted");
        assert_eq!(fs::metadata(&g.key).unwrap().mode() & 0o777, 0o600);
        assert_eq!(fs::metadata(&g.dir).unwrap().mode() & 0o777, 0o700);
        let leftovers: Vec<_> = fs::read_dir(&tmp.0).unwrap().flatten().map(|e| e.file_name().to_string_lossy().to_string()).filter(|n| n.starts_with(STAGING_PREFIX)).collect();
        assert!(leftovers.is_empty(), "staging must not remain: {leftovers:?}");
    }

    #[test]
    fn expired_and_not_yet_valid_are_rejected() {
        let (_tmp, g) = generated("dates");
        let config = config_for(&g);
        let expired = validate_material(&config, Some(now() + 2000 * 86400)).err().expect("expired must fail");
        assert!(expired.contains("expired"), "{expired}");
        let early = validate_material(&config, Some(now() - 30 * 86400)).err().expect("not yet valid must fail");
        assert!(early.contains("not yet valid"), "{early}");
    }

    #[test]
    fn key_mismatch_is_rejected_even_when_repinned() {
        let (_tmp, g) = generated("mismatch");
        let other = g.dir.join("other-key.pem");
        openssl_ok([OsStr::new("genpkey"), OsStr::new("-algorithm"), OsStr::new("RSA"), OsStr::new("-out"), other.as_os_str()], "make a key").unwrap();
        fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
        fs::rename(&other, &g.key).unwrap();
        fs::set_permissions(&g.key, fs::Permissions::from_mode(0o600)).unwrap();
        let config = config_for(&g); // pins recomputed for the swapped key
        let err = validate_material(&config, None).err().expect("must fail");
        assert!(err.contains("do not match"), "{err}");
    }

    #[test]
    fn altered_files_and_symlinks_are_rejected() {
        let (_tmp, g) = generated("tamper");
        let config = config_for(&g);
        let mut altered = fs::read_to_string(&g.cert).unwrap();
        altered.push('\n');
        fs::write(&g.cert, altered).unwrap();
        assert!(validate_material(&config, None).err().unwrap().contains("changed after setup"));

        let (_tmp2, g2) = generated("symlink");
        let real = g2.dir.join("real-key.pem");
        fs::rename(&g2.key, &real).unwrap();
        symlink(&real, &g2.key).unwrap();
        assert!(validate_material(&config_for(&g2), None).err().unwrap().contains("symlink"));

        let (_tmp3, g3) = generated("perm");
        fs::set_permissions(&g3.key, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(validate_material(&config_for(&g3), None).err().unwrap().contains("unsafe permissions"));
    }

    #[test]
    fn linked_or_unpinned_material_is_refused() {
        let (_tmp, g) = generated("unpinned");
        let mut config = config_for(&g);
        config.linux_ca_sha256 = None;
        assert!(validate_material(&config, None).err().unwrap().contains("integrity record"));
        let mut config = config_for(&g);
        config.generated = false;
        assert!(validate_material(&config, None).err().unwrap().contains("only RDT-generated"));
    }

    #[test]
    fn trust_comes_from_bundle_verification_not_file_existence() {
        let (tmp, g) = generated("trust");
        // The CA file exists on disk, but a bundle without it must NOT verify the leaf.
        let (_tmp_other, other) = generated("other-ca");
        assert!(g.ca.is_file());
        let err = verify_leaf_against_bundle(&other.ca, &g.cert, None).err().expect("untrusted bundle must fail");
        assert!(err.contains("does not chain"), "{err}");
        // A bundle that contains the CA verifies it (what a real system bundle looks like).
        let bundle = tmp.0.join("bundle.pem");
        let mut text = fs::read_to_string(&other.ca).unwrap();
        text.push_str(&fs::read_to_string(&g.ca).unwrap());
        fs::write(&bundle, text).unwrap();
        verify_leaf_against_bundle(&bundle, &g.cert, None).expect("bundle containing the CA verifies");
    }

    #[test]
    fn status_never_reports_trusted_without_valid_material() {
        let config = LocalTlsConfig::default();
        assert!(!certificate_trusted(&config));
        let status = super::super::local_https_status_from_config(&config);
        assert!(!status.trusted && !status.configured);
        assert!(status.mode.eq_ignore_ascii_case("https"), "Linux default must be HTTPS, never a silent HTTP fallback");
    }

    #[test]
    fn config_compatibility_old_files_and_new_fields() {
        let old: LocalTlsConfig = serde_json::from_str(r#"{"mode":"http","generated":false}"#).unwrap();
        assert!(old.linux_ca_sha256.is_none() && old.managed_trust_fingerprints.is_empty());
        assert_eq!(old.mode, "http");
        let empty: LocalTlsConfig = serde_json::from_str("{}").unwrap();
        assert_eq!(empty.mode, "https");
        let (_tmp, g) = generated("serde");
        let json = serde_json::to_string(&config_for(&g)).unwrap();
        assert!(json.contains("linuxCaSha256") && json.contains("windowsCertificateSha256"));
        let back: LocalTlsConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(back.linux_ca_sha256, config_for(&g).linux_ca_sha256);
    }
}

//! Linux native GGML catalog, cache verification and scoped cleanup.
//!
//! The Python `backend/whisper_cpp/model_registry.py` is the download authority.
//! Its approved model IDs, immutable revision, filenames and SHA-256 pins are
//! mirrored here because the desktop must independently verify cache readiness
//! before it can show a model as ready. Unit tests keep the two catalogs aligned.
use serde_json::Value;
use std::collections::HashSet;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

pub(crate) const SOURCE_REPO: &str = "ggerganov/whisper.cpp";
pub(crate) const REVISION: &str = "fb5ed9369c26eaf08392504d8446da431e0fffb9";
const MAX_MODEL_BYTES: u64 = 4_000_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ModelSpec {
    pub(crate) id: &'static str,
    pub(crate) name: &'static str,
    pub(crate) filename: &'static str,
    pub(crate) quantization: &'static str,
    pub(crate) sha256: &'static str,
    pub(crate) approximate_bytes: u64,
    pub(crate) description: &'static str,
    pub(crate) recommended: bool,
}

pub(crate) const MODELS: [ModelSpec; 5] = [
    ModelSpec {
        id: "tiny",
        name: "Tiny",
        filename: "ggml-tiny-q8_0.bin",
        quantization: "Q8_0",
        sha256: "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca",
        approximate_bytes: 43_500_000,
        description: "Fastest, smallest GGML model",
        recommended: false,
    },
    ModelSpec {
        id: "base",
        name: "Base",
        filename: "ggml-base-q8_0.bin",
        quantization: "Q8_0",
        sha256: "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9",
        approximate_bytes: 81_800_000,
        description: "Lightweight GGML choice",
        recommended: false,
    },
    ModelSpec {
        id: "small",
        name: "Small",
        filename: "ggml-small-q8_0.bin",
        quantization: "Q8_0",
        sha256: "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f",
        approximate_bytes: 264_000_000,
        description: "Balanced GGML model",
        recommended: false,
    },
    ModelSpec {
        id: "large-v3-turbo",
        name: "Large v3 Turbo",
        filename: "ggml-large-v3-turbo-q5_0.bin",
        quantization: "Q5_0",
        sha256: "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        approximate_bytes: 574_041_195,
        description: "Strong balance of speed and quality",
        recommended: true,
    },
    ModelSpec {
        id: "large-v3",
        name: "Large v3",
        filename: "ggml-large-v3-q5_0.bin",
        quantization: "Q5_0",
        sha256: "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
        approximate_bytes: 1_080_000_000,
        description: "Largest GGML model in this list",
        recommended: false,
    },
];

pub(crate) fn spec(model_id: &str) -> Option<&'static ModelSpec> {
    MODELS.iter().find(|entry| entry.id == model_id)
}

fn verified_sidecar(artifact: &Path) -> PathBuf {
    artifact.with_file_name(format!(
        "{}.rdt-verified.json",
        artifact.file_name().unwrap_or_default().to_string_lossy()
    ))
}

fn regular_non_symlink(path: &Path) -> Result<fs::Metadata, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "unsafe non-regular model-cache file: {}",
            path.display()
        ));
    }
    Ok(metadata)
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct FileIdentity {
    device: u64,
    inode: u64,
    length: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

impl From<&fs::Metadata> for FileIdentity {
    fn from(metadata: &fs::Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            length: metadata.len(),
            modified_seconds: metadata.mtime(),
            modified_nanoseconds: metadata.mtime_nsec(),
            changed_seconds: metadata.ctime(),
            changed_nanoseconds: metadata.ctime_nsec(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct VerificationKey {
    canonical_path: PathBuf,
    model_id: String,
    expected_sha256: String,
    artifact: FileIdentity,
    sidecar: FileIdentity,
}

static VERIFIED_MODELS: OnceLock<Mutex<HashSet<VerificationKey>>> = OnceLock::new();

fn verification_cache() -> &'static Mutex<HashSet<VerificationKey>> {
    VERIFIED_MODELS.get_or_init(|| Mutex::new(HashSet::new()))
}

#[cfg(test)]
static HASH_COUNTS: OnceLock<Mutex<std::collections::HashMap<PathBuf, usize>>> = OnceLock::new();

#[cfg(test)]
fn record_full_hash(path: &Path) {
    let counts = HASH_COUNTS.get_or_init(|| Mutex::new(std::collections::HashMap::new()));
    let mut counts = counts
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *counts.entry(path.to_path_buf()).or_insert(0) += 1;
}

#[cfg(test)]
fn full_hash_count(path: &Path) -> usize {
    HASH_COUNTS
        .get_or_init(|| Mutex::new(std::collections::HashMap::new()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .get(path)
        .copied()
        .unwrap_or(0)
}

fn file_identity(path: &Path) -> Result<FileIdentity, String> {
    let metadata = regular_non_symlink(path)?;
    Ok(FileIdentity::from(&metadata))
}

fn invalidate_cache_for_path(path: &Path) {
    let mut cache = verification_cache()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    cache.retain(|key| key.canonical_path != path);
}

/// Invalidate successful verification before a model can be replaced or deleted.
pub(crate) fn invalidate_model_cache(root: &Path, model_id: &str) {
    let Some(entry) = spec(model_id) else {
        return;
    };
    let root_path = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    invalidate_cache_for_path(&root_path.join(entry.filename));
}

fn verify_artifact_with_expected_sha(
    root: &Path,
    entry: &ModelSpec,
    expected_sha: &str,
) -> Result<PathBuf, String> {
    let root_metadata = fs::symlink_metadata(root)
        .map_err(|error| format!("model storage root is unavailable: {error}"))?;
    if root_metadata.file_type().is_symlink() || !root_metadata.is_dir() {
        return Err("model storage root must be a normal directory, not a symlink".into());
    }
    let canonical_root = fs::canonicalize(root)
        .map_err(|error| format!("cannot resolve model storage root: {error}"))?;
    let model_dir = root;
    let model_dir_metadata = fs::symlink_metadata(&model_dir)
        .map_err(|error| format!("model directory is unavailable: {error}"))?;
    if model_dir_metadata.file_type().is_symlink() || !model_dir_metadata.is_dir() {
        return Err("model directory must be a normal directory".into());
    }
    let canonical_model_dir = fs::canonicalize(&model_dir)
        .map_err(|error| format!("cannot resolve model directory: {error}"))?;
    if !canonical_model_dir.starts_with(&canonical_root) {
        return Err("model directory escapes the configured storage root".into());
    }

    let artifact = model_dir.join(entry.filename);
    let metadata = regular_non_symlink(&artifact)?;
    let canonical_artifact = fs::canonicalize(&artifact)
        .map_err(|error| format!("cannot resolve model artifact: {error}"))?;
    if !canonical_artifact.starts_with(&canonical_model_dir)
        || metadata.len() == 0
        || metadata.len() > MAX_MODEL_BYTES
    {
        return Err("model artifact path or size is invalid".into());
    }
    let sidecar = verified_sidecar(&artifact);
    let sidecar_metadata = regular_non_symlink(&sidecar)?;
    if !fs::canonicalize(&sidecar)
        .map_err(|error| format!("cannot resolve model sidecar: {error}"))?
        .starts_with(&canonical_model_dir)
    {
        return Err("model sidecar escapes the GGML model directory".into());
    }
    if sidecar_metadata.len() == 0 || sidecar_metadata.len() > 16 * 1024 {
        return Err("model sidecar size is invalid".into());
    }
    let sidecar_bytes =
        fs::read(&sidecar).map_err(|error| format!("cannot read model sidecar: {error}"))?;
    let sidecar_json: Value = serde_json::from_slice(&sidecar_bytes)
        .map_err(|_| "model sidecar is invalid JSON".to_string())?;
    let expected_url = format!(
        "https://huggingface.co/{SOURCE_REPO}/resolve/{REVISION}/{}",
        entry.filename
    );
    if sidecar_json.get("schema_version").and_then(Value::as_u64) != Some(1)
        || sidecar_json.get("model_id").and_then(Value::as_str) != Some(entry.id)
        || sidecar_json.get("repo").and_then(Value::as_str) != Some(SOURCE_REPO)
        || sidecar_json.get("revision").and_then(Value::as_str) != Some(REVISION)
        || sidecar_json.get("url").and_then(Value::as_str) != Some(expected_url.as_str())
        || sidecar_json.get("filename").and_then(Value::as_str) != Some(entry.filename)
        || sidecar_json.get("quantization").and_then(Value::as_str) != Some(entry.quantization)
        || sidecar_json.get("sha256").and_then(Value::as_str) != Some(expected_sha)
        || sidecar_json.get("size_bytes").and_then(Value::as_u64) != Some(metadata.len())
    {
        return Err("model sidecar identity, revision, checksum or size does not match the approved registry".into());
    }

    // Key successful verification by canonical path and stable artifact/sidecar identity.
    // This avoids hashing hundreds of MB on every UI refresh while detecting ordinary
    // replacements and in-place changes through inode, length, mtime and ctime.
    let artifact_identity = FileIdentity::from(&metadata);
    let sidecar_identity = FileIdentity::from(&sidecar_metadata);
    let key = VerificationKey {
        canonical_path: canonical_artifact.clone(),
        model_id: entry.id.to_string(),
        expected_sha256: expected_sha.to_string(),
        artifact: artifact_identity.clone(),
        sidecar: sidecar_identity.clone(),
    };
    let artifact_now = file_identity(&artifact)?;
    let sidecar_now = file_identity(&sidecar)?;
    let canonical_now = fs::canonicalize(&artifact)
        .map_err(|error| format!("cannot re-resolve model artifact: {error}"))?;
    if artifact_now != artifact_identity
        || sidecar_now != sidecar_identity
        || canonical_now != canonical_artifact
    {
        invalidate_cache_for_path(&canonical_artifact);
        return Err(
            "model artifact or sidecar changed while verification was being prepared".into(),
        );
    }
    {
        let cache = verification_cache()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if cache.contains(&key) {
            return Ok(canonical_artifact);
        }
    }

    // A cache miss must hash the complete artifact. The Python registry independently
    // performs this same full-byte check before any inference process uses the model.
    #[cfg(test)]
    record_full_hash(&canonical_artifact);
    let actual_sha = super::sha256_file_hex(&canonical_artifact)?;
    if actual_sha != expected_sha {
        invalidate_cache_for_path(&canonical_artifact);
        return Err("full model SHA-256 does not match the approved registry".into());
    }
    // Reject concurrent replacement/modification during hashing; cache only a stable read.
    let artifact_after = file_identity(&artifact)?;
    let sidecar_after = file_identity(&sidecar)?;
    let canonical_after = fs::canonicalize(&artifact)
        .map_err(|error| format!("cannot re-resolve model artifact after hashing: {error}"))?;
    if artifact_after != artifact_identity
        || sidecar_after != sidecar_identity
        || canonical_after != canonical_artifact
    {
        invalidate_cache_for_path(&canonical_artifact);
        return Err("model artifact or sidecar changed during full-file verification".into());
    }
    let mut cache = verification_cache()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    cache.retain(|cached| cached.canonical_path != canonical_artifact);
    cache.insert(key);
    Ok(canonical_artifact)
}

pub(crate) fn verify_model(root: &Path, model_id: &str) -> Result<PathBuf, String> {
    let entry = spec(model_id).ok_or_else(|| format!("unsupported GGML model ID: {model_id}"))?;
    verify_artifact_with_expected_sha(root, entry, entry.sha256)
}

pub(crate) fn model_cache_has_weights(root: &Path, model_id: &str) -> bool {
    verify_model(root, model_id).is_ok()
}

/// Deletes only the selected model's known artifact, verification sidecar, and
/// downloader staging files. It never follows or deletes symlinks and preflights
/// all candidates before removing any of them.
pub(crate) fn delete_model_cache(root: &Path, model_id: &str) -> Result<u64, String> {
    let entry = spec(model_id).ok_or_else(|| format!("unsupported GGML model ID: {model_id}"))?;
    invalidate_model_cache(root, model_id);
    let root_meta = match fs::symlink_metadata(root) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(format!("cannot inspect model storage root: {error}")),
    };
    if root_meta.file_type().is_symlink() || !root_meta.is_dir() {
        return Err("model storage root must be a normal directory, not a symlink".into());
    }
    let model_dir = root;
    let dir_meta = match fs::symlink_metadata(&model_dir) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(format!("cannot inspect GGML model directory: {error}")),
    };
    if dir_meta.file_type().is_symlink() || !dir_meta.is_dir() {
        return Err("model directory must be a normal directory".into());
    }
    let canonical_dir = fs::canonicalize(&model_dir)
        .map_err(|error| format!("cannot resolve model directory: {error}"))?;
    let canonical_root = fs::canonicalize(root)
        .map_err(|error| format!("cannot resolve model storage root: {error}"))?;
    if !canonical_dir.starts_with(&canonical_root) {
        return Err("model directory escapes the configured storage root".into());
    }

    let artifact = model_dir.join(entry.filename);
    let sidecar = verified_sidecar(&artifact);
    let part = artifact.with_file_name(format!("{}.part", entry.filename));
    let sidecar_part = sidecar.with_file_name(format!(
        "{}.part",
        sidecar.file_name().unwrap_or_default().to_string_lossy()
    ));
    let candidates = [artifact, sidecar, part, sidecar_part];
    let mut existing: Vec<(PathBuf, u64)> = Vec::new();
    for path in candidates {
        match fs::symlink_metadata(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(format!(
                    "cannot inspect model cleanup candidate {}: {error}",
                    path.display()
                ))
            }
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    return Err(format!(
                        "refusing to delete unsafe model cleanup candidate: {}",
                        path.display()
                    ));
                }
                let resolved = fs::canonicalize(&path)
                    .map_err(|error| format!("cannot resolve model cleanup candidate: {error}"))?;
                if !resolved.starts_with(&canonical_dir) {
                    return Err(
                        "model cleanup candidate escapes the approved GGML directory".into(),
                    );
                }
                existing.push((path, metadata.len()));
            }
        }
    }
    let mut bytes = 0u64;
    for (path, size) in existing {
        // Recheck the file type at the deletion boundary to reject a replaced symlink.
        let metadata = fs::symlink_metadata(&path)
            .map_err(|error| format!("cannot recheck model cleanup candidate: {error}"))?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(format!(
                "refusing to delete replaced unsafe cleanup candidate: {}",
                path.display()
            ));
        }
        fs::remove_file(&path)
            .map_err(|error| format!("could not delete {}: {error}", path.display()))?;
        bytes = bytes.saturating_add(size);
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);

    fn temp_root(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "rdt-ggml-{label}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&path).expect("create isolated test root");
        path
    }
    fn test_spec() -> ModelSpec {
        ModelSpec {
            id: "tiny",
            name: "Tiny",
            filename: "ggml-tiny-q8_0.bin",
            quantization: "Q8_0",
            sha256: "unused-by-test-helper",
            approximate_bytes: 1,
            description: "test",
            recommended: false,
        }
    }
    fn write_sidecar(root: &Path, entry: &ModelSpec, payload: &[u8], digest: &str) {
        let file = root.join(entry.filename);
        fs::write(&file, payload).expect("write fixture model");
        let value = serde_json::json!({
            "schema_version": 1, "model_id": entry.id, "repo": SOURCE_REPO,
            "revision": REVISION, "url": format!("https://huggingface.co/{SOURCE_REPO}/resolve/{REVISION}/{}", entry.filename),
            "filename": entry.filename, "quantization": entry.quantization,
            "size_bytes": payload.len(), "sha256": digest,
        });
        fs::write(
            verified_sidecar(&file),
            serde_json::to_vec(&value).expect("encode sidecar"),
        )
        .expect("write sidecar");
    }
    fn digest(payload: &[u8]) -> String {
        let path = std::env::temp_dir().join(format!(
            "rdt-ggml-hash-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::write(&path, payload).expect("write hash fixture");
        let hash = super::super::sha256_file_hex(&path).expect("hash fixture");
        let _ = fs::remove_file(path);
        hash
    }
    #[test]
    fn catalog_has_only_five_approved_logical_ids() {
        assert_eq!(
            MODELS.map(|entry| entry.id),
            ["tiny", "base", "small", "large-v3-turbo", "large-v3"]
        );
        assert!(spec("other/model").is_none());
    }
    #[test]
    fn complete_file_and_matching_sidecar_are_verified_by_full_hash() {
        let root = temp_root("verified");
        let entry = test_spec();
        let payload = b"synthetic bytes for hash verification";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        assert!(verify_artifact_with_expected_sha(&root, &entry, &hash).is_ok());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[test]
    fn unchanged_verified_model_reuses_full_hash_result() {
        let root = temp_root("cache-reuse");
        let entry = test_spec();
        let payload = b"stable synthetic GGML payload";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        let artifact = fs::canonicalize(root.join(entry.filename)).expect("canonical artifact");
        verify_artifact_with_expected_sha(&root, &entry, &hash).expect("first full hash");
        verify_artifact_with_expected_sha(&root, &entry, &hash).expect("cached verification");
        assert_eq!(
            full_hash_count(&artifact),
            1,
            "unchanged file should be hashed only once"
        );
        fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn verification_cache_invalidates_explicitly_and_on_replacement() {
        let root = temp_root("cache-invalidate");
        let entry = test_spec();
        let payload = b"same size payload A";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        let artifact_path = root.join(entry.filename);
        let canonical = fs::canonicalize(&artifact_path).expect("canonical artifact");
        verify_artifact_with_expected_sha(&root, &entry, &hash).expect("first full hash");
        invalidate_cache_for_path(&canonical);
        verify_artifact_with_expected_sha(&root, &entry, &hash).expect("rehash after invalidation");
        assert_eq!(
            full_hash_count(&canonical),
            2,
            "explicit invalidation must force rehash"
        );

        // Replace with identical bytes: content/hash/size stay the same but inode changes.
        let replacement = root.join("replacement.bin");
        fs::write(&replacement, payload).expect("replacement bytes");
        fs::rename(&replacement, &artifact_path).expect("atomic model replacement");
        verify_artifact_with_expected_sha(&root, &entry, &hash).expect("rehash after replacement");
        assert_eq!(
            full_hash_count(&canonical),
            3,
            "replacement identity must force rehash"
        );
        fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn modified_or_truncated_bytes_are_rejected_even_with_stale_sidecar() {
        let root = temp_root("corrupt");
        let entry = test_spec();
        let payload = b"synthetic bytes for hash verification";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        fs::write(root.join(entry.filename), b"short").expect("truncate fixture");
        assert!(verify_artifact_with_expected_sha(&root, &entry, &hash).is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[test]
    fn same_size_modified_bytes_are_rejected_even_with_matching_sidecar_size() {
        let root = temp_root("same-size-corruption");
        let entry = test_spec();
        let payload = b"synthetic bytes for hash verification";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        fs::write(
            root.join(entry.filename),
            b"Xynthetic bytes for hash verification",
        )
        .expect("mutate bytes without changing size");
        assert!(verify_artifact_with_expected_sha(&root, &entry, &hash).is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[test]
    fn stale_revision_metadata_is_rejected() {
        let root = temp_root("stale");
        let entry = test_spec();
        let payload = b"synthetic bytes";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        let sidecar = verified_sidecar(&root.join(entry.filename));
        let mut json: Value = serde_json::from_slice(&fs::read(&sidecar).expect("read sidecar"))
            .expect("parse sidecar");
        json["revision"] = Value::String("0".repeat(40));
        fs::write(&sidecar, serde_json::to_vec(&json).expect("encode sidecar"))
            .expect("stale sidecar");
        assert!(verify_artifact_with_expected_sha(&root, &entry, &hash).is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[test]
    fn mismatched_sidecar_size_is_rejected() {
        let root = temp_root("bad-size");
        let entry = test_spec();
        let payload = b"synthetic bytes";
        let hash = digest(payload);
        write_sidecar(&root, &entry, payload, &hash);
        let sidecar = verified_sidecar(&root.join(entry.filename));
        let mut json: Value = serde_json::from_slice(&fs::read(&sidecar).expect("read sidecar"))
            .expect("parse sidecar");
        json["size_bytes"] = Value::from(payload.len() + 1);
        fs::write(&sidecar, serde_json::to_vec(&json).expect("encode sidecar"))
            .expect("wrong size");
        assert!(verify_artifact_with_expected_sha(&root, &entry, &hash).is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[test]
    fn deletion_is_scoped_to_one_model_and_preserves_unrelated_files() {
        let root = temp_root("delete");
        let entry = spec("tiny").expect("known model");
        let ggml = root.as_path();
        fs::write(ggml.join(entry.filename), b"not a verified real model").expect("artifact");
        fs::write(verified_sidecar(&ggml.join(entry.filename)), b"{}").expect("sidecar");
        fs::write(ggml.join(format!("{}.part", entry.filename)), b"partial").expect("partial");
        fs::write(ggml.join("unrelated.bin"), b"keep").expect("unrelated");
        let removed = delete_model_cache(&root, "tiny").expect("scoped deletion");
        assert!(removed > 0);
        assert!(!ggml.join(entry.filename).exists());
        assert!(ggml.join("unrelated.bin").is_file());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[cfg(unix)]
    #[test]
    fn symlinked_model_artifact_is_not_verified_or_deleted() {
        use std::os::unix::fs::symlink;
        let root = temp_root("symlink");
        let entry = spec("tiny").expect("known model");
        let external = root.join("outside.bin");
        fs::write(&external, b"outside data").expect("external data");
        symlink(&external, root.join(entry.filename)).expect("create symlink");
        assert!(verify_model(&root, "tiny").is_err());
        assert!(delete_model_cache(&root, "tiny").is_err());
        assert_eq!(
            fs::read(&external).expect("external remains"),
            b"outside data"
        );
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[cfg(unix)]
    #[test]
    fn symlinked_storage_root_is_rejected() {
        use std::os::unix::fs::symlink;
        let real = temp_root("real-root");
        let link = std::env::temp_dir().join(format!(
            "rdt-ggml-root-link-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        symlink(&real, &link).expect("create root symlink");
        assert!(verify_model(&link, "tiny").is_err());
        assert!(delete_model_cache(&link, "tiny").is_err());
        fs::remove_file(link).expect("remove link");
        fs::remove_dir_all(real).expect("cleanup real root");
    }
    #[test]
    fn partial_download_alone_is_never_a_ready_model() {
        let root = temp_root("partial");
        let entry = spec("tiny").expect("known model");
        fs::write(
            root.join(format!("{}.part", entry.filename)),
            b"interrupted",
        )
        .expect("partial");
        assert!(!model_cache_has_weights(&root, "tiny"));
        fs::remove_dir_all(root).expect("cleanup");
    }
}

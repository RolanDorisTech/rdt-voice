//! Non-destructive Linux/XDG path selection for RDT Connect.
use std::fs;
use std::path::{Path, PathBuf};

pub(crate) fn choose_data_dir(
    home: &Path,
    app_name: &str,
    xdg_data_home: Option<&Path>,
    legacy_exists: bool,
) -> PathBuf {
    let legacy = home
        .join("Library")
        .join("Application Support")
        .join(app_name);
    if legacy_exists {
        return legacy;
    }
    let base = xdg_data_home
        .filter(|path| path.is_absolute())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home.join(".local").join("share"));
    base.join(app_name)
}

pub(crate) fn choose_state_dir(
    home: &Path,
    app_name: &str,
    xdg_state_home: Option<&Path>,
    legacy_exists: bool,
) -> PathBuf {
    let legacy = home.join("Library").join("Logs").join(app_name);
    if legacy_exists {
        return legacy;
    }
    let base = xdg_state_home
        .filter(|path| path.is_absolute())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home.join(".local").join("state"));
    base.join(app_name)
}

fn legacy_directory_exists(path: &Path, kind: &str) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => Err(format!(
            "Refusing to reuse the legacy {kind} path because it is a symbolic link: {}",
            path.display()
        )),
        Ok(metadata) if metadata.is_dir() => Ok(true),
        Ok(_) => Err(format!(
            "The legacy {kind} path exists but is not a directory: {}",
            path.display()
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!(
            "Could not inspect the legacy {kind} path {}: {error}",
            path.display()
        )),
    }
}

/// Validate every existing component of a selected application-data or log path
/// before create_dir_all or chmod is allowed to touch it. The check deliberately
/// rejects symlink components and existing non-directories rather than following
/// an XDG path into an unrelated location.
pub(crate) fn validate_directory_target(path: &Path, kind: &str) -> Result<(), String> {
    if !path.is_absolute() {
        return Err(format!(
            "The selected {kind} destination must be absolute: {}",
            path.display()
        ));
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(format!("Refusing the selected {kind} destination because a path component is a symbolic link: {}", current.display()));
            }
            Ok(metadata) if !metadata.is_dir() => {
                return Err(format!(
                    "The selected {kind} destination contains a non-directory component: {}",
                    current.display()
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(format!(
                    "Could not inspect the selected {kind} destination {}: {error}",
                    current.display()
                ))
            }
        }
    }
    Ok(())
}

pub(crate) fn application_support_dir(app_name: &str) -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from).ok_or_else(|| {
        "HOME is unavailable; cannot resolve the RDT Connect data directory".to_string()
    })?;
    let legacy = home
        .join("Library")
        .join("Application Support")
        .join(app_name);
    let xdg = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from);
    let selected = choose_data_dir(
        &home,
        app_name,
        xdg.as_deref(),
        legacy_directory_exists(&legacy, "application-support")?,
    );
    validate_directory_target(&selected, "application-data")?;
    Ok(selected)
}

pub(crate) fn application_log_dir(app_name: &str) -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from).ok_or_else(|| {
        "HOME is unavailable; cannot resolve the RDT Connect state directory".to_string()
    })?;
    let legacy = home.join("Library").join("Logs").join(app_name);
    let xdg = std::env::var_os("XDG_STATE_HOME").map(PathBuf::from);
    let selected = choose_state_dir(
        &home,
        app_name,
        xdg.as_deref(),
        legacy_directory_exists(&legacy, "log")?,
    );
    validate_directory_target(&selected, "application-log")?;
    Ok(selected)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_https_support_is_kept_when_it_exists() {
        let home = Path::new("/tmp/a home with spaces");
        let path = choose_data_dir(home, "RDT Connect", Some(Path::new("/tmp/xdg data")), true);
        assert_eq!(path, home.join("Library/Application Support/RDT Connect"));
    }
    #[test]
    fn new_install_uses_absolute_xdg_data_home() {
        let home = Path::new("/tmp/user");
        assert_eq!(
            choose_data_dir(home, "RDT Connect", Some(Path::new("/tmp/data")), false),
            Path::new("/tmp/data/RDT Connect")
        );
        assert_eq!(
            choose_data_dir(home, "RDT Connect", None, false),
            Path::new("/tmp/user/.local/share/RDT Connect")
        );
    }
    #[test]
    fn relative_xdg_overrides_are_ignored() {
        let home = Path::new("/tmp/user");
        assert_eq!(
            choose_data_dir(home, "RDT Connect", Some(Path::new("relative")), false),
            Path::new("/tmp/user/.local/share/RDT Connect")
        );
    }
    #[cfg(unix)]
    #[test]
    fn legacy_symlink_is_rejected_instead_of_followed() {
        use std::os::unix::fs::symlink;
        let root = std::env::temp_dir().join(format!("rdt-path-symlink-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("real")).expect("create target directory");
        symlink(root.join("real"), root.join("legacy-link")).expect("create legacy symlink");
        assert!(legacy_directory_exists(&root.join("legacy-link"), "application-support").is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }
    #[cfg(unix)]
    #[test]
    fn existing_symlinked_xdg_application_data_destination_is_rejected() {
        use std::os::unix::fs::symlink;
        let root = std::env::temp_dir().join(format!("rdt-xdg-target-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let xdg = root.join("xdg-data");
        fs::create_dir_all(&xdg).expect("create XDG data root");
        let real = root.join("real-data");
        fs::create_dir_all(&real).expect("create real destination");
        let target = xdg.join("RDT Connect");
        symlink(&real, &target).expect("create symlink at XDG destination");
        assert!(validate_directory_target(&target, "application-data").is_err());
        fs::remove_dir_all(root).expect("cleanup");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_xdg_state_root_is_rejected_before_log_creation() {
        use std::os::unix::fs::symlink;
        let root = std::env::temp_dir().join(format!("rdt-xdg-state-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("real-state")).expect("create real state root");
        let xdg_state = root.join("state-link");
        symlink(root.join("real-state"), &xdg_state).expect("create XDG state symlink");
        assert!(
            validate_directory_target(&xdg_state.join("RDT Connect"), "application-log").is_err()
        );
        fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn legacy_logs_are_preserved_and_new_logs_use_xdg_state() {
        let home = Path::new("/tmp/home with spaces");
        assert_eq!(
            choose_state_dir(home, "RDT Connect", None, true),
            home.join("Library/Logs/RDT Connect")
        );
        assert_eq!(
            choose_state_dir(home, "RDT Connect", Some(Path::new("/tmp/state")), false),
            Path::new("/tmp/state/RDT Connect")
        );
        assert_eq!(
            choose_state_dir(home, "RDT Connect", None, false),
            Path::new("/tmp/home with spaces/.local/state/RDT Connect")
        );
    }
}

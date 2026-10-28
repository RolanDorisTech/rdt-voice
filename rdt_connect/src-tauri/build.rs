use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=RDT_BUILD_ID");
    println!("cargo:rerun-if-env-changed=CARGO_CFG_TARGET_OS");
    println!("cargo:rerun-if-env-changed=CARGO_CFG_TARGET_ARCH");
    println!("cargo:rerun-if-env-changed=CARGO_PKG_VERSION");

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_arch = env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default();
    let build_id = match env::var("RDT_BUILD_ID") {
        Ok(value) if is_valid_build_id_for_target(value.trim(), &target_os, &target_arch) => value.trim().to_string(),
        Ok(value) if !value.trim().is_empty() && value.trim() != "dev-unmarked" => {
            panic!("Invalid RDT_BUILD_ID '{value}' for target '{target_os}'. Expected a marker in that platform's reserved namespace.");
        }
        _ => source_build_id(&target_os),
    };
    verify_package_version_marker(&build_id);
    println!("cargo:rustc-env=RDT_BUILD_ID={build_id}");
    tauri_build::build();
}

fn is_valid_build_id_for_target(value: &str, target_os: &str, target_arch: &str) -> bool {
    let bytes = value.as_bytes();

    match target_os {
        "macos" => {
            bytes.len() == 4
                && bytes[0] == b'1'
                && bytes[1].is_ascii_uppercase()
                && bytes[2].is_ascii_digit()
                && bytes[3].is_ascii_digit()
                && target_arch == "aarch64"
        }
        "windows" => {
            bytes.len() == 4
                && bytes[1].is_ascii_uppercase()
                && bytes[2].is_ascii_digit()
                && bytes[3].is_ascii_digit()
                && ((bytes[0] == b'2' && target_arch == "aarch64")
                    || (bytes[0] == b'3' && target_arch == "x86_64"))
        }
        "linux" => {
            bytes.len() == 5
                && (bytes[0] == b'4' || bytes[0] == b'5')
                && bytes[1] == b'L'
                && bytes[2..].iter().all(u8::is_ascii_digit)
                && ((bytes[0] == b'4' && target_arch == "aarch64")
                    || (bytes[0] == b'5' && target_arch == "x86_64"))
        }
        _ => false,
    }
}

fn verify_package_version_marker(marker: &str) {
    let version = env::var("CARGO_PKG_VERSION")
        .expect("Cargo sets CARGO_PKG_VERSION for this package build script");
    let suffix = format!(".{marker}");
    if !version.ends_with(&suffix) {
        panic!("RDT build marker {marker} does not match package version {version}. Run scripts/stamp-platform-build.cjs for the target platform before building.");
    }
}

fn source_build_id(target_os: &str) -> String {
    let manifest_dir = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"),
    );
    let project_root = manifest_dir
        .parent()
        .expect("Tauri manifest directory is inside rdt_connect");
    let marker_name = match target_os {
        "windows" => "build-id-sequence-windows.txt",
        "linux" => "build-id-sequence-linux.txt",
        _ => "build-id-sequence.txt",
    };
    let marker_file = project_root.join("scripts").join(marker_name);
    println!("cargo:rerun-if-changed={}", marker_file.display());

    let stored = fs::read_to_string(&marker_file).unwrap_or_else(|error| {
        panic!(
            "Could not read active RDT Connect build marker {}: {error}",
            marker_file.display()
        )
    });
    let marker = stored.trim();
    let target_arch = env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_default();
    if !is_valid_build_id_for_target(marker, target_os, &target_arch) {
        panic!("Invalid RDT Connect build marker '{marker}' for target '{target_os}'. Run the tested platform-version stamping workflow.");
    }
    marker.to_string()
}

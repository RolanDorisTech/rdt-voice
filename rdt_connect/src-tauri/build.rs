use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=RDT_BUILD_ID");
    println!("cargo:rerun-if-env-changed=CARGO_CFG_TARGET_OS");
    println!("cargo:rerun-if-env-changed=CARGO_PKG_VERSION");

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let build_id = match env::var("RDT_BUILD_ID") {
        Ok(value) if is_valid_build_id_for_target(value.trim(), &target_os) => value.trim().to_string(),
        Ok(value) if !value.trim().is_empty() && value.trim() != "dev-unmarked" => {
            panic!("Invalid RDT_BUILD_ID '{value}' for target '{target_os}'. Expected a marker in that platform's reserved namespace.");
        }
        _ => source_build_id(&target_os),
    };
    verify_package_version_marker(&build_id);
    println!("cargo:rustc-env=RDT_BUILD_ID={build_id}");
    tauri_build::build();
}

fn is_valid_build_id_for_target(value: &str, target_os: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 4
        || !matches!(bytes[0], b'0'..=b'5')
        || !bytes[1].is_ascii_uppercase()
        || !bytes[2].is_ascii_digit()
        || !bytes[3].is_ascii_digit()
    {
        return false;
    }
    match target_os {
        "macos" => matches!(bytes[0], b'0' | b'1'),
        "windows" => matches!(bytes[0], b'2' | b'3'),
        "linux" => matches!(bytes[0], b'4' | b'5'),
        _ => true,
    }
}

fn verify_package_version_marker(marker: &str) {
    let version = env::var("CARGO_PKG_VERSION")
        .expect("Cargo sets CARGO_PKG_VERSION for this package build script");
    let counter = marker[2..].parse::<u32>().expect("validated numeric build counter");
    let suffix = format!(".{}.{}", &marker[..2], counter);
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
    if !is_valid_build_id_for_target(marker, target_os) {
        panic!("Invalid RDT Connect build marker '{marker}' for target '{target_os}'. Run the tested platform-version stamping workflow.");
    }
    marker.to_string()
}

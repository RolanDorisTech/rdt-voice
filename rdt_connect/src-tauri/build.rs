use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=RDT_BUILD_ID");

    let build_id = match env::var("RDT_BUILD_ID") {
        Ok(value) if is_valid_build_id(value.trim()) => value.trim().to_string(),
        Ok(value) if !value.trim().is_empty() && value.trim() != "dev-unmarked" => {
            panic!(
                "Invalid RDT_BUILD_ID '{value}'. Expected four characters: platform digit, uppercase letter, two digits (for example, 1A10)."
            );
        }
        _ => source_build_id(),
    };
    println!("cargo:rustc-env=RDT_BUILD_ID={build_id}");
    tauri_build::build();
}

fn is_valid_build_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 4
        && matches!(bytes[0], b'0'..=b'5')
        && bytes[1].is_ascii_uppercase()
        && bytes[2].is_ascii_digit()
        && bytes[3].is_ascii_digit()
}

fn source_build_id() -> String {
    let manifest_dir = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by Cargo"),
    );
    let project_root = manifest_dir
        .parent()
        .expect("Tauri manifest directory is inside rdt_connect");
    let marker_file = project_root.join("scripts").join("build-id-sequence.txt");
    println!("cargo:rerun-if-changed={}", marker_file.display());

    let stored = fs::read_to_string(&marker_file).unwrap_or_else(|error| {
        panic!(
            "Could not read active RDT Connect build marker {}: {error}",
            marker_file.display()
        )
    });
    let marker = stored.trim();
    if !is_valid_build_id(marker) {
        panic!(
            "Invalid active RDT Connect build marker '{marker}'. Expected four characters: platform digit, uppercase letter, two digits (for example, 1A10)."
        );
    }
    marker.to_string()
}

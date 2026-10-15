fn main() {
    println!("cargo:rerun-if-env-changed=RDT_BUILD_ID");
    let build_id = std::env::var("RDT_BUILD_ID").unwrap_or_else(|_| "dev-unmarked".to_string());
    println!("cargo:rustc-env=RDT_BUILD_ID={build_id}");
    tauri_build::build()
}

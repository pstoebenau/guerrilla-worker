fn main() {
    println!(
        "cargo:rustc-env=DESKTOP_TARGET={}",
        std::env::var("TARGET").expect("Cargo build target")
    );
    tauri_build::build()
}

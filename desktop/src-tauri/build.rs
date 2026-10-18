fn main() {
    println!(
        "cargo:rustc-env=DESKTOP_TARGET={}",
        std::env::var("TARGET").expect("Cargo build target")
    );
    let mut attributes = tauri_build::Attributes::new();
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows")
        && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc")
    {
        // Tauri's .res is linked only to bin targets. Let the MSVC linker
        // embed the same Common Controls dependency into every executable,
        // including the library's unit-test harness, without duplicate resources.
        attributes = attributes
            .windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
        println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
        println!(
            "cargo:rustc-link-arg=/MANIFESTDEPENDENCY:type='win32' name='Microsoft.Windows.Common-Controls' version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'"
        );
    }
    tauri_build::try_build(attributes).expect("Tauri build failed")
}

mod enrollment;
mod profile;
mod updates;

use profile::Profile;
use serde::Serialize;
use serde_json::Value;
#[cfg(test)]
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};
use tauri::{
    Emitter, Manager,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    sync::Mutex,
};

struct DesktopState {
    directory: PathBuf,
    resources: PathBuf,
    worker: PathBuf,
    gate: Mutex<()>,
    quitting: AtomicBool,
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopInfo {
    profile: Profile,
    profile_directory: String,
    enrolled: bool,
    runtime_available: bool,
    gpu_runtime: guerrilla_runtime::SetupStatus,
    version: &'static str,
    platform: &'static str,
}

fn read_profile(state: &DesktopState) -> Result<Profile, String> {
    let path = state.directory.join("config.json");
    let mut p = profile::load_profile(&path, &state.directory)?;
    let runtime = state.resources.join("runtime");
    p.node_path = runtime
        .join(if cfg!(windows) {
            "node/node.exe"
        } else {
            "node/bin/node"
        })
        .to_string_lossy()
        .into_owned();
    p.ffmpeg_path = runtime
        .join(if cfg!(windows) {
            "ffmpeg/ffmpeg.exe"
        } else {
            "ffmpeg/ffmpeg"
        })
        .to_string_lossy()
        .into_owned();
    p.converter_path = runtime
        .join("converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs")
        .to_string_lossy()
        .into_owned();
    Ok(p)
}

fn info(state: &DesktopState) -> Result<DesktopInfo, String> {
    let profile = read_profile(state)?;
    let runtime_available = state.worker.is_file()
        && Path::new(&profile.ffmpeg_path).is_file()
        && Path::new(&profile.node_path).is_file()
        && Path::new(&profile.converter_path).is_file();
    Ok(DesktopInfo {
        profile,
        profile_directory: state.directory.to_string_lossy().into_owned(),
        enrolled: state.directory.join("credentials.json").exists(),
        runtime_available,
        gpu_runtime: guerrilla_runtime::status(&state.resources.join("runtime"), &state.directory),
        version: env!("CARGO_PKG_VERSION"),
        platform: std::env::consts::OS,
    })
}

fn owned_command(
    state: &DesktopState,
    action: &str,
    enrollment: bool,
) -> Result<tokio::process::Command, String> {
    if !state.worker.is_file() {
        return Err("The bundled worker executable is missing. Reinstall Guerrilla Worker or prepare desktop resources for development.".into());
    }
    let mut command = tokio::process::Command::new(&state.worker);
    command.env_clear();
    // Only the user's platform/GPU session reaches native engine processes.
    // Enrollment and execution always use this app's saved profile, not shell
    // credentials or an inherited Python environment.
    for key in [
        "PATH",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "VK_ICD_FILENAMES",
        "NVIDIA_DRIVER_CAPABILITIES",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_ID",
        "XDG_SESSION_TYPE",
        "XDG_DATA_HOME",
        "GPU_LOCK_PATH",
        "WORKER_AGENT_LOCK_DIRECTORY",
        "SPLAT_TRANSFORM_GPU",
        "SPLAT_TRANSFORM_GPU_BACKEND",
    ] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    command
        .args(["--config"])
        .arg(state.directory.join("config.json"))
        .arg(action)
        .env("WORKER_RUNTIME_ROOT", state.resources.join("runtime"))
        .env(
            "WORKER_PIPELINE_BIN",
            state.worker.with_file_name(
                state
                    .worker
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .replacen("guerrilla-worker", "guerrilla-pipeline", 1),
            ),
        )
        .stdin(if enrollment {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000); // CREATE_NO_WINDOW for management calls
    if enrollment {
        command.arg("--token-stdin");
    }
    Ok(command)
}

async fn cli(state: &DesktopState, action: &str, token: Option<String>) -> Result<Value, String> {
    let mut command = owned_command(state, action, token.is_some())?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("Cannot run bundled worker: {e}"))?;
    if let Some(secret) = token {
        let mut input = child.stdin.take().ok_or("Cannot open enrollment input")?;
        input
            .write_all(secret.as_bytes())
            .await
            .map_err(|_| "Cannot send enrollment token")?;
        input
            .shutdown()
            .await
            .map_err(|_| "Cannot close enrollment input")?;
    }
    let output = tokio::time::timeout(
        Duration::from_secs(if matches!(action, "preflight" | "enroll" | "start") {
            1900
        } else if action == "stop" {
            120
        } else {
            60
        }),
        child.wait_with_output(),
    )
    .await
    .map_err(|_| {
        "Worker command timed out. Check worker status and logs before retrying.".to_owned()
    })?
    .map_err(|e| format!("Worker command failed: {e}"))?;
    if output.stdout.len() > 2_000_000 || output.stderr.len() > 2_000_000 {
        return Err("Worker command returned too much output".into());
    }
    if !output.status.success() {
        // Only allowlisted error codes are displayed; process output can contain tokens.
        if action == "enroll" {
            return Err(enrollment::failure(&output.stderr, &output.status));
        }
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(if stderr.trim().is_empty() {
            format!("Worker {action} failed ({})", output.status)
        } else {
            stderr.chars().take(2000).collect()
        });
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|_| format!("Worker {action} returned an invalid response"))
}

#[tauri::command]
async fn desktop_info(state: tauri::State<'_, DesktopState>) -> Result<DesktopInfo, String> {
    info(&state)
}
#[tauri::command]
async fn worker_status(state: tauri::State<'_, DesktopState>) -> Result<Value, String> {
    cli(&state, "status", None).await
}
#[tauri::command]
async fn worker_preflight(state: tauri::State<'_, DesktopState>) -> Result<Value, String> {
    let _guard = state.gate.lock().await;
    cli(&state, "preflight", None).await
}

#[tauri::command]
async fn install_gpu_runtime(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<DesktopInfo, String> {
    let _guard = state.gate.lock().await;
    if state.quitting.load(Ordering::SeqCst) {
        return Err("Worker is quitting.".into());
    }
    let mut command = owned_command(&state, "setup-runtime", false)?;
    let mut child = command
        .spawn()
        .map_err(|error| format!("Cannot start GPU setup: {error}"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or("Missing GPU setup error output")?;
    let errors = tauri::async_runtime::spawn(async move {
        let mut reader = stderr;
        let mut tail = Vec::new();
        let mut block = [0_u8; 4096];
        while let Ok(size) = reader.read(&mut block).await {
            if size == 0 {
                break;
            }
            tail.extend_from_slice(&block[..size]);
            if tail.len() > 8192 {
                tail.drain(..tail.len() - 8192);
            }
        }
        String::from_utf8_lossy(&tail).into_owned()
    });
    let mut lines =
        BufReader::new(child.stdout.take().ok_or("Missing GPU setup progress")?).lines();
    let mut completed = false;
    loop {
        let line = tokio::select! {
            line = lines.next_line() => line.map_err(|error| error.to_string())?,
            _ = tokio::time::sleep(Duration::from_millis(250)) => {
                if state.quitting.load(Ordering::SeqCst) {
                    let _ = child.kill().await;
                    return Err("GPU setup canceled; verified downloads remain cached.".into());
                }
                continue;
            }
        };
        let Some(line) = line else { break };
        if line.len() > 16_384 {
            return Err("Invalid GPU setup response".into());
        }
        let event: Value = serde_json::from_str(&line).map_err(|_| "Invalid GPU setup response")?;
        match event["type"].as_str() {
            Some("runtime-setup-progress") => {
                let _ = app.emit("runtime-setup-progress", event);
            }
            Some("runtime-setup-complete") => completed = event["installed"] == true,
            _ => return Err("Unexpected GPU setup response".into()),
        }
    }
    let success = child
        .wait()
        .await
        .map_err(|error| error.to_string())?
        .success();
    let error = errors.await.map_err(|error| error.to_string())?;
    if !success || !completed {
        return Err(if error.trim().is_empty() {
            "GPU setup did not finish. Retry from Setup.".into()
        } else {
            error.chars().take(3000).collect()
        });
    }
    info(&state)
}

#[tauri::command]
async fn save_profile(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
    mut profile: Profile,
) -> Result<DesktopInfo, String> {
    let _guard = state.gate.lock().await;
    let current = read_profile(&state)?;
    profile.control_url = profile::normalize_server(&profile.control_url)?;
    if let Some(origin) = profile::bound_server(&state.directory)?
        && origin != profile.control_url
    {
        return Err("Stop and disconnect this worker before changing servers. Enrollment belongs to its original server.".into());
    }
    if state.worker.is_file() {
        let status = cli(&state, "status", None).await?;
        if status.get("running").and_then(Value::as_bool) != Some(false) {
            return Err("Stop the worker before changing execution settings.".into());
        }
    }
    // Runtime locations are supplied by the bundle; only preferences are editable.
    profile.ffmpeg_path = current.ffmpeg_path;
    profile.node_path = current.node_path;
    profile.converter_path = current.converter_path;
    profile.extra = current.extra;
    profile::validate_paths(&profile)?;
    if std::env::var_os("GUERRILLA_WORKER_DATA_DIR").is_some() {
        if profile.start_at_login {
            return Err("Login startup is unavailable with an isolated test data directory. Leave startup disabled, or use the normal desktop profile.".into());
        }
        profile::atomic_save(&state.directory.join("config.json"), &profile)?;
        return info(&state);
    }
    let was_enabled = app.autolaunch().is_enabled().map_err(|e| e.to_string())?;
    if profile.start_at_login {
        app.autolaunch().enable()
    } else {
        app.autolaunch().disable()
    }
    .map_err(|e| format!("Cannot change login startup: {e}"))?;
    if let Err(e) = profile::atomic_save(&state.directory.join("config.json"), &profile) {
        let _ = if was_enabled {
            app.autolaunch().enable()
        } else {
            app.autolaunch().disable()
        };
        return Err(e);
    }
    info(&state)
}

#[tauri::command]
async fn enroll_worker(
    state: tauri::State<'_, DesktopState>,
    token: String,
    control_url: String,
) -> Result<Value, String> {
    enroll(&state, token, control_url).await
}

async fn enroll(state: &DesktopState, token: String, control_url: String) -> Result<Value, String> {
    let _guard = state.gate.lock().await;
    if state.quitting.load(Ordering::SeqCst) {
        return Err("Worker is quitting.".into());
    }
    if token.trim().is_empty() || token.len() > 4096 {
        return Err("Paste an enrollment token from the server's Workers page.".into());
    }
    if profile::bound_server(&state.directory)?.is_some() {
        return Err("Worker already enrolled; disconnect before enrolling again.".into());
    }
    let mut p = read_profile(state)?;
    p.control_url = profile::normalize_server(&control_url)?;
    // Only enrollment settings are saved here. Engine paths, scratch storage,
    // and login startup are validated separately when saving worker settings.
    profile::atomic_save(&state.directory.join("config.json"), &p)?;
    cli(state, "enroll", Some(token.trim().to_owned())).await
}

async fn action(state: &DesktopState, action: &str) -> Result<Value, String> {
    if !matches!(action, "start" | "stop" | "drain" | "disconnect") {
        return Err("Unknown worker action".into());
    }
    let _guard = state.gate.lock().await;
    if state.quitting.load(Ordering::SeqCst) {
        return Err("Worker is quitting.".into());
    }
    cli(state, action, None).await?;
    cli(state, "status", None).await
}
#[tauri::command]
async fn worker_action(
    state: tauri::State<'_, DesktopState>,
    action: String,
) -> Result<Value, String> {
    self::action(&state, &action).await
}
#[tauri::command]
async fn worker_logs(state: tauri::State<'_, DesktopState>) -> Result<Value, String> {
    cli(&state, "logs", None).await
}

#[tauri::command]
async fn browse_path(app: tauri::AppHandle, field: String) -> Result<Option<String>, String> {
    let directory = match field.as_str() {
        "scratchDirectory" => true,
        _ => return Err("Unknown path setting".into()),
    };
    tauri::async_runtime::spawn_blocking(move || {
        let picker = app.dialog().file().set_title(if directory {
            "Select folder"
        } else {
            "Select engine executable"
        });
        let selected = if directory {
            picker.blocking_pick_folder()
        } else {
            picker.blocking_pick_file()
        };
        selected
            .map(|p| {
                p.into_path()
                    .map(|p| p.to_string_lossy().into_owned())
                    .map_err(|_| "Select a local filesystem path".into())
            })
            .transpose()
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn open_server(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<(), String> {
    let url = profile::normalize_server(&read_profile(&state)?.control_url)?;
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}
#[tauri::command]
async fn check_for_updates() -> Result<Option<updates::AvailableUpdate>, String> {
    updates::check().await
}
#[tauri::command]
async fn open_release(app: tauri::AppHandle, url: String) -> Result<(), String> {
    if !updates::valid_release_url(&url) {
        return Err("Invalid worker release URL".into());
    }
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}
#[tauri::command]
async fn open_data_directory(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<(), String> {
    app.opener()
        .open_path(state.directory.to_string_lossy(), None::<&str>)
        .map_err(|e| e.to_string())
}
#[tauri::command]
async fn quit_interface(app: tauri::AppHandle) -> Result<(), String> {
    stop_and_quit(&app).await
}

async fn stop_for_quit(state: &DesktopState) -> Result<(), String> {
    let status = cli(state, "stop", None).await?;
    if status.get("running").and_then(Value::as_bool) != Some(false) {
        return Err(
            "The worker has not stopped yet. Check its status and try quitting again.".into(),
        );
    }
    Ok(())
}

async fn stop_and_quit(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    if state.quitting.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    // Finish pending management work, then stop it. Reject queued starts and
    // enrollments until the app has exited or shutdown fails.
    let _guard = state.gate.lock().await;
    if let Err(error) = stop_for_quit(&state).await {
        state.quitting.store(false, Ordering::SeqCst);
        return Err(error);
    }
    app.exit(0);
    Ok(())
}

fn request_quit(app: &tauri::AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(message) = stop_and_quit(&app).await {
            let _ = app.emit(
                "worker-error",
                format!("Could not stop jobs and quit: {message}"),
            );
            show_window(&app);
        }
    });
}

fn show_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn run() {
    let mut context = tauri::generate_context!();
    if std::env::var_os("GUERRILLA_WORKER_DATA_DIR").is_some() {
        use std::hash::{Hash, Hasher};
        let directory =
            profile::default_directory().expect("Invalid isolated worker data directory");
        let mut identity = std::collections::hash_map::DefaultHasher::new();
        directory.hash(&mut identity);
        // Single-instance IPC must not reopen a normal enrolled app while QA
        // launches with a separate profile. Repeated launches of the same test
        // profile still attach to that profile's interface.
        context.config_mut().identifier =
            format!("dad.guerrilla.worker.qa{:016x}", identity.finish());
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _| {
            if !args.iter().any(|a| a == "--background") {
                show_window(app);
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--background"]),
        ))
        .invoke_handler(tauri::generate_handler![
            desktop_info,
            worker_status,
            worker_preflight,
            install_gpu_runtime,
            save_profile,
            enroll_worker,
            worker_action,
            worker_logs,
            browse_path,
            open_server,
            check_for_updates,
            open_release,
            open_data_directory,
            quit_interface
        ])
        .setup(|app| {
            let directory = profile::default_directory().map_err(std::io::Error::other)?;
            profile::secure_directory(&directory).map_err(std::io::Error::other)?;
            let resources = if cfg!(debug_assertions) {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources")
            } else {
                app.path().resource_dir()?.join("resources")
            };
            let executable = std::env::current_exe()?;
            let mut worker = executable
                .parent()
                .ok_or("Executable directory unavailable")?
                .join(if cfg!(windows) {
                    "guerrilla-worker.exe"
                } else {
                    "guerrilla-worker"
                });
            if cfg!(debug_assertions) {
                let name = format!(
                    "guerrilla-worker-{}{}",
                    env!("DESKTOP_TARGET"),
                    if cfg!(windows) { ".exe" } else { "" }
                );
                let candidate = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("binaries")
                    .join(name);
                if candidate.exists() {
                    worker = candidate;
                }
            }
            let state = DesktopState {
                directory,
                resources,
                worker,
                gate: Mutex::new(()),
                quitting: AtomicBool::new(false),
            };
            let p = read_profile(&state).map_err(std::io::Error::other)?;
            profile::atomic_save(&state.directory.join("config.json"), &p)
                .map_err(std::io::Error::other)?;
            app.manage(state);
            let overview =
                MenuItem::with_id(app, "overview", "Open Guerrilla Worker", true, None::<&str>)?;
            let start = MenuItem::with_id(app, "start", "Start worker", true, None::<&str>)?;
            let stop = MenuItem::with_id(app, "stop", "Stop worker", true, None::<&str>)?;
            let drain = MenuItem::with_id(
                app,
                "drain",
                "Finish current job, then pause",
                true,
                None::<&str>,
            )?;
            let quit =
                MenuItem::with_id(app, "quit", "Quit and stop all jobs", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu =
                Menu::with_items(app, &[&overview, &separator, &start, &stop, &drain, &quit])?;
            let icon = app
                .default_window_icon()
                .cloned()
                .ok_or("Application icon unavailable")?;
            let tray = TrayIconBuilder::with_id("worker")
                .icon(icon)
                .tooltip("Guerrilla Worker")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "overview" => show_window(app),
                    "quit" => request_quit(app),
                    "start" | "stop" | "drain" => {
                        let app = app.clone();
                        let command = event.id.as_ref().to_owned();
                        tauri::async_runtime::spawn(async move {
                            let result = action(&app.state::<DesktopState>(), &command).await;
                            match result {
                                Ok(status) => {
                                    let _ = app.emit("worker-status", status);
                                }
                                Err(message) => {
                                    let _ = app.emit("worker-error", message);
                                    show_window(&app);
                                }
                            }
                        });
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        show_window(tray.app_handle());
                    }
                })
                .build(app);
            // Linux desktop environments without a tray can still manage the
            // worker. Without a tray, closing stops jobs and quits the app.
            let has_tray = tray.is_ok();
            let background = std::env::args().any(|a| a == "--background");
            if !background || !has_tray {
                show_window(app.handle());
            }
            if background && p.start_at_login {
                let app = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(message) = action(&app.state::<DesktopState>(), "start").await {
                        let _ = app.emit("worker-error", message);
                        show_window(&app);
                    }
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                if window.app_handle().tray_by_id("worker").is_some() {
                    let _ = window.hide();
                    let _ = window.emit("window-hidden", ());
                } else {
                    request_quit(window.app_handle());
                }
            }
        })
        .build(context)
        .expect("Guerrilla Worker desktop could not start")
        .run(|app, event| {
            // OS quit requests (e.g. Cmd+Q) follow the same shutdown path.
            // app.exit(0) from stop_and_quit supplies a code and may proceed.
            if let tauri::RunEvent::ExitRequested {
                api, code: None, ..
            } = event
            {
                api.prevent_exit();
                request_quit(app);
            }
        });
}

#[cfg(test)]
mod integration_tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn quit_waits_for_stop_and_rejects_unconfirmed_shutdown() {
        use std::os::unix::fs::PermissionsExt;
        let directory = std::env::temp_dir().join(format!(
            "guerrilla-quit-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        profile::secure_directory(&directory).unwrap();
        let worker = directory.join("worker");
        let state = DesktopState {
            directory: directory.clone(),
            resources: directory.clone(),
            worker: worker.clone(),
            gate: Mutex::new(()),
            quitting: AtomicBool::new(false),
        };
        tauri::async_runtime::block_on(async {
            for (output, exit_code, succeeds) in [
                (r#"{"running":false,"state":"stopped"}"#, 0, true),
                (r#"{"running":false,"state":"unenrolled"}"#, 0, true),
                (r#"{"running":true,"state":"busy"}"#, 0, false),
                (r#"{"state":"stopped"}"#, 0, false),
                ("invalid response", 0, false),
                (r#"{"running":false}"#, 1, false),
            ] {
                // The fixture accepts only the stop command and publishes its
                // response after completing its work, just like the real CLI.
                std::fs::write(&worker, format!(
                    "#!/bin/sh\n[ \"$3\" = stop ] || exit 2\nprintf '%s' '{output}'\nexit {exit_code}\n"
                )).unwrap();
                std::fs::set_permissions(&worker, std::fs::Permissions::from_mode(0o700)).unwrap();
                assert_eq!(stop_for_quit(&state).await.is_ok(), succeeds, "{output}");
            }
            state.quitting.store(true, Ordering::SeqCst);
            assert_eq!(
                action(&state, "start").await.unwrap_err(),
                "Worker is quitting."
            );
        });
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    #[ignore = "requires a built real worker; set GUERRILLA_DESKTOP_TEST_WORKER and run --ignored"]
    fn native_command_transport_uses_real_worker_and_redacts_enrollment_failures() {
        let worker = PathBuf::from(
            std::env::var_os("GUERRILLA_DESKTOP_TEST_WORKER")
                .expect("Set GUERRILLA_DESKTOP_TEST_WORKER to the built worker executable"),
        );
        assert!(worker.is_absolute() && worker.is_file());
        let directory = std::env::temp_dir().join(format!(
            "guerrilla-desktop-transport-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        profile::secure_directory(&directory).unwrap();
        let state = DesktopState {
            resources: directory.join("absent-runtime"),
            directory: directory.clone(),
            worker,
            gate: Mutex::new(()),
            quitting: AtomicBool::new(false),
        };
        let mut profile = read_profile(&state).unwrap();
        profile.control_url = "http://127.0.0.1:9".into();
        profile::atomic_save(&directory.join("config.json"), &profile).unwrap();
        tauri::async_runtime::block_on(async {
            let status = cli(&state, "status", None).await.unwrap();
            assert_eq!(status["state"], "unenrolled");
            assert_eq!(status["connected"], false);
            assert_eq!(status["running"], false);
            let logs = cli(&state, "logs", None).await.unwrap();
            assert!(logs["lines"].as_array().unwrap().is_empty());
            assert_eq!(
                action(&state, "drain").await.unwrap()["state"],
                "unenrolled"
            );
            assert_eq!(
                action(&state, "disconnect").await.unwrap()["state"],
                "unenrolled"
            );
            assert!(action(&state, "start; echo injected").await.is_err());
            let failure = cli(&state, "enroll", Some("private-enrollment-token".into()))
                .await
                .unwrap_err();
            assert!(failure.contains("Enrollment failed"));
            assert!(!failure.contains("private-enrollment-token"));
            assert!(!directory.join("credentials.json").exists());
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let server = std::thread::spawn(move || {
                use std::io::{Read, Write};
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(10)))
                    .unwrap();
                let mut bytes = vec![];
                let mut buffer = [0u8; 4096];
                let (header_end, size) = loop {
                    let count = socket.read(&mut buffer).unwrap();
                    assert!(count > 0);
                    bytes.extend_from_slice(&buffer[..count]);
                    if let Some(index) = bytes.windows(4).position(|block| block == b"\r\n\r\n") {
                        let header_end = index + 4;
                        let header = String::from_utf8_lossy(&bytes[..header_end]);
                        assert!(header.starts_with("POST /api/worker/enroll "));
                        let size: usize = header
                            .lines()
                            .find_map(|line| {
                                line.to_ascii_lowercase()
                                    .strip_prefix("content-length: ")
                                    .map(|size| size.parse().unwrap())
                            })
                            .unwrap();
                        break (header_end, size);
                    }
                };
                while bytes.len() < header_end + size {
                    let count = socket.read(&mut buffer).unwrap();
                    assert!(count > 0);
                    bytes.extend_from_slice(&buffer[..count]);
                }
                let request: Value =
                    serde_json::from_slice(&bytes[header_end..header_end + size]).unwrap();
                let body = r#"{"workerId":"enrolled-worker","credential":"fixture-credential"}"#;
                write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
                request
            });
            // The desktop enrolls before installing the runtime,
            // saves only the server setting, and never starts a worker process.
            let enrolled = enroll(&state, "private-enrollment-token".into(), origin)
                .await
                .unwrap();
            assert_eq!(enrolled["workerId"], "enrolled-worker");
            assert_eq!(enrolled["state"], "stopped");
            assert_eq!(enrolled["running"], false);
            let request = server.join().unwrap();
            assert_eq!(request["registration"]["healthy"], false);
            assert_eq!(request["registration"]["engines"], json!({}));
            assert_eq!(request["registration"]["gpus"], json!([]));
            assert!(directory.join("credentials.json").exists());
            assert!(
                enroll(
                    &state,
                    "unused-token".into(),
                    "https://other-server.example".into()
                )
                .await
                .unwrap_err()
                .contains("already enrolled")
            );
        });
        std::fs::remove_dir_all(directory).unwrap();
    }
}

use crate::{DesktopState, cli};
use serde::Serialize;
use serde_json::Value;
use std::{
    path::PathBuf,
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tauri::{Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

pub fn initialize_tls() {
    // The updater uses reqwest's rustls-no-provider feature. Install the same
    // supported provider as the bundled worker instead of depending on request order.
    let _ = rustls::crypto::ring::default_provider().install_default();
}

const RELEASES_URL: &str = "https://github.com/pstoebenau/guerrilla-worker/releases/tag/";

#[derive(Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    revision: u64,
    status: Status,
    version: Option<String>,
    release_url: Option<String>,
    downloaded: u64,
    total: Option<u64>,
    install_queued: bool,
    worker_busy: bool,
    error: Option<String>,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
enum Status {
    #[default]
    Idle,
    Checking,
    Downloading,
    Ready,
    WaitingForIdle,
    Installing,
    Error,
}

#[derive(Default)]
struct Inner {
    snapshot: Snapshot,
    update: Option<Update>,
    package: Option<PathBuf>,
}

#[derive(Default)]
pub struct State {
    inner: Mutex<Inner>,
    cancel: AtomicBool,
}

fn change<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    mutate: impl FnOnce(&mut Inner),
) -> Snapshot {
    let state = app.state::<State>();
    let mut inner = state.inner.lock().unwrap();
    mutate(&mut inner);
    inner.snapshot.revision += 1;
    let snapshot = inner.snapshot.clone();
    let _ = app.emit("updater-state", &snapshot);
    snapshot
}

pub fn observe_worker_status(app: &tauri::AppHandle, status: &Value) {
    let state = app.state::<State>();
    let mut inner = state.inner.lock().unwrap();
    let worker_busy = busy(status);
    if inner.snapshot.worker_busy != worker_busy {
        inner.snapshot.worker_busy = worker_busy;
        inner.snapshot.revision += 1;
        let _ = app.emit("updater-state", &inner.snapshot);
    }
}

pub fn snapshot<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Snapshot {
    app.state::<State>().inner.lock().unwrap().snapshot.clone()
}

fn error(app: &tauri::AppHandle, message: String) {
    change(app, |inner| {
        inner.snapshot.status = Status::Error;
        inner.snapshot.install_queued = false;
        inner.snapshot.error = Some(message);
    });
}

pub fn valid_release_url(url: &str) -> bool {
    url.strip_prefix(RELEASES_URL).is_some_and(|tag| {
        let tag = ["windows-", "linux-", "macos-"]
            .iter()
            .find_map(|prefix| tag.strip_prefix(prefix))
            .unwrap_or(tag);
        semver::Version::parse(tag.strip_prefix('v').unwrap_or(tag))
            .is_ok_and(|version| version.pre.is_empty())
    })
}

// Unknown or incomplete status is never permission to replace a running worker.
fn running(status: &Value) -> Result<bool, String> {
    status["running"].as_bool().ok_or_else(|| {
        "Cannot confirm worker activity. Check worker status before updating.".into()
    })
}
fn busy(status: &Value) -> bool {
    running(status) != Ok(false)
        && (status["scanId"].as_str().is_some()
            || !matches!(
                status["state"].as_str(),
                Some("idle" | "starting" | "connecting")
            ))
}

pub async fn check(app: tauri::AppHandle) -> Result<Snapshot, String> {
    {
        let state = app.state::<State>();
        let mut inner = state.inner.lock().unwrap();
        if matches!(
            inner.snapshot.status,
            Status::Checking
                | Status::Downloading
                | Status::Ready
                | Status::WaitingForIdle
                | Status::Installing
        ) {
            return Ok(inner.snapshot.clone());
        }
        // Failed installation keeps the verified download available for retry.
        if inner.package.is_some() && inner.update.is_some() {
            inner.snapshot.status = Status::Ready;
            inner.snapshot.error = None;
            inner.snapshot.revision += 1;
            let _ = app.emit("updater-state", &inner.snapshot);
            return Ok(inner.snapshot.clone());
        }
        inner.snapshot.status = Status::Checking;
        inner.snapshot.error = None;
        inner.snapshot.revision += 1;
        let _ = app.emit("updater-state", &inner.snapshot);
    }
    let initial = snapshot(&app);
    tauri::async_runtime::spawn(async move {
        if let Err(message) = check_and_download(&app).await {
            error(&app, message);
        }
    });
    Ok(initial)
}

async fn check_and_download(app: &tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    if !matches!(
        tauri::utils::platform::bundle_type(),
        Some(
            tauri::utils::config::BundleType::AppImage
                | tauri::utils::config::BundleType::Deb
                | tauri::utils::config::BundleType::Rpm
        )
    ) {
        return Err("In-app updates require the installed AppImage, DEB or RPM package. Download the matching package from GitHub Releases.".into());
    }
    let update = app
        .updater_builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| format!("Updates are unavailable: {e}"))?
        .check()
        .await
        .map_err(|e| format!("Cannot check for updates: {e}"))?;
    let package = app
        .state::<DesktopState>()
        .directory
        .join("updater/verified-update.bin");
    let Some(mut update) = update else {
        let old_cache = package;
        let _ = tauri::async_runtime::spawn_blocking(move || std::fs::remove_file(old_cache)).await;
        change(app, |inner| {
            inner.snapshot = Snapshot {
                revision: inner.snapshot.revision,
                ..Default::default()
            }
        });
        return Ok(());
    };
    // Large engine bundles need a download timeout independent of the small manifest request.
    update.timeout = Some(Duration::from_secs(6 * 60 * 60));
    change(app, |inner| {
        inner.snapshot.status = Status::Downloading;
        inner.snapshot.version = Some(update.version.clone());
        inner.snapshot.release_url = update.raw_json["releaseUrl"]
            .as_str()
            .filter(|url| valid_release_url(url))
            .map(str::to_owned);
        inner.snapshot.downloaded = 0;
        inner.snapshot.total = None;
    });
    // Reopening the interface must not repeat a large completed download. Verify
    // cached bytes against the freshly checked release, rather than trusting disk metadata.
    let cached_update = update.clone();
    let cached_path = package.clone();
    let cached_size = tauri::async_runtime::spawn_blocking(move || {
        verified_cache_size(&cached_update, &cached_path)
    })
    .await
    .map_err(|e| e.to_string())?;
    if let Some(size) = cached_size {
        change(app, |inner| {
            inner.update = Some(update);
            inner.package = Some(package);
            inner.snapshot.status = Status::Ready;
            inner.snapshot.downloaded = size;
            inner.snapshot.total = Some(size);
        });
        return Ok(());
    }
    // The supported plugin verifies the updater signature before returning any bytes.
    let mut downloaded = 0_u64;
    let mut last_event = std::time::Instant::now() - Duration::from_secs(1);
    let bytes = update
        .download(
            |chunk, total| {
                downloaded += chunk as u64;
                if last_event.elapsed() >= Duration::from_millis(250) {
                    change(app, |inner| {
                        inner.snapshot.downloaded = downloaded;
                        inner.snapshot.total = total;
                    });
                    last_event = std::time::Instant::now();
                }
            },
            || {},
        )
        .await
        .map_err(|e| format!("Update download or signature verification failed: {e}"))?;
    change(app, |inner| inner.snapshot.downloaded = bytes.len() as u64);
    let directory = package
        .parent()
        .ok_or("Update cache directory unavailable")?
        .to_path_buf();
    let output = package.clone();
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&directory)
            .map_err(|e| format!("Cannot create update cache: {e}"))?;
        std::fs::write(output, bytes).map_err(|e| format!("Cannot save downloaded update: {e}"))
    })
    .await
    .map_err(|e| e.to_string())??;
    change(app, |inner| {
        inner.update = Some(update);
        inner.package = Some(package);
        inner.snapshot.status = Status::Ready;
    });
    Ok(())
}

fn verified_cache_size(update: &Update, package: &std::path::Path) -> Option<u64> {
    let bytes = std::fs::read(package).ok()?;
    if update.verify(&bytes).is_err() {
        let _ = std::fs::remove_file(package);
        return None;
    }
    Some(bytes.len() as u64)
}

pub fn schedule(app: tauri::AppHandle, wait_for_idle: bool) -> Result<Snapshot, String> {
    let desktop = app.state::<DesktopState>();
    if desktop.quitting.load(Ordering::SeqCst) {
        return Err("Worker is quitting.".into());
    }
    {
        let state = app.state::<State>();
        let mut inner = state.inner.lock().unwrap();
        if inner.snapshot.install_queued || inner.snapshot.status == Status::Installing {
            return Ok(inner.snapshot.clone());
        }
        if inner.package.is_none() || inner.update.is_none() {
            return Err("Download and verify an update before installing it.".into());
        }
        if desktop.updating.swap(true, Ordering::SeqCst) {
            return Err("An update is already preparing to install.".into());
        }
        state.cancel.store(false, Ordering::SeqCst);
        inner.snapshot.status = Status::WaitingForIdle;
        inner.snapshot.install_queued = true;
        inner.snapshot.error = None;
        inner.snapshot.revision += 1;
        let _ = app.emit("updater-state", &inner.snapshot);
    }
    let initial = snapshot(&app);
    drop(desktop);
    tauri::async_runtime::spawn(async move {
        let desktop = app.state::<DesktopState>();
        // The same gate protects all enrollment, setup, settings and worker lifecycle changes.
        let _guard = desktop.gate.lock().await;
        let result = prepare_and_install(&app, &desktop, wait_for_idle).await;
        desktop.updating.store(false, Ordering::SeqCst);
        if let Err(message) = result {
            error(&app, message);
        }
    });
    Ok(initial)
}

pub fn cancel<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<Snapshot, String> {
    let state = app.state::<State>();
    let mut inner = state.inner.lock().unwrap();
    if inner.snapshot.status == Status::Installing {
        return Err("Update installation has already started.".into());
    }
    if !inner.snapshot.install_queued {
        return Ok(inner.snapshot.clone());
    }
    state.cancel.store(true, Ordering::SeqCst);
    inner.snapshot.install_queued = false;
    // A drain cannot be undone. The task finishes the scan and restores prior activity.
    inner.snapshot.error =
        Some("Update canceled. Finishing the current job before restoring worker activity.".into());
    inner.snapshot.revision += 1;
    let _ = app.emit("updater-state", &inner.snapshot);
    Ok(inner.snapshot.clone())
}

async fn wait_for_drain(state: &DesktopState) -> Result<(), String> {
    let started = std::time::Instant::now();
    let mut seen_draining = false;
    loop {
        if state.quitting.load(Ordering::SeqCst) {
            return Err("Update canceled because the app is quitting.".into());
        }
        let status = cli(state, "status", None).await?;
        if !running(&status)? {
            return Ok(());
        }
        if status["state"] == "draining" {
            seen_draining = true;
        } else if matches!(
            status["state"].as_str(),
            Some("idle" | "starting" | "connecting" | "busy")
        ) && (seen_draining || started.elapsed() >= Duration::from_secs(3))
        {
            return Err("Worker activity changed before it confirmed pausing. No update was installed; check worker status.".into());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

async fn restore(
    state: &DesktopState,
    was_running: bool,
    drain_complete: bool,
) -> Result<(), String> {
    if was_running && !state.quitting.load(Ordering::SeqCst) {
        // Never restart against an unconfirmed daemon exit.
        if running(&cli(state, "status", None).await?)? {
            // A daemon observed after confirmed exit belongs to a later start.
            if drain_complete {
                return Ok(());
            }
            wait_for_drain(state).await?;
        }
        cli(state, "start", None).await?;
    }
    Ok(())
}

fn invalidate_package<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    change(app, |inner| {
        if let Some(package) = inner.package.take() {
            let _ = std::fs::remove_file(package);
        }
        inner.update = None;
        inner.snapshot.downloaded = 0;
        inner.snapshot.total = None;
    });
}

// cancel() and this transition share a lock: once cancellation is acknowledged,
// the installer cannot start. Setting Installing makes subsequent cancel calls fail.
fn begin_install<R: tauri::Runtime>(app: &tauri::AppHandle<R>, desktop: &DesktopState) -> bool {
    let state = app.state::<State>();
    let mut inner = state.inner.lock().unwrap();
    if state.cancel.load(Ordering::SeqCst) || desktop.quitting.load(Ordering::SeqCst) {
        return false;
    }
    inner.snapshot.status = Status::Installing;
    inner.snapshot.worker_busy = false;
    inner.snapshot.revision += 1;
    let _ = app.emit("updater-state", &inner.snapshot);
    true
}

async fn prepare_and_install<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    desktop: &DesktopState,
    wait_for_idle: bool,
) -> Result<(), String> {
    let state = app.state::<State>();
    if state.cancel.load(Ordering::SeqCst) || desktop.quitting.load(Ordering::SeqCst) {
        change(app, |inner| {
            inner.snapshot.status = Status::Ready;
            inner.snapshot.install_queued = false;
            inner.snapshot.error = None;
        });
        return Ok(());
    }
    let status = cli(desktop, "status", None).await?;
    let was_running = running(&status)?;
    // An already draining worker reflects the user's earlier pause request.
    let resume_worker = was_running && status["state"] != "draining";
    let worker_busy = busy(&status);
    change(app, |inner| inner.snapshot.worker_busy = worker_busy);
    if worker_busy && !wait_for_idle {
        return Err("A scan is active. Choose Update when jobs finish.".into());
    }
    let (update, package) = {
        let inner = state.inner.lock().unwrap();
        (
            inner.update.clone().ok_or("Update metadata unavailable")?,
            inner
                .package
                .clone()
                .ok_or("Downloaded update unavailable")?,
        )
    };
    // Check the on-disk package before draining; install also verifies it again.
    let verifier = update.clone();
    let payload = tauri::async_runtime::spawn_blocking(move || {
        let bytes =
            std::fs::read(package).map_err(|e| format!("Cannot read downloaded update: {e}"))?;
        verifier
            .verify(&bytes)
            .map_err(|e| format!("Update signature verification failed: {e}"))?;
        Ok::<_, String>(bytes)
    })
    .await
    .map_err(|e| e.to_string())?;
    let bytes = match payload {
        Ok(bytes) => bytes,
        Err(message) => {
            invalidate_package(app);
            return Err(message);
        }
    };
    // The job may take hours to drain. Keep only the verified file while waiting.
    drop(bytes);
    let mut drain_complete = !was_running;
    let mut drain_confirmed = false;
    let prepared = async {
        if was_running {
            // A failed response can follow a successful write. Retry, then briefly
            // observe daemon acknowledgement; do not wait forever if neither write worked.
            match cli(desktop, "drain", None).await {
                Ok(_) => drain_confirmed = true,
                Err(first) => {
                    match cli(desktop, "drain", None).await {
                        Ok(_) => drain_confirmed = true,
                        Err(second) => {
                            for _ in 0..8 {
                                if desktop.quitting.load(Ordering::SeqCst) { break; }
                                let status = cli(desktop, "status", None).await?;
                                if !running(&status)? || status["state"] == "draining" {
                                    drain_confirmed = true;
                                    break;
                                }
                                tokio::time::sleep(Duration::from_millis(250)).await;
                            }
                            if !drain_confirmed { return Err(format!("Cannot confirm worker drain: {first}; {second}. No update was installed; check worker status before restarting it.")); }
                        }
                    }
                }
            }
            wait_for_drain(desktop).await?;
            drain_complete = true;
        }
        if state.cancel.load(Ordering::SeqCst) || desktop.quitting.load(Ordering::SeqCst) { return Ok(false); }
        if running(&cli(desktop, "status", None).await?)? { return Err("Worker started again; update was not installed.".into()); }
        // Hold the daemon's own profile lock through installation, so an external
        // CLI cannot start a new scan between the idle check and replacing binaries.
        let _worker_lock = lock_for_install(desktop)?;
        // Re-read only once idle; the plugin rechecks both signature and signed version.
        let package = state.inner.lock().unwrap().package.clone().ok_or("Downloaded update unavailable")?;
        let bytes = match tauri::async_runtime::spawn_blocking(move || std::fs::read(package)).await.map_err(|e| e.to_string())? {
            Ok(bytes) => bytes,
            Err(error) => { invalidate_package(app); return Err(format!("Cannot read downloaded update: {error}")); }
        };
        if resume_worker { write_resume(desktop)?; }
        if !begin_install(app, desktop) { return Ok(false); }
        tauri::async_runtime::spawn_blocking(move || update.install(bytes)).await
            .map_err(|e| format!("Update installer failed: {e}"))?
            .map_err(|e| format!("Update installation failed: {e}"))?;
        Ok(true)
    }.await;
    match prepared {
        Ok(true) => {
            desktop.quitting.store(true, Ordering::SeqCst);
            app.restart();
        }
        result => {
            let resumed = if drain_confirmed || drain_complete {
                restore(desktop, resume_worker, drain_complete).await
            } else {
                Ok(())
            };
            if resumed.is_ok() {
                let _ = std::fs::remove_file(resume_path(desktop));
            }
            if let Err(message) = resumed {
                return Err(format!(
                    "Worker activity could not be restored: {message}. Start it from Overview when ready."
                ));
            }
            match result {
                Err(message) => Err(message),
                _ => {
                    change(app, |inner| {
                        inner.snapshot.status = Status::Ready;
                        inner.snapshot.install_queued = false;
                        inner.snapshot.error = None;
                    });
                    Ok(())
                }
            }
        }
    }
}

fn lock_for_install(state: &DesktopState) -> Result<std::fs::File, String> {
    use fs2::FileExt;
    let path = state.directory.join("worker.lock");
    if std::fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err("Worker lock cannot be a symbolic link.".into());
    }
    let mut options = std::fs::OpenOptions::new();
    options.create(true).truncate(false).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options
        .open(path)
        .map_err(|e| format!("Cannot check worker lock: {e}"))?;
    file.try_lock_exclusive().map_err(|_| {
        "A worker is running. No update was installed; let it finish before retrying.".to_owned()
    })?;
    Ok(file)
}

fn resume_path(state: &DesktopState) -> PathBuf {
    state.directory.join("resume-after-update")
}
fn write_resume(state: &DesktopState) -> Result<(), String> {
    use std::io::Write;
    let path = resume_path(state);
    if std::fs::read(&path).is_ok_and(|bytes| bytes == b"resume-worker\n") {
        return Ok(());
    }
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|e| format!("Cannot save worker restart intent: {e}"))?;
    file.write_all(b"resume-worker\n")
        .and_then(|_| file.sync_all())
        .map_err(|e| format!("Cannot save worker restart intent: {e}"))
}

pub async fn resume_after_update(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let path = resume_path(&state);
    if !path.exists() {
        return Ok(());
    }
    let _guard = state.gate.lock().await;
    crate::ensure_mutable(&state)?;
    cli(&state, "start", None).await?;
    std::fs::remove_file(path).map_err(|e| format!("Cannot clear worker restart intent: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn incomplete_or_busy_worker_status_never_allows_immediate_install() {
        for status in [
            json!({}),
            json!({"running":true}),
            json!({"running":true,"state":"busy"}),
            json!({"running":true,"state":"idle","scanId":"scan"}),
            json!({"running":true,"state":"draining"}),
        ] {
            assert!(busy(&status));
        }
        assert!(running(&json!({})).is_err());
        assert!(!busy(&json!({"running":false,"state":"stopped"})));
        assert!(!busy(&json!({"running":true,"state":"idle"})));
    }

    #[test]
    fn release_links_are_limited_to_worker_versions() {
        assert!(valid_release_url(&format!("{RELEASES_URL}v0.3.5")));
        assert!(valid_release_url(&format!("{RELEASES_URL}windows-v0.3.5")));
        for url in [
            "https://example.com/v0.3.5",
            "file:///tmp/v0.3.5",
            "https://github.com/pstoebenau/guerrilla-worker/releases/tag/v0.3.5/../../issues",
        ] {
            assert!(!valid_release_url(url));
        }
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use base64::{Engine, engine::general_purpose::STANDARD};
    use serde_json::json;
    use std::sync::atomic::AtomicU64;

    static NEXT: AtomicU64 = AtomicU64::new(0);
    fn directory() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "guerrilla-updater-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }
    fn desktop(directory: PathBuf) -> DesktopState {
        DesktopState {
            worker: directory.join("worker"),
            resources: directory.clone(),
            directory,
            gate: tokio::sync::Mutex::new(()),
            quitting: AtomicBool::new(false),
            updating: AtomicBool::new(false),
        }
    }
    fn app() -> tauri::App<tauri::test::MockRuntime> {
        let app = tauri::test::mock_app();
        app.manage(State::default());
        app
    }

    #[test]
    fn acknowledged_cancel_cannot_cross_the_installer_transition() {
        let app = app();
        let desktop = desktop(directory());
        {
            let state = app.state::<State>();
            let mut inner = state.inner.lock().unwrap();
            inner.snapshot.status = Status::WaitingForIdle;
            inner.snapshot.install_queued = true;
        }
        assert!(!cancel(app.handle()).unwrap().install_queued);
        assert!(!begin_install(app.handle(), &desktop));
        app.state::<State>().cancel.store(false, Ordering::SeqCst);
        assert!(begin_install(app.handle(), &desktop));
        assert!(cancel(app.handle()).is_err());
        std::fs::remove_dir_all(desktop.directory).unwrap();
    }

    #[cfg(unix)]
    fn fixture_script(state: &DesktopState, source: &str) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::write(&state.worker, source).unwrap();
        std::fs::set_permissions(&state.worker, std::fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn draining_waits_for_daemon_exit_and_restores_activity_without_stop() {
        let state = desktop(directory());
        fixture_script(
            &state,
            r#"#!/bin/sh
root=$(dirname "$0")
case "$3" in
status)
 if [ -f "$root/finished" ]; then printf '{"running":false,"state":"stopped"}';
 else printf '{"running":true,"state":"draining","scanId":"active-scan"}'; fi;;
start) touch "$root/restarted"; printf '{"running":true,"state":"idle"}';;
*) touch "$root/unexpected-command"; exit 2;;
esac
"#,
        );
        tauri::async_runtime::block_on(async {
            assert!(
                tokio::time::timeout(Duration::from_millis(60), wait_for_drain(&state))
                    .await
                    .is_err()
            );
            assert!(!state.directory.join("restarted").exists());
            std::fs::write(state.directory.join("finished"), "").unwrap();
            wait_for_drain(&state).await.unwrap();
            restore(&state, true, true).await.unwrap();
            assert!(state.directory.join("restarted").exists());
            assert!(!state.directory.join("unexpected-command").exists());
        });
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn explicit_quit_releases_drain_gate_and_never_restarts_worker() {
        let state = desktop(directory());
        fixture_script(&state, "#!/bin/sh\nexit 99\n");
        state.quitting.store(true, Ordering::SeqCst);
        tauri::async_runtime::block_on(async {
            let guard = state.gate.lock().await;
            assert!(wait_for_drain(&state).await.is_err());
            restore(&state, true, false).await.unwrap();
            drop(guard);
            assert!(state.gate.try_lock().is_ok());
        });
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn later_external_worker_start_is_preserved_and_mutations_are_rejected_while_updating() {
        let state = desktop(directory());
        fixture_script(
            &state,
            "#!/bin/sh\n[ \"$3\" = status ] || exit 99\nprintf '{\"running\":true,\"state\":\"busy\"}'\n",
        );
        tauri::async_runtime::block_on(async {
            tokio::time::timeout(Duration::from_secs(2), restore(&state, true, true))
                .await
                .unwrap()
                .unwrap();
            state.updating.store(true, Ordering::SeqCst);
            assert!(crate::action(&state, "start").await.is_err());
            assert!(crate::action(&state, "disconnect").await.is_err());
        });
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    // Public throwaway signing fixture from the pinned official updater's tests.
    const PUBLIC_KEY: &str = "untrusted comment: minisign public key: A49EF4746FA6E6D7\nRWTX5qZvdPSepDGWg2bz8n6ArQnBMwgQK1geVosorcbLzcunD+KJ+cYe";
    const SIGNATURE: &str = "untrusted comment: signature from tauri secret key\nRUTX5qZvdPSepPF2zsjvP/L/TQtBLRnWRvaq+4omK1UEugtQp/USrGiI1v762sDy3id3aor7bRC9ETyeot6lMHHrWoZ5hjUM1Qc=\ntrusted comment: timestamp:1791649051\tfile:payload\tversion:2.0.0\nDDq2CLOEl5cQmC2eJRHlnZPVViOZsu1CjpPKQoei4A/XBfkDqGO0B+QMrcrHgEQ5qV3mne75tdFWhJpqUXh9AA==";

    fn manifest(url: &str, signature: &str) -> Value {
        json!({"version":"2.0.0", "platforms":{"fixture":{"url":url,"signature":signature}}})
    }

    fn updater(key: &str, executable: PathBuf) -> tauri_plugin_updater::Updater {
        initialize_tls();
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context.config_mut().plugins.0.insert(
            "updater".into(),
            json!({"pubkey":key, "requireSignedVersion":true}),
        );
        let app = tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().pubkey(key).build())
            .build(context)
            .unwrap();
        app.updater_builder()
            .target("fixture")
            .executable_path(executable)
            .endpoints(vec!["https://example.invalid/update".parse().unwrap()])
            .unwrap()
            .timeout(Duration::from_secs(5))
            .no_proxy()
            .build()
            .unwrap()
    }

    fn cached_fixture(
        app: &tauri::App<tauri::test::MockRuntime>,
        desktop: &DesktopState,
        bytes: &[u8],
    ) {
        let updater = updater(
            &STANDARD.encode(PUBLIC_KEY),
            desktop.directory.join("old-app"),
        );
        let update = updater
            .restore_update(manifest(
                "https://example.invalid/package",
                &STANDARD.encode(SIGNATURE),
            ))
            .unwrap()
            .unwrap();
        let package = desktop.directory.join("download.bin");
        std::fs::write(&package, bytes).unwrap();
        let state = app.state::<State>();
        let mut inner = state.inner.lock().unwrap();
        inner.update = Some(update);
        inner.package = Some(package);
        inner.snapshot.status = Status::WaitingForIdle;
        inner.snapshot.install_queued = true;
    }

    #[cfg(unix)]
    #[test]
    fn accepted_drain_with_failed_response_still_restores_after_cancel() {
        let app = app();
        let state = desktop(directory());
        fixture_script(
            &state,
            r#"#!/bin/sh
root=$(dirname "$0")
case "$3" in
status)
 if [ -f "$root/finished" ]; then printf '{"running":false,"state":"stopped"}';
 else printf '{"running":true,"state":"busy","scanId":"active-scan"}'; fi;;
drain) touch "$root/finished"; sleep 0.15; printf 'invalid-response';;
start) touch "$root/restarted"; printf '{"running":true,"state":"idle"}';;
*) touch "$root/unexpected-command"; exit 2;;
esac
"#,
        );
        cached_fixture(&app, &state, b"test");
        tauri::async_runtime::block_on(async {
            let operation = prepare_and_install(app.handle(), &state, true);
            let cancellation = async {
                while !state.directory.join("finished").exists() {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                cancel(app.handle()).unwrap();
            };
            let (result, _) = tokio::time::timeout(Duration::from_secs(4), async {
                tokio::join!(operation, cancellation)
            })
            .await
            .unwrap();
            result.unwrap();
            assert!(state.directory.join("restarted").exists());
            assert!(!state.directory.join("unexpected-command").exists());
            assert!(snapshot(app.handle()).status == Status::Ready);
        });
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn corrupt_cache_is_discarded_before_drain_and_can_be_downloaded_again() {
        let app = app();
        let state = desktop(directory());
        fixture_script(
            &state,
            "#!/bin/sh\n[ \"$3\" = status ] || exit 99\nprintf '{\"running\":true,\"state\":\"busy\"}'\n",
        );
        cached_fixture(&app, &state, b"corrupted");
        assert!(
            tauri::async_runtime::block_on(prepare_and_install(app.handle(), &state, true))
                .is_err()
        );
        let store = app.state::<State>();
        assert!(store.inner.lock().unwrap().package.is_none());
        assert!(store.inner.lock().unwrap().update.is_none());
        assert!(!state.directory.join("download.bin").exists());
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn failed_drain_writes_return_instead_of_waiting_on_an_unchanged_worker() {
        let app = app();
        let state = desktop(directory());
        fixture_script(
            &state,
            r#"#!/bin/sh
case "$3" in
status) printf '{"running":true,"state":"busy","scanId":"active-scan"}';;
drain) exit 19;;
*) exit 99;;
esac
"#,
        );
        cached_fixture(&app, &state, b"test");
        tauri::async_runtime::block_on(async {
            let guard = state.gate.lock().await;
            let result = tokio::time::timeout(
                Duration::from_secs(5),
                prepare_and_install(app.handle(), &state, true),
            )
            .await
            .unwrap();
            assert!(result.unwrap_err().contains("Cannot confirm worker drain"));
            drop(guard);
            assert!(state.gate.try_lock().is_ok());
        });
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[test]
    fn install_lock_rejects_external_worker_and_is_released_for_resume() {
        let state = desktop(directory());
        let lock = lock_for_install(&state).unwrap();
        assert!(lock_for_install(&state).is_err());
        drop(lock);
        assert!(lock_for_install(&state).is_ok());
        std::fs::remove_dir_all(state.directory).unwrap();
    }

    #[test]
    fn official_plugin_rejects_tampering_and_wrong_announced_versions_before_install() {
        let directory = directory();
        let original = directory.join("original-app");
        std::fs::write(&original, "old app").unwrap();
        let updater = updater(&STANDARD.encode(PUBLIC_KEY), original.clone());
        let metadata = manifest(
            "https://example.invalid/package",
            &STANDARD.encode(SIGNATURE),
        );
        let update = updater.restore_update(metadata.clone()).unwrap().unwrap();
        update.verify(b"test").unwrap();
        assert!(update.verify(b"tampered").is_err());
        assert!(update.install(b"tampered").is_err());
        let mut wrong_version = metadata;
        wrong_version["version"] = json!("3.0.0");
        let wrong = updater.restore_update(wrong_version).unwrap().unwrap();
        assert!(wrong.verify(b"test").is_err());
        assert!(wrong.install(b"test").is_err());
        assert_eq!(std::fs::read_to_string(original).unwrap(), "old app");
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn persisted_cache_is_reused_only_after_signature_verification() {
        let directory = directory();
        let package = directory.join("download.bin");
        let updater = updater(&STANDARD.encode(PUBLIC_KEY), directory.join("app"));
        let update = updater
            .restore_update(manifest(
                "https://example.invalid/package",
                &STANDARD.encode(SIGNATURE),
            ))
            .unwrap()
            .unwrap();
        std::fs::write(&package, "test").unwrap();
        assert_eq!(verified_cache_size(&update, &package), Some(4));
        std::fs::write(&package, "corrupted").unwrap();
        assert_eq!(verified_cache_size(&update, &package), None);
        assert!(!package.exists());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn official_plugin_download_rejects_unsigned_or_corrupted_payload() {
        use std::{
            io::{BufRead, BufReader, Write},
            net::TcpListener,
        };
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
            }
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\ntampered",
                )
                .unwrap();
        });
        let directory = directory();
        let updater = updater(&STANDARD.encode(PUBLIC_KEY), directory.join("app"));
        let update = updater
            .restore_update(manifest(
                &format!("http://{address}/package"),
                &STANDARD.encode(SIGNATURE),
            ))
            .unwrap()
            .unwrap();
        assert!(tauri::async_runtime::block_on(update.download(|_, _| {}, || {})).is_err());
        server.join().unwrap();
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires a throwaway signed Fixture.app.tar.gz; never targets the installed app"]
    fn native_signed_macos_fixture_installs_and_preserves_old_app_on_rejection() {
        let payload =
            std::fs::read(std::env::var("GUERRILLA_UPDATER_TEST_PACKAGE").unwrap()).unwrap();
        let key =
            std::fs::read_to_string(std::env::var("GUERRILLA_UPDATER_TEST_PUBLIC_KEY").unwrap())
                .unwrap();
        let signature =
            std::fs::read_to_string(std::env::var("GUERRILLA_UPDATER_TEST_SIGNATURE").unwrap())
                .unwrap();
        let directory = directory();
        let app = directory.join("Fixture.app");
        std::fs::create_dir_all(app.join("Contents/MacOS")).unwrap();
        std::fs::write(app.join("Contents/version"), "old").unwrap();
        std::fs::write(app.join("Contents/MacOS/fixture"), "old executable").unwrap();
        let updater = updater(key.trim(), app.join("Contents/MacOS/fixture"));
        let version =
            std::env::var("GUERRILLA_UPDATER_TEST_VERSION").unwrap_or_else(|_| "2.0.0".into());
        let mut metadata = manifest("https://example.invalid/package", signature.trim());
        metadata["version"] = json!(version);
        let update = updater.restore_update(metadata.clone()).unwrap().unwrap();
        let mut corrupted = payload.clone();
        corrupted[0] ^= 1;
        assert!(update.install(&corrupted).is_err());
        assert_eq!(
            std::fs::read_to_string(app.join("Contents/version")).unwrap(),
            "old"
        );
        let mut metadata = metadata;
        let mut wrong_version = semver::Version::parse(&version).unwrap();
        wrong_version.patch += 1;
        metadata["version"] = json!(wrong_version.to_string());
        let wrong = updater.restore_update(metadata).unwrap().unwrap();
        assert!(wrong.install(&payload).is_err());
        assert_eq!(
            std::fs::read_to_string(app.join("Contents/version")).unwrap(),
            "old"
        );
        update.install(payload).unwrap();
        if app.join("Contents/Info.plist").exists() {
            let installed = std::process::Command::new("/usr/bin/plutil")
                .args(["-extract", "CFBundleShortVersionString", "raw", "-o", "-"])
                .arg(app.join("Contents/Info.plist"))
                .output()
                .unwrap();
            assert!(installed.status.success());
            assert_eq!(String::from_utf8(installed.stdout).unwrap().trim(), version);
        } else {
            assert_eq!(
                std::fs::read_to_string(app.join("Contents/version")).unwrap(),
                "new"
            );
        }
        if std::env::var_os("GUERRILLA_UPDATER_TEST_KEEP").is_some() {
            println!("Installed test app: {}", app.display());
        } else {
            std::fs::remove_dir_all(directory).unwrap();
        }
    }
}

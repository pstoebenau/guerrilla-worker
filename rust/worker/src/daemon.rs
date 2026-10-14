use crate::{
    api::{body, Client},
    enrollment, execution, files,
    profile::ContextPaths,
    protocol::{redact, Credentials, Gpu, Poll},
};
use anyhow::{bail, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub state: String,
    pub connected: bool,
    #[serde(default)]
    pub running: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub worker_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stage: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress: Option<Value>,
    pub message: String,
    pub gpus: Vec<Gpu>,
    pub engines: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scan_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempt_id: Option<String>,
}
impl Default for Status {
    fn default() -> Self {
        Self {
            state: "stopped".into(),
            connected: false,
            running: false,
            worker_id: None,
            backend: None,
            stage: None,
            progress: None,
            message: "Worker stopped".into(),
            gpus: vec![],
            engines: json!({}),
            scan_id: None,
            attempt_id: None,
        }
    }
}
fn open_lock(path: &Path) -> Result<File> {
    std::fs::create_dir_all(path.parent().context("Missing lock parent")?)?;
    if std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("Lock path cannot be a link");
    }
    let mut options = OpenOptions::new();
    options.create(true).truncate(false).write(true).read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options.open(path)?;
    Ok(file)
}
pub fn running(paths: &ContextPaths) -> Result<bool> {
    let file = open_lock(&paths.state.join("worker.lock"))?;
    match file.try_lock_exclusive() {
        Ok(()) => Ok(false),
        Err(e)
            if e.kind() == std::io::ErrorKind::WouldBlock
                || e.raw_os_error() == fs2::lock_contended_error().raw_os_error() =>
        {
            Ok(true)
        }
        Err(e) => Err(e.into()),
    }
}
pub fn status(paths: &ContextPaths) -> Result<Status> {
    let mut status: Status = std::fs::read(paths.state.join("status.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    let credentials = paths.credentials()?;
    status.worker_id = credentials.as_ref().map(|c| c.worker_id.clone());
    status.running = running(paths)?;
    if !status.running {
        let failed = status.state == "error";
        status.state = if credentials.is_none() {
            "unenrolled"
        } else if failed {
            "error"
        } else {
            "stopped"
        }
        .into();
        status.connected = false;
        status.backend = None;
        status.stage = None;
        status.progress = None;
        status.scan_id = None;
        status.attempt_id = None;
        if !failed || credentials.is_none() {
            status.message = if credentials.is_none() {
                "Enroll this worker to begin"
            } else {
                "Worker stopped"
            }
            .into();
        }
    }
    Ok(status)
}
pub async fn enroll(paths: &ContextPaths, token: &str) -> Result<Status> {
    if token.is_empty() || token.len() > 4096 {
        bail!("Enrollment token must contain between 1 and 4096 characters");
    }
    if paths.credentials()?.is_some() {
        bail!("Worker already enrolled; disconnect before enrolling again");
    }
    if paths.profile.control_url.is_empty() {
        bail!("Set controlUrl before enrolling");
    }
    // Enrollment establishes an identity. Verified capabilities are sent when
    // starting the worker, after GPU discovery and engine dependency checks.
    let registration = ContextPaths::enrollment_identity();
    let client = Client::new(&paths.profile.control_url, "")?;
    let enrolled: Value = client
        .post(
            "enroll",
            &json!({"token":token,"registration":registration}),
            &CancellationToken::new(),
        )
        .await
        .map_err(enrollment::server_error)?;
    let credentials = Credentials {
        origin: client.origin,
        worker_id: enrolled["workerId"]
            .as_str()
            .context("Missing worker id")?
            .into(),
        credential: enrolled["credential"]
            .as_str()
            .context("Missing worker credential")?
            .into(),
    };
    let credential_path = paths.credential_path();
    files::private_dir(
        credential_path
            .parent()
            .context("Invalid credential path")?,
    )?;
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    use std::io::Write;
    let mut file = options.open(credential_path)?;
    file.write_all(&serde_json::to_vec(&credentials)?)?;
    file.sync_all()?;
    status(paths)
}
pub async fn start(paths: &ContextPaths) -> Result<Status> {
    if running(paths)? {
        return status(paths);
    }
    if paths.credentials()?.is_none() {
        bail!("Enroll worker before starting");
    }
    let _ = std::fs::remove_file(paths.state.join("command.json"));
    let log = open_lock(&paths.state.join("daemon.log"))?;
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    drop(log);
    let log = options.open(paths.state.join("daemon.log"))?;
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .args([
            "--config",
            paths.config.to_str().context("Invalid config path")?,
            "run",
        ])
        .stdin(std::process::Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    command.env_remove("WORKER_ENROLLMENT_TOKEN");
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000 | 0x00000200);
    }
    let mut child = command.spawn()?;
    for _ in 0..100 {
        if running(paths)? {
            return status(paths);
        }
        if child.try_wait()?.is_some() {
            bail!("Worker failed to start; see diagnostics logs");
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    bail!("Worker startup not acknowledged; check status and diagnostics before retrying")
}
pub async fn control(paths: &ContextPaths, command: &str) -> Result<Status> {
    if !matches!(command, "stop" | "drain") {
        bail!("Invalid local command");
    }
    if !running(paths)? {
        return status(paths);
    }
    files::write_private(
        &paths.state.join("command.json"),
        &serde_json::to_vec(&json!({"command":command}))?,
    )?;
    if command == "stop" {
        for _ in 0..350 {
            if !running(paths)? {
                return status(paths);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        bail!("Worker shutdown is still in progress; retry status");
    }
    status(paths)
}
pub async fn disconnect(paths: &ContextPaths) -> Result<Status> {
    control(paths, "stop").await?;
    if running(paths)? {
        bail!("Stop worker before disconnecting");
    }
    match std::fs::remove_file(paths.credential_path()) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    };
    status(paths)
}
pub fn logs(paths: &ContextPaths) -> Result<Value> {
    use std::io::{Read, Seek, SeekFrom};
    let path = paths.state.join("daemon.log");
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(json!({"lines":[]})),
        Err(e) => return Err(e.into()),
    };
    let size = file.metadata()?.len();
    let offset = size.saturating_sub(64 * 1024);
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = Vec::new();
    file.take(64 * 1024).read_to_end(&mut bytes)?;
    let text = String::from_utf8_lossy(&bytes);
    let text = if offset > 0 {
        text.split_once('\n').map(|(_, rest)| rest).unwrap_or("")
    } else {
        &text
    };
    let lines: Vec<_> = text.lines().rev().take(200).map(redact).collect();
    Ok(json!({"lines":lines.into_iter().rev().collect::<Vec<_>>()}))
}
async fn publish(
    paths: &ContextPaths,
    status: &Arc<Mutex<Status>>,
    state: &str,
    message: &str,
    connected: bool,
) -> Result<()> {
    let mut status = status.lock().await;
    status.state = state.into();
    status.message = redact(message);
    status.connected = connected;
    status.running = state != "stopped";
    if state != "busy" && state != "draining" {
        status.backend = None;
        status.stage = None;
        status.progress = None;
        status.scan_id = None;
        status.attempt_id = None;
    }
    files::write_private(
        &paths.state.join("status.json"),
        &serde_json::to_vec(&*status)?,
    )?;
    Ok(())
}
pub async fn run(paths: ContextPaths) -> Result<()> {
    let lock = open_lock(&paths.state.join("worker.lock"))?;
    lock.try_lock_exclusive()
        .context("Another worker process already owns this profile")?;
    let status = Arc::new(Mutex::new(Status::default()));
    publish(
        &paths,
        &status,
        "starting",
        "Checking worker dependencies",
        false,
    )
    .await?;
    let cancel = CancellationToken::new();
    let drain = Arc::new(AtomicBool::new(false));
    let signals = cancel.clone();
    tokio::spawn(async move {
        #[cfg(unix)]
        {
            let mut term =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
            let mut hup =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup()).unwrap();
            tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{},_=hup.recv()=>{}}
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
        signals.cancel();
    });
    let command_cancel = cancel.clone();
    let command_drain = drain.clone();
    let command_path = paths.state.join("command.json");
    let command_status = status.clone();
    let command_state_path = paths.state.join("status.json");
    #[cfg(target_os = "linux")]
    let session_id = std::env::var("XDG_SESSION_ID").ok();
    let commands = tokio::spawn(async move {
        let mut ticks = 0u64;
        #[cfg(target_os = "linux")]
        let mut session_verified = false;
        while !command_cancel.is_cancelled() {
            tokio::time::sleep(Duration::from_millis(250)).await;
            if let Ok(bytes) = std::fs::read(&command_path) {
                let _ = std::fs::remove_file(&command_path);
                if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
                    match value["command"].as_str() {
                        Some("stop") => command_cancel.cancel(),
                        Some("drain") => {
                            command_drain.store(true, Ordering::SeqCst);
                            let mut status = command_status.lock().await;
                            status.state = "draining".into();
                            status.message = "Finishing current job, then pausing".into();
                            if let Ok(bytes) = serde_json::to_vec(&*status) {
                                let _ = files::write_private(&command_state_path, &bytes);
                            }
                        }
                        _ => {}
                    }
                }
            }
            ticks += 1;
            if ticks.is_multiple_of(20) {
                #[cfg(target_os = "linux")]
                if let Some(session) = &session_id {
                    let result = tokio::time::timeout(
                        Duration::from_secs(3),
                        tokio::process::Command::new("loginctl")
                            .args(["show-session", session, "--property=State", "--value"])
                            .kill_on_drop(true)
                            .output(),
                    )
                    .await;
                    match result {
                        Ok(Ok(result)) if result.status.success() => {
                            let state = String::from_utf8_lossy(&result.stdout);
                            if matches!(state.trim(), "active" | "online") {
                                session_verified = true;
                            }
                            if matches!(state.trim(), "closing" | "") {
                                command_cancel.cancel();
                            }
                        }
                        _ if session_verified => command_cancel.cancel(),
                        _ => {}
                    }
                }
                #[cfg(target_os = "macos")]
                {
                    use std::os::unix::fs::MetadataExt;
                    if let Ok(metadata) = std::fs::metadata("/dev/console") {
                        if metadata.uid() != unsafe { libc::geteuid() } {
                            command_cancel.cancel();
                        }
                    }
                }
            }
        }
    });
    let result = run_loop(&paths, &status, &cancel, drain).await;
    cancel.cancel();
    commands.abort();
    match &result {
        Ok(()) => {
            publish(
                &paths,
                &status,
                "stopped",
                "Worker stopped; recovery files retained",
                false,
            )
            .await?
        }
        Err(error) => publish(&paths, &status, "error", &error.to_string(), false).await?,
    }
    drop(lock);
    result
}
async fn run_loop(
    paths: &ContextPaths,
    status: &Arc<Mutex<Status>>,
    cancel: &CancellationToken,
    drain: Arc<AtomicBool>,
) -> Result<()> {
    let report = tokio::select! {_=cancel.cancelled()=>return Ok(()),r=paths.preflight()=>r?};
    let info = ContextPaths::registration(&report)?;
    {
        let mut status = status.lock().await;
        status.gpus = info.gpus.clone();
        status.engines = report["backends"].clone();
    }
    if !info.healthy {
        bail!("Install at least one supported engine and resolve preflight checks before starting");
    }
    let gpu_id = &info.gpus[0].id;
    let gpu_dir = std::env::var_os("WORKER_AGENT_LOCK_DIRECTORY")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            if cfg!(windows) {
                dirs::data_local_dir()
                    .unwrap_or_else(std::env::temp_dir)
                    .join("Guerrilla")
            } else {
                std::env::temp_dir()
            }
        });
    if !gpu_dir.is_absolute() {
        bail!("WORKER_AGENT_LOCK_DIRECTORY must be absolute");
    }
    // The global GPU lock also interoperates with the existing Python agent lock.
    let gpu_lock = open_lock(&gpu_dir.join(format!(
        "guerrilla-agent-{}.lock",
        hex::encode(Sha256::digest(gpu_id.as_bytes()))
    )))?;
    gpu_lock
        .try_lock_exclusive()
        .context("Another worker owns this GPU")?;
    let credentials = loop {
        if cancel.is_cancelled() {
            return Ok(());
        }
        if let Some(credentials) = paths.credentials()? {
            break credentials;
        }
        publish(paths, status, "unenrolled", "Waiting for enrollment", false).await?;
        tokio::select! {_=cancel.cancelled()=>return Ok(()),_=tokio::time::sleep(Duration::from_secs(3))=>{}}
    };
    status.lock().await.worker_id = Some(credentials.worker_id);
    let client = Client::new(&credentials.origin, &credentials.credential)?;
    files::private_dir(&paths.scratch)?;
    let mut clean = files::clean(&paths.scratch)?;
    let mut recovering = true;
    while !cancel.is_cancelled() && !drain.load(Ordering::SeqCst) {
        let connected = status.lock().await.connected;
        publish(
            paths,
            status,
            "idle",
            "Waiting for assigned work",
            connected,
        )
        .await?;
        let response = client
            .post::<Poll>(
                "poll",
                &json!({"registration":info,"clean":clean,"recovering":recovering}),
                cancel,
            )
            .await;
        let step: Result<u64> = async {
            let response = response?;
            publish(paths,status,"idle","Connected; waiting for assigned work",true).await?;
            recovering = response.recovery_pending;
            for fence in &response.cleanup {
                files::remove(&paths.scratch, &fence.scan_id)?;
                let _: Value = client
                    .post("cleanup", &serde_json::to_value(fence)?, cancel)
                    .await?;
                let _: Result<Value> = client
                    .post(
                        "finish",
                        &body(
                            fence,
                            json!({"state":"interrupted","error":"Cancelled by owner."}),
                        ),
                        cancel,
                    )
                    .await;
            }
            if !response.cleanup.is_empty() {
                clean = files::clean(&paths.scratch)?;
                return Ok(0);
            }
            if response.command != "continue" {
                drain.store(true, Ordering::SeqCst);
                return Ok(0);
            }
            if let Some(assignment) = response.assignment {
                if drain.load(Ordering::SeqCst) {
                    let _:Value=client.post("finish",&body(&assignment.fence,json!({"state":"interrupted","error":"Worker locally drained before starting assignment."})),cancel).await?;
                    return Ok(0);
                }
                clean = false;
                {
                    let mut s = status.lock().await;
                    s.scan_id = Some(assignment.fence.scan_id.clone());
                    s.attempt_id = Some(assignment.fence.attempt_id.clone());
                }
                let server_drain=execution::execute(
                    paths,
                    &client,
                    assignment,
                    cancel,
                    drain.clone(),
                    status.clone(),
                )
                .await?;
                clean = files::clean(&paths.scratch)?;
                if server_drain {
                    let _:Poll=client.post("poll",&json!({"registration":info,"clean":clean}),cancel).await?;
                }
            }
            Ok(response.poll_after_ms.clamp(1000, 30000))
        }
        .await;
        match step {
            Ok(delay) => {
                if drain.load(Ordering::SeqCst) {
                    break;
                }
                tokio::select! {_=cancel.cancelled()=>break,_=tokio::time::sleep(Duration::from_millis(delay))=>{}}
            }
            Err(error) => {
                if cancel.is_cancelled() {
                    break;
                }
                clean = files::clean(&paths.scratch).unwrap_or(false);
                publish(paths, status, "error", &error.to_string(), false).await?;
                eprintln!("{}", redact(&error.to_string()));
                tokio::select! {_=cancel.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(5))=>{}}
            }
        }
    }
    if drain.load(Ordering::SeqCst) && !cancel.is_cancelled() {
        publish(
            paths,
            status,
            "draining",
            "Worker drained; no new assignments will be accepted",
            true,
        )
        .await?;
    }
    drop(gpu_lock);
    Ok(())
}

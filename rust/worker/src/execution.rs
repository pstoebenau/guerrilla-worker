use crate::{
    api::{body, ApiError, Client},
    archive::{self, BackgroundArchive},
    daemon::Status,
    files,
    profile::ContextPaths,
    protocol::{redact, valid_id, Assignment, Fence, Heartbeat},
};
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::Mutex,
};
use tokio_util::sync::CancellationToken;

fn timestamp(value: &str) -> Result<i64> {
    Ok(chrono::DateTime::parse_from_rfc3339(value)?.timestamp_millis())
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
struct LeaseTasks {
    cancel: CancellationToken,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}
impl Drop for LeaseTasks {
    fn drop(&mut self) {
        self.cancel.cancel();
        for task in &self.tasks {
            task.abort();
        }
    }
}
struct Events {
    client: Client,
    fence: Fence,
    sequence: AtomicU64,
    cancel: CancellationToken,
    status: Arc<Mutex<Status>>,
    state: std::path::PathBuf,
    drain: Arc<AtomicBool>,
    serial: Mutex<()>,
}
impl Events {
    async fn send(
        &self,
        kind: &str,
        message: &str,
        progress: Value,
        stage: Option<&str>,
        versions: Option<&Value>,
    ) -> Result<()> {
        let _serial = self.serial.lock().await;
        let mut update = body(
            &self.fence,
            json!({"eventId":uuid::Uuid::new_v4().to_string(),"sequence":self.sequence.fetch_add(1,Ordering::SeqCst)+1,"kind":kind,"message":redact(message),"progress":progress}),
        );
        if let Some(stage) = stage {
            update["stage"] = stage.into();
        }
        if let Some(versions) = versions {
            update["runtimeVersions"] = versions.clone();
        }
        let _: Value = self.client.post("events", &update, &self.cancel).await?;
        if kind == "upload" {
            return Ok(());
        }
        let mut status = self.status.lock().await;
        if self.drain.load(Ordering::SeqCst) {
            status.state = "draining".into();
        }
        if let Some(stage) = stage {
            status.stage = Some(stage.into());
        }
        status.message = redact(message);
        status.progress = Some(progress);
        files::write_private(
            &self.state.join("status.json"),
            &serde_json::to_vec(&*status)?,
        )?;
        Ok(())
    }
}
pub async fn execute(
    paths: &ContextPaths,
    client: &Client,
    assignment: Assignment,
    shutdown: &CancellationToken,
    local_drain: Arc<AtomicBool>,
    status: Arc<Mutex<Status>>,
) -> Result<bool> {
    let backend = assignment.request["backend"]
        .as_str()
        .context("Missing backend")?;
    if assignment.protocol_version != 2
        || backend != "spirula"
        || assignment.request["maxCap"]
            .as_u64()
            .filter(|v| *v > 0 && *v <= 30_000_000)
            .is_none()
        || [
            &assignment.fence.scan_id,
            &assignment.fence.attempt_id,
            &assignment.fence.lease_id,
        ]
        .iter()
        .any(|id| !valid_id(id))
    {
        bail!("Incompatible assignment");
    }
    let deadline = Arc::new(AtomicI64::new(timestamp(&assignment.lease_expires_at)?));
    if deadline.load(Ordering::SeqCst) <= now() {
        bail!("Expired assignment");
    }
    let cancel = shutdown.child_token();
    let task_cancel = cancel.clone();
    let client_heartbeat = client.clone();
    let fence = assignment.fence.clone();
    let expiry = deadline.clone();
    let cancelled = Arc::new(AtomicBool::new(false));
    let owner_cancelled = cancelled.clone();
    let drain = local_drain.clone();
    let server_drain = Arc::new(AtomicBool::new(false));
    let heartbeat_drain = server_drain.clone();
    let watchdog = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_millis(250));
        loop {
            tokio::select! {_=task_cancel.cancelled()=>break,_=interval.tick()=>{if now()>=expiry.load(Ordering::SeqCst){task_cancel.cancel();break;}}}
        }
    });
    let heartbeat_cancel = cancel.clone();
    let heartbeat = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(5));
        interval.tick().await;
        loop {
            tokio::select! {_=heartbeat_cancel.cancelled()=>break,_=interval.tick()=>{}}
            match client_heartbeat
                .post::<Heartbeat>(
                    "heartbeat",
                    &serde_json::to_value(&fence).unwrap(),
                    &heartbeat_cancel,
                )
                .await
            {
                Ok(response) => {
                    if response.command == "stop" {
                        owner_cancelled.store(
                            response.reason.as_deref() == Some("cancelled"),
                            Ordering::SeqCst,
                        );
                        heartbeat_cancel.cancel();
                        break;
                    }
                    if response.command == "drain" {
                        drain.store(true, Ordering::SeqCst);
                        heartbeat_drain.store(true, Ordering::SeqCst);
                    }
                    if let Ok(expiry) = timestamp(&response.lease_expires_at) {
                        deadline.store(expiry, Ordering::SeqCst);
                    }
                }
                Err(error) => {
                    if error
                        .downcast_ref::<ApiError>()
                        .is_some_and(|s| s.0 < 500 && s.0 != 429)
                    {
                        heartbeat_cancel.cancel();
                        break;
                    }
                    eprintln!("Lease renewal transport failed; deadline remains enforced");
                }
            }
        }
    });
    let lease_tasks = LeaseTasks {
        cancel: cancel.clone(),
        tasks: vec![watchdog, heartbeat],
    };
    let job = files::checked(
        &paths.scratch,
        &format!(
            "{}/{}",
            assignment.fence.scan_id, assignment.fence.attempt_id
        ),
    )?;
    let output = job.join("run");
    {
        let mut status = status.lock().await;
        status.state = "busy".into();
        status.backend = Some(backend.into());
        status.message = "Preparing assignment".into();
        files::write_private(
            &paths.state.join("status.json"),
            &serde_json::to_vec(&*status)?,
        )?;
    }
    let events = Arc::new(Events {
        client: client.clone(),
        fence: assignment.fence.clone(),
        sequence: AtomicU64::new(0),
        cancel: cancel.clone(),
        status,
        state: paths.state.clone(),
        drain: local_drain.clone(),
        serial: Mutex::new(()),
    });
    let archive_cancel = cancel.child_token();
    let archives = BackgroundArchive::new(
        client.clone(),
        assignment.fence.clone(),
        output.clone(),
        assignment.resume.clone(),
        archive_cancel.clone(),
    );
    let upload_events = events.clone();
    archives.observe(Arc::new(move |progress: Value| {
        let events = upload_events.clone();
        Box::pin(async move {
            let stages: Vec<String> =
                serde_json::from_value(progress["stages"].clone()).unwrap_or_default();
            let state = progress["status"].as_str().unwrap_or("uploading");
            let message = if state == "retrying" {
                "Upload delayed. Local files retained while uploads retry."
            } else if state == "saved" {
                "Stage outputs saved."
            } else {
                "Saving stage outputs in the background."
            };
            let mut detail = progress;
            detail.as_object_mut().unwrap().remove("stages");
            for stage in stages {
                let _ = events
                    .send("upload", message, detail.clone(), Some(&stage), None)
                    .await;
            }
            Ok(())
        })
    }));
    let pipeline_error = Arc::new(Mutex::new(None::<(String, bool)>));
    let result=async {
        files::private_dir(&paths.scratch)?;
        files::private_dir(&output.join(".worker"))?;
        if let Some(state)=&assignment.resume_state{files::write_private(&output.join("platform-state.json"),&serde_json::to_vec(state)?)?;}
        let needs_input=assignment.resume_state.as_ref().and_then(|state|state["completed"].get("selection")).is_none();
        let input=job.join("input.mp4");
        if needs_input {
            events.send("stage","Preparing video",json!({"status":"running"}),Some("download"),None).await?;
            if assignment.input.kind=="stored" {
                let sha=assignment.input.sha256.as_deref().context("Missing input checksum")?;let size=assignment.input.size.context("Missing input size")?;
                if archive::download(client,&assignment.input.url,&input,size,sha,&cancel).await.is_err(){if cancel.is_cancelled(){bail!("Download interrupted");}let refreshed:Value=client.post("refresh",&body(&assignment.fence,json!({"input":true,"artifactIds":[]})),&cancel).await?;archive::download(client,refreshed["input"]["url"].as_str().context("Missing refreshed input URL")?,&input,size,sha,&cancel).await?;}
            }else if assignment.input.kind=="source" {
                let args=vec!["--download".into(),assignment.input.url.clone(),"--public-source".into(),"--destination".into(),input.to_string_lossy().into_owned()];
                run_process(paths,&args,&job.join("download.log"),&cancel,|event:Value| {
                    let events=&events;let error=&pipeline_error;
                    async move {
                        if event["type"]=="failed" {
                            let message=redact(event["error"].as_str().unwrap_or("Source download failed"));
                            *error.lock().await=Some((message.clone(),event["interrupted"]==true));
                            events.send("progress",&message,json!({"status":"failed"}),Some("download"),None).await?;
                        } else if event["type"]=="progress" {
                            let mut progress=event.as_object().cloned().unwrap_or_default();
                            for key in ["type","stage","message","timestamp"] {progress.remove(key);}
                            events.send("progress",event["message"].as_str().unwrap_or("Downloading source video"),Value::Object(progress),Some("download"),None).await?;
                        }
                        Ok(())
                    }
                }).await?;
                archive::archive_file(client,&assignment.fence,archive::UploadFile{path:&input,relative:"input/source.mp4",role:"input",checkpoint:None,client_id:&uuid::Uuid::new_v4().to_string()},&cancel).await?;
            }else {bail!("Unknown input kind");}
            events.send("stage","Video ready",json!({"status":"completed"}),Some("download"),None).await?;
        }
        let mut request=assignment.request.clone();let request_object=request.as_object_mut().context("Invalid request")?;
        request_object.extend(json!({"scanId":assignment.fence.scan_id,"attemptId":assignment.fence.attempt_id,"inputPath":input,"outputPath":output,"resume":assignment.resume_state.is_some(),"selectiveResume":assignment.resume_state.is_some(),"inputSha256":assignment.input.sha256,"archiveAck":true,"backgroundArchive":true}).as_object().unwrap().clone());
        if let Some(versions)=&assignment.runtime_versions{request_object.insert("runtimeVersions".into(),versions.clone());}
        let request_path=output.join(".worker/request.json");files::write_private(&request_path,&serde_json::to_vec(&request)?)?;
        let checkpoint_sequence=AtomicU64::new(0);let restored=Mutex::new(BTreeSet::new());
        run_process(paths,&["--request".into(),request_path.to_string_lossy().into_owned()],&job.join("worker.log"),&cancel,|event:Value| {
            let restored=&restored;let archives=&archives;let events=&events;let output=&output;let assignment=&assignment;let job=&job;let sequence=&checkpoint_sequence;let error=&pipeline_error;let cancel=&cancel;
            async move {
                let kind=event["type"].as_str().unwrap_or("progress");let stage=event["stage"].as_str();
                if kind=="restore" {
                    let id=event["restoreId"].as_str().filter(|id|valid_id(id)).context("Invalid restore identity")?;let paths:Vec<String>=serde_json::from_value(event["paths"].clone())?;
                    events.send("progress","Restoring required stage inputs",json!({"substep":"Downloading saved inputs"}),stage,None).await?;
                    archive::restore(client,&assignment.fence,&assignment.resume,output,&paths,&mut *restored.lock().await,cancel).await?;
                    files::write_private(&output.join(".worker/restored").join(id),b"verified")?;return Ok(());
                }
                if kind=="checkpoint" {
                    files::private_dir(&output.join("logs"))?;files::write_log_tail(&job.join("worker.log"),&output.join("logs/worker.log"))?;
                    let id=event["checkpointId"].as_str().context("Missing checkpoint identity")?;
                    archives.enqueue(id,sequence.fetch_add(1,Ordering::SeqCst)+1).await?;return Ok(());
                }
                if kind=="failed"{*error.lock().await=Some((redact(event["error"].as_str().unwrap_or("Pipeline failed")),event["interrupted"]==true));}
                if kind=="completed" {return Ok(());}
                let mut progress=event.as_object().cloned().unwrap_or_default();for key in ["type","runtimeVersions","message"] {progress.remove(key);}
                events.send(kind,event["message"].as_str().or(event["error"].as_str()).unwrap_or(kind),Value::Object(progress),stage,event.get("runtimeVersions")).await
            }
        }).await?;
        events.send("stage","Waiting for required uploads",json!({"status":"running"}),Some("finalizing"),None).await?;
        archives.drain().await?;
        let _:Value=client.post("finish",&body(&assignment.fence,json!({"state":"completed"})),&cancel).await?;
        files::remove(&paths.scratch,&assignment.fence.scan_id)?;Ok(())
    }.await;
    if let Err(error) = &result {
        if !cancel.is_cancelled() && error.downcast_ref::<ApiError>().is_some_and(|s| s.0 == 409) {
            if let Ok(response) = client
                .post::<Heartbeat>(
                    "heartbeat",
                    &serde_json::to_value(&assignment.fence)?,
                    &cancel,
                )
                .await
            {
                if response.command == "stop" {
                    cancelled.store(
                        response.reason.as_deref() == Some("cancelled"),
                        Ordering::SeqCst,
                    );
                    cancel.cancel();
                }
            }
        }
        archive_cancel.cancel();
        let cleanup_cancel = CancellationToken::new();
        let mut cleanup_pending = false;
        if cancelled.load(Ordering::SeqCst) {
            cleanup_pending = files::remove(&paths.scratch, &assignment.fence.scan_id).is_err();
            if !cleanup_pending {
                let response: Result<Value> = client
                    .post(
                        "cleanup",
                        &serde_json::to_value(&assignment.fence)?,
                        &cleanup_cancel,
                    )
                    .await;
                cleanup_pending = response.is_err();
            }
        }
        if !cancel.is_cancelled() {
            for name in ["download.log", "worker.log"] {
                let saved = output.join("logs").join(name);
                if files::write_log_tail(&job.join(name), &saved).is_ok() {
                    if let Err(upload_error) = archive::archive_file(
                        client,
                        &assignment.fence,
                        archive::UploadFile {
                            path: &saved,
                            relative: &format!("logs/{name}"),
                            role: "log",
                            checkpoint: None,
                            client_id: &uuid::Uuid::new_v4().to_string(),
                        },
                        &cancel,
                    )
                    .await
                    {
                        eprintln!(
                            "Could not upload {name}; local log retained at {}: {}",
                            job.join(name).display(),
                            redact(&upload_error.to_string())
                        );
                    }
                }
            }
        }
        let pipeline_error = pipeline_error.lock().await;
        let interrupted = cancel.is_cancelled() || pipeline_error.as_ref().is_some_and(|e| e.1);
        let message = if cleanup_pending {
            "Cancelled; local cleanup pending.".to_owned()
        } else if cancelled.load(Ordering::SeqCst) {
            "Cancelled by owner.".to_owned()
        } else {
            pipeline_error
                .as_ref()
                .map(|e| e.0.clone())
                .unwrap_or_else(|| redact(&error.to_string()))
        };
        let _ = tokio::time::timeout(
            Duration::from_secs(10),
            client.post::<Value>(
                "finish",
                &body(
                    &assignment.fence,
                    json!({"state":if interrupted{"interrupted"}else{"failed"},"error":message}),
                ),
                &cleanup_cancel,
            ),
        )
        .await;
    }
    cancel.cancel();
    drop(lease_tasks);
    result?;
    Ok(server_drain.load(Ordering::SeqCst))
}

pub async fn run_process<F, Fut>(
    paths: &ContextPaths,
    args: &[String],
    log_path: &Path,
    cancel: &CancellationToken,
    mut event: F,
) -> Result<()>
where
    F: FnMut(Value) -> Fut,
    Fut: std::future::Future<Output = Result<()>>,
{
    if cancel.is_cancelled() {
        bail!("Pipeline interrupted");
    }
    files::private_dir(log_path.parent().unwrap())?;
    let mut options = tokio::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    options.mode(0o600);
    let log = Arc::new(Mutex::new(options.open(log_path).await?));
    let mut command = paths.command();
    command
        .args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let mut child = command
        .spawn()
        .context("Could not start bundled pipeline")?;
    let pid = child.id().context("Pipeline missing process id")?;
    let stderr = child.stderr.take().unwrap();
    let stderr_log = log.clone();
    let stderr_task = tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let mut log = stderr_log.lock().await;
            let _ = log.write_all(format!("{line}\n").as_bytes()).await;
        }
    });
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let processing = async {
        while let Some(line) = lines.next_line().await? {
            let mut log = log.lock().await;
            log.write_all(format!("{line}\n").as_bytes()).await?;
            log.flush().await?;
            drop(log);
            if line.len() <= 65536 {
                if let Ok(value) = serde_json::from_str::<Value>(&line) {
                    if value["type"].is_string() {
                        event(value).await?;
                    }
                }
            }
        }
        Ok::<(), anyhow::Error>(())
    };
    let mut result = tokio::select! {_=cancel.cancelled()=>Err(anyhow::anyhow!("Pipeline interrupted")),r=processing=>r};
    let exit = if result.is_ok() {
        tokio::select! {_=cancel.cancelled()=>{result=Err(anyhow::anyhow!("Pipeline interrupted"));None},exit=child.wait()=>Some(exit?)}
    } else {
        None
    };
    let exit = if let Some(exit) = exit {
        exit
    } else {
        terminate_tree(pid, false);
        let _ = tokio::time::timeout(Duration::from_secs(15), child.wait()).await;
        terminate_tree(pid, true);
        let _ = child.kill().await;
        child.wait().await?
    };
    let mut stderr_task = stderr_task;
    tokio::select! {_=cancel.cancelled()=>{terminate_tree(pid,true);stderr_task.abort();if result.is_ok(){result=Err(anyhow::anyhow!("Pipeline interrupted"));}},_= &mut stderr_task=>{}}
    log.lock().await.flush().await?;
    result?;
    if !exit.success() {
        let tail = crate::diagnostics::tail(log_path)?;
        let detail = tail.lines.iter().rev().find_map(|line| {
            let event: Value = serde_json::from_str(line).ok()?;
            (event["type"] == "failed")
                .then(|| event["error"].as_str().map(redact))
                .flatten()
        });
        bail!(
            "{} ({exit}). Open Logs in Guerrilla Worker. Retained log: {}",
            detail
                .as_deref()
                .unwrap_or("Pipeline exited unsuccessfully"),
            log_path.display()
        );
    }
    Ok(())
}
/// The standalone CLI shares the enrolled pipeline and its process-tree cleanup.
pub async fn scan(paths: &ContextPaths, args: &[String]) -> Result<()> {
    let mut command = paths.command();
    command.arg("scan").args(args);
    command
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::inherit());
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command
        .spawn()
        .context("Could not start bundled pipeline")?;
    let pid = child.id().context("Pipeline missing process id")?;
    let status = tokio::select! {
        result = child.wait() => result?,
        _ = tokio::signal::ctrl_c() => {
            terminate_tree(pid, false);
            match tokio::time::timeout(Duration::from_secs(15), child.wait()).await {
                Ok(result) => result?,
                Err(_) => {
                    terminate_tree(pid, true);
                    let _ = child.kill().await;
                    child.wait().await?
                }
            }
        }
    };
    if !status.success() {
        bail!("Scan did not complete; see the scan output and retained logs");
    }
    Ok(())
}

fn terminate_tree(pid: u32, force: bool) {
    #[cfg(unix)]
    unsafe {
        libc::kill(
            -(pid as i32),
            if force { libc::SIGKILL } else { libc::SIGTERM },
        );
    }
    #[cfg(windows)]
    {
        let mut command = std::process::Command::new("taskkill");
        command.args(["/PID", &pid.to_string(), "/T"]);
        if force {
            command.arg("/F");
        }
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
        let _ = command.output();
    }
}

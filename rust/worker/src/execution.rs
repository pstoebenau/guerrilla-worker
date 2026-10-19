use crate::{
    api::{body, Client},
    archive::{self, BackgroundArchive},
    daemon::Status,
    files,
    profile::ContextPaths,
    protocol::{redact, valid_id, Assignment, Fence},
};
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::Mutex,
};
use tokio_util::sync::CancellationToken;

struct ExecutionTasks {
    cancel: CancellationToken,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}
impl ExecutionTasks {
    async fn stop(&mut self) {
        self.cancel.cancel();
        for task in &self.tasks {
            task.abort();
        }
        for task in &mut self.tasks {
            let _ = task.await;
        }
    }
}
impl Drop for ExecutionTasks {
    fn drop(&mut self) {
        self.cancel.cancel();
        for task in &self.tasks {
            task.abort();
        }
    }
}
struct Events {
    fence: Fence,
    outbox: Arc<crate::outbox::Outbox>,
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
            json!({"eventId":uuid::Uuid::new_v4().to_string(),"kind":kind,"message":redact(message),"progress":progress}),
        );
        if let Some(stage) = stage {
            update["stage"] = stage.into();
        }
        if let Some(versions) = versions {
            update["runtimeVersions"] = versions.clone();
        }
        self.outbox.push(update).await?;
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
    mut assignment: Assignment,
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
    let cancel = shutdown.child_token();
    let cancelled = Arc::new(AtomicBool::new(false));
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
    files::private_dir(&output.join(".worker"))?;
    let fence_path = output.join(".worker/fence.json");
    let same_fence = std::fs::read(&fence_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|fence| fence == serde_json::to_value(&assignment.fence).unwrap());
    if same_fence {
        if let Ok(bytes) = std::fs::read(output.join("platform-state.json")) {
            assignment.resume_state = Some(serde_json::from_slice(&bytes)?);
        }
    }
    files::write_private(&fence_path, &serde_json::to_vec(&assignment.fence)?)?;
    let outbox = crate::outbox::Outbox::open(output.join(".worker/events.json"))?;
    if let Some(session) = &client.session {
        session
            .activate(
                assignment.fence.clone(),
                cancel.clone(),
                cancelled.clone(),
                local_drain.clone(),
            )
            .await;
    }
    let event_task = tokio::spawn(outbox.clone().run(client.clone(), cancel.clone()));
    let mut execution_tasks = ExecutionTasks {
        cancel: cancel.clone(),
        tasks: vec![event_task],
    };
    let events = Arc::new(Events {
        fence: assignment.fence.clone(),
        outbox: outbox.clone(),
        status,
        state: paths.state.clone(),
        drain: local_drain.clone(),
        serial: Mutex::new(()),
    });
    let archive_cancel = cancel.child_token();
    let mut archives = BackgroundArchive::new(
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
    let finish_path = output.join(".worker/finish.json");
    let saved_finish: Option<Value> = match std::fs::read(&finish_path) {
        Ok(bytes) => Some(serde_json::from_slice(&bytes)?),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    let result=async {
        if saved_finish.is_some() { return Ok::<(), anyhow::Error>(()); }
        files::private_dir(&paths.scratch)?;
        files::private_dir(&output.join(".worker"))?;
        if let Some(state)=&assignment.resume_state{files::write_private(&output.join("platform-state.json"),&serde_json::to_vec(state)?)?;}
        let needs_input=assignment.resume_state.as_ref().and_then(|state|state["completed"].get("selection")).is_none();
        let input=job.join("input.mp4");
        let input_cached = assignment.input.size.zip(assignment.input.sha256.as_ref())
            .is_some_and(|(size, sha)| files::hash(&input).is_ok_and(|identity| identity == (size, sha.clone())));
        if needs_input && !input_cached {
            events.send("stage","Preparing video",json!({"status":"running"}),Some("download"),None).await?;
            if assignment.input.kind=="stored" {
                let sha=assignment.input.sha256.as_deref().context("Missing input checksum")?;let size=assignment.input.size.context("Missing input size")?;
                if archive::download(client,&assignment.input.url,&input,size,sha,&cancel).await.is_err(){if cancel.is_cancelled(){bail!("Download interrupted");}let refreshed:Value=client.post("refresh",&body(&assignment.fence,json!({"input":true,"artifactIds":[]})),&cancel).await?;archive::download(client,refreshed["input"]["url"].as_str().context("Missing refreshed input URL")?,&input,size,sha,&cancel).await?;}
            }else {bail!("Hosted scans require a video saved to private storage; the GPU worker does not fetch source links.");}
            events.send("stage","Video ready",json!({"status":"completed"}),Some("download"),None).await?;
        }
        let mut request=assignment.request.clone();let request_object=request.as_object_mut().context("Invalid request")?;
        request_object.extend(json!({"scanId":assignment.fence.scan_id,"attemptId":assignment.fence.attempt_id,"inputPath":input,"outputPath":output,"resume":assignment.resume_state.is_some(),"selectiveResume":assignment.resume_state.is_some(),"inputSha256":assignment.input.sha256,"archiveAck":true,"backgroundArchive":true}).as_object().unwrap().clone());
        if let Some(versions)=&assignment.runtime_versions{request_object.insert("runtimeVersions".into(),versions.clone());}
        let request_path=output.join(".worker/request.json");files::write_private(&request_path,&serde_json::to_vec(&request)?)?;
        let sequence_path = output.join(".worker/checkpoint-sequence");
        let sequence = std::fs::read_to_string(&sequence_path).ok().and_then(|s| s.parse().ok()).unwrap_or(0);
        let checkpoint_sequence=AtomicU64::new(sequence);let restored=Mutex::new(BTreeSet::new());
        run_process(paths,&["--request".into(),request_path.to_string_lossy().into_owned()],&job.join("worker.log"),&cancel,|event:Value| {
            let restored=&restored;let archives=&archives;let events=&events;let output=&output;let assignment=&assignment;let job=&job;let sequence=&checkpoint_sequence;let error=&pipeline_error;let cancel=&cancel;let sequence_path=&sequence_path;
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
                    let next=sequence.fetch_add(1,Ordering::SeqCst)+1;files::write_private(sequence_path,next.to_string().as_bytes())?;archives.enqueue(id,next).await?;return Ok(());
                }
                if kind=="failed"{*error.lock().await=Some((redact(event["error"].as_str().unwrap_or("Pipeline failed")),event["interrupted"]==true));}
                if kind=="completed" {return Ok(());}
                let mut progress=event.as_object().cloned().unwrap_or_default();for key in ["type","runtimeVersions","message"] {progress.remove(key);}
                events.send(kind,event["message"].as_str().or(event["error"].as_str()).unwrap_or(kind),Value::Object(progress),stage,event.get("runtimeVersions")).await
            }
        }).await?;
        Ok(())
    }.await;
    let pipeline_error = pipeline_error.lock().await;
    let mut terminal = saved_finish.unwrap_or_else(|| {
        if let Err(error) = &result {
            let interrupted = shutdown.is_cancelled() || pipeline_error.as_ref().is_some_and(|e| e.1);
            let message = pipeline_error.as_ref().map(|e| e.0.clone()).unwrap_or_else(|| redact(&error.to_string()));
            body(&assignment.fence, json!({"state": if interrupted { "interrupted" } else { "failed" }, "error": message}))
        } else { body(&assignment.fence, json!({"state":"completed"})) }
    });
    drop(pipeline_error);
    // Persist completion before waiting for uploads. A daemon restart can finish
    // publishing without repeating successful GPU computation.
    if !cancel.is_cancelled() || shutdown.is_cancelled() {
        files::write_private(&finish_path, &serde_json::to_vec(&terminal)?)?;
    }
    let publication: Result<()> = async {
        if cancelled.load(Ordering::SeqCst) {
            archives.stop().await;
            execution_tasks.stop().await;
            files::remove(&paths.scratch, &assignment.fence.scan_id)?;
            let _: Value = client
                .post(
                    "cleanup",
                    &serde_json::to_value(&assignment.fence)?,
                    shutdown,
                )
                .await?;
            return Ok(());
        }
        if cancel.is_cancelled() {
            bail!("Assignment interrupted; recovery files retained");
        }
        if terminal["state"] == "completed" {
            events
                .send(
                    "stage",
                    "Waiting for required uploads",
                    json!({"status":"running"}),
                    Some("finalizing"),
                    None,
                )
                .await?;
        }
        if let Err(error) = archives.drain().await {
            if cancel.is_cancelled() {
                return Err(error);
            }
            terminal = body(
                &assignment.fence,
                json!({"state":"failed","error":redact(&error.to_string())}),
            );
            files::write_private(&finish_path, &serde_json::to_vec(&terminal)?)?;
        }
        outbox.flush(&cancel).await?;
        let _: Value = client.post("finish", &terminal, &cancel).await?;
        if terminal["state"] == "completed" {
            files::remove(&paths.scratch, &assignment.fence.scan_id)?;
        } else {
            std::fs::remove_file(&finish_path)?;
        }
        Ok(())
    }
    .await;

    let publication = if publication.is_err()
        && client.session.as_ref().is_some_and(|session| {
            session.confirmed(&assignment.fence, terminal["state"].as_str().unwrap_or(""))
        }) {
        archives.stop().await;
        execution_tasks.stop().await;
        if terminal["state"] == "completed" {
            files::remove(&paths.scratch, &assignment.fence.scan_id)
        } else {
            std::fs::remove_file(&finish_path).map_err(Into::into)
        }
    } else {
        publication
    };
    cancel.cancel();
    drop(execution_tasks);
    if let Some(session) = &client.session {
        session.clear().await;
    }
    publication?;
    result?;
    Ok(false)
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

/// Resolve durable terminal intents even when a committed remote attempt is no
/// longer returned by poll after a daemon restart.
pub async fn recover_finished(
    paths: &ContextPaths,
    client: &Client,
    cancel: &CancellationToken,
) -> Result<()> {
    for scan in std::fs::read_dir(&paths.scratch)? {
        let scan = scan?;
        let Some(scan_id) = scan.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !valid_id(&scan_id) || !scan.file_type()?.is_dir() {
            continue;
        }
        for attempt in std::fs::read_dir(files::checked(&paths.scratch, &scan_id)?)? {
            let attempt = attempt?;
            let Some(attempt_id) = attempt.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            if !valid_id(&attempt_id) || !attempt.file_type()?.is_dir() {
                continue;
            }
            let relative = format!("{scan_id}/{attempt_id}/run/.worker/finish.json");
            let finish_path = files::checked(&paths.scratch, &relative)?;
            let bytes = match std::fs::read(&finish_path) {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            let terminal: Value = serde_json::from_slice(&bytes)?;
            let fence: Fence = serde_json::from_value(terminal.clone())?;
            if fence.scan_id != scan_id
                || fence.attempt_id != attempt_id
                || !valid_id(&fence.lease_id)
            {
                bail!("Invalid retained terminal fence");
            }
            let response: Value = client
                .post("reconcile", &serde_json::to_value(&fence)?, cancel)
                .await?;
            if response["command"] != "stop" {
                continue;
            }
            if response["reason"] == "cancelled" {
                files::remove(&paths.scratch, &scan_id)?;
                let _: Value = client
                    .post("cleanup", &serde_json::to_value(&fence)?, cancel)
                    .await?;
                break;
            }
            if response["state"] == "completed" && terminal["state"] == "completed" {
                files::remove(&paths.scratch, &scan_id)?;
                break;
            }
            if response["state"] == terminal["state"] && response["state"].is_string() {
                std::fs::remove_file(&finish_path)?;
            } else if response["reason"] == "superseded" {
                // Keep unsuccessful/stale local work for diagnostics without
                // replaying its terminal intent on every future connection.
                std::fs::rename(
                    &finish_path,
                    finish_path.with_file_name("finish-superseded.json"),
                )?;
            }
        }
    }
    Ok(())
}

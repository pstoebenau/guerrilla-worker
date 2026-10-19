use crate::{
    api::{body, transfer_url, Client},
    files,
    protocol::{valid_id, Artifact, Fence, Intent},
};
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};
use tokio_util::{io::ReaderStream, sync::CancellationToken};

#[derive(Debug)]
struct TransferTransport;
impl std::fmt::Display for TransferTransport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Scoped transfer transport unavailable")
    }
}
impl std::error::Error for TransferTransport {}
pub async fn download(
    client: &Client,
    url: &str,
    destination: &Path,
    size: u64,
    sha256: &str,
    cancel: &CancellationToken,
) -> Result<()> {
    loop {
        match download_once(client, url, destination, size, sha256, cancel).await {
            Err(error)
                if !cancel.is_cancelled()
                    && client.session.is_some()
                    && error.downcast_ref::<TransferTransport>().is_some() =>
            {
                client.session.as_ref().unwrap().disconnect();
                client.wait_connected(cancel).await?;
            }
            result => return result,
        }
    }
}
async fn download_once(
    client: &Client,
    url: &str,
    destination: &Path,
    size: u64,
    sha256: &str,
    cancel: &CancellationToken,
) -> Result<()> {
    if sha256.len() != 64
        || !sha256
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        bail!("Invalid expected download identity");
    }
    client.wait_connected(cancel).await?;
    let response = tokio::select! {_=cancel.cancelled()=>bail!("Download interrupted"),r=client.http.get(transfer_url(url)?).timeout(Duration::from_secs(600)).send()=>r.map_err(|_|anyhow::Error::new(TransferTransport))?};
    if !response.status().is_success() {
        if response.status().is_server_error() {
            return Err(TransferTransport.into());
        }
        return Err(crate::api::ApiError(response.status().as_u16()).into());
    }
    files::private_dir(
        destination
            .parent()
            .context("Invalid download destination")?,
    )?;
    let temporary = destination.with_extension(format!("{}.part", uuid::Uuid::new_v4()));
    let result=async {
        let mut options=tokio::fs::OpenOptions::new();options.create_new(true).write(true);
        #[cfg(unix)] options.mode(0o600);
        let mut file=options.open(&temporary).await?; let mut response=response;let mut bytes=0u64;let mut hash=Sha256::new();
        loop {let chunk=tokio::select!{_=cancel.cancelled()=>bail!("Download interrupted"),r=response.chunk()=>r.map_err(|_|anyhow::Error::new(TransferTransport))?};let Some(chunk)=chunk else {break};bytes+=chunk.len() as u64;if bytes>size{bail!("Download exceeded manifest size");}hash.update(&chunk);file.write_all(&chunk).await?;}
        if bytes!=size||hex::encode(hash.finalize())!=sha256{bail!("Download checksum mismatch");}
        file.sync_all().await?;drop(file);
        #[cfg(windows)] files::replace_windows(&temporary,destination)?;
        #[cfg(not(windows))] tokio::fs::rename(&temporary,destination).await?;Ok(())
    }.await;
    let _ = tokio::fs::remove_file(&temporary).await;
    result
}
async fn put(
    client: &Client,
    url: &str,
    path: &Path,
    start: u64,
    length: u64,
    cancel: &CancellationToken,
) -> Result<Option<String>> {
    client.wait_connected(cancel).await?;
    let mut file = tokio::fs::File::open(path).await?;
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let request = client
        .http
        .put(transfer_url(url)?)
        .timeout(Duration::from_secs(600))
        .header("content-length", length)
        .body(reqwest::Body::wrap_stream(ReaderStream::new(
            file.take(length),
        )));
    let response = tokio::select! {_=cancel.cancelled()=>bail!("Upload interrupted"),r=request.send()=>r.map_err(|_|{if let Some(session) = &client.session { session.disconnect(); } anyhow::anyhow!("Upload transport failed")})?};
    if !response.status().is_success() {
        return Err(crate::api::ApiError(response.status().as_u16()).into());
    }
    Ok(response
        .headers()
        .get("etag")
        .and_then(|s| s.to_str().ok())
        .map(str::to_owned))
}
pub struct UploadFile<'a> {
    pub path: &'a Path,
    pub relative: &'a str,
    pub role: &'a str,
    pub checkpoint: Option<&'a str>,
    pub client_id: &'a str,
}
pub async fn archive_file(
    client: &Client,
    fence: &Fence,
    file: UploadFile<'_>,
    cancel: &CancellationToken,
) -> Result<String> {
    let UploadFile {
        path,
        relative,
        role,
        checkpoint,
        client_id,
    } = file;
    if !files::safe_relative(relative) {
        bail!("Invalid artifact path");
    }
    let (size, sha256) = files::hash(path)?;
    let before = std::fs::metadata(path)?;
    let mut allocation = body(
        fence,
        json!({"action":"allocate","clientId":client_id,"relativePath":relative,"role":role,"size":size,"sha256":sha256,"reuseUploaded":true}),
    );
    if let Some(id) = checkpoint {
        allocation["checkpointId"] = id.into();
    }
    let mut intent: Intent = client.post("artifacts", &allocation, cancel).await?;
    if intent.method == "reuse" {
        return Ok(intent.artifact_id);
    }
    let mut parts = vec![];
    if intent.method == "multipart" {
        let part_size = intent
            .part_size
            .filter(|n| *n > 0)
            .context("Missing multipart size")?;
        let count = size.div_ceil(part_size);
        if count > 10000 {
            bail!("Too many multipart parts");
        }
        for number in 1..=count {
            let mut url = part_url(client, fence, &intent.artifact_id, number, cancel).await?;
            let start = (number - 1) * part_size;
            let length = part_size.min(size - start);
            let result = put(client, &url, path, start, length, cancel).await;
            let etag = match result {
                Err(e)
                    if e.downcast_ref::<crate::api::ApiError>()
                        .is_some_and(|s| s.0 == 403) =>
                {
                    url = part_url(client, fence, &intent.artifact_id, number, cancel).await?;
                    put(client, &url, path, start, length, cancel).await?
                }
                other => other?,
            }
            .context("Multipart response missing ETag")?;
            parts.push(json!({"partNumber":number,"etag":etag}));
        }
    } else if intent.method == "put" {
        let url = intent.url.as_deref().context("Missing upload URL")?;
        if put(client, url, path, 0, size, cancel).await.is_err() {
            if cancel.is_cancelled() {
                bail!("Upload interrupted");
            }
            intent = client.post("artifacts", &allocation, cancel).await?;
            if intent.method == "put" {
                put(
                    client,
                    intent
                        .url
                        .as_deref()
                        .context("Missing refreshed upload URL")?,
                    path,
                    0,
                    size,
                    cancel,
                )
                .await?;
            } else if !matches!(intent.method.as_str(), "reuse" | "verify") {
                bail!("Upload intent changed unexpectedly");
            }
        }
    } else if intent.method != "verify" {
        bail!("Unsupported upload intent");
    }
    let after = std::fs::metadata(path)?;
    if before.len() != after.len()
        || before.modified()? != after.modified()?
        || files::hash(path)? != (size, sha256)
    {
        bail!("Artifact changed during upload");
    }
    if intent.method != "reuse" {
        let mut completion = body(fence, json!({"artifactId":intent.artifact_id}));
        if !parts.is_empty() {
            completion["parts"] = parts.into();
        }
        let _: Value = client.post("complete", &completion, cancel).await?;
    }
    Ok(intent.artifact_id)
}
async fn part_url(
    client: &Client,
    fence: &Fence,
    id: &str,
    number: u64,
    cancel: &CancellationToken,
) -> Result<String> {
    let result: Value = client
        .post(
            "artifacts",
            &body(
                fence,
                json!({"action":"parts","artifactId":id,"partNumbers":[number]}),
            ),
            cancel,
        )
        .await?;
    result["parts"]
        .as_array()
        .and_then(|p| p.iter().find(|p| p["partNumber"] == number))
        .and_then(|p| p["url"].as_str())
        .map(str::to_owned)
        .context("Missing multipart URL")
}
fn declared(state: &Value) -> Result<BTreeSet<String>> {
    let mut names = BTreeSet::from(["platform-state.json".to_owned()]);
    for group in ["stageCheckpoints", "completed"] {
        if let Some(stages) = state[group].as_object() {
            for stage in stages.values() {
                if let Some(files) = stage["files"].as_array() {
                    for file in files {
                        let path = file["path"]
                            .as_str()
                            .context("Invalid declared checkpoint file")?;
                        if !files::safe_relative(path) {
                            bail!("Invalid checkpoint file path");
                        }
                        names.insert(path.into());
                    }
                }
            }
        }
    }
    Ok(names)
}
fn exclusions(state: &Value) -> Result<Vec<String>> {
    let paths = state["retention"]["excludePaths"].as_array();
    let mut result = vec![];
    if let Some(paths) = paths {
        for path in paths {
            let path = path.as_str().context("Invalid retention exclusion")?;
            if !files::safe_relative(path) {
                bail!("Invalid retention exclusion");
            }
            result.push(path.into());
        }
    } else if !state["retention"]["excludePaths"].is_null() {
        bail!("Invalid retention exclusions");
    }
    Ok(result)
}
fn excluded(path: &str, paths: &[String]) -> bool {
    paths
        .iter()
        .any(|prefix| path == prefix || path.starts_with(&format!("{prefix}/")))
}
pub fn freeze(source: &Path, destination: &Path, inherited: &[Artifact]) -> Result<()> {
    freeze_cached(source, destination, inherited, &mut BTreeMap::new())
}
type FrozenFiles = BTreeMap<(u64, String), PathBuf>;
fn freeze_cached(
    source: &Path,
    destination: &Path,
    inherited: &[Artifact],
    cache: &mut FrozenFiles,
) -> Result<()> {
    let state: Value = serde_json::from_slice(&std::fs::read(source.join("platform-state.json"))?)?;
    cache.retain(|_, path| path.is_file());
    let mut identities = BTreeMap::new();
    for group in ["completed", "stageCheckpoints"] {
        if let Some(stages) = state[group].as_object() {
            for stage in stages.values() {
                for file in stage["files"].as_array().into_iter().flatten() {
                    if let (Some(path), Some(size), Some(sha)) = (
                        file["path"].as_str(),
                        file["size"].as_u64(),
                        file["sha256"].as_str(),
                    ) {
                        identities.insert(path.to_owned(), (size, sha.to_owned()));
                    }
                }
            }
        }
    }
    let mut names = declared(&state)?;
    if source.join("logs/worker.log").exists() {
        names.insert("logs/worker.log".into());
    }
    let exclusions = exclusions(&state)?;
    files::private_dir(destination)?;
    for path in names {
        if excluded(&path, &exclusions) {
            continue;
        }
        let from = files::checked(source, &path)?;
        if !from.exists() && inherited.iter().any(|file| file.relative_path == path) {
            continue;
        }
        let meta = std::fs::symlink_metadata(&from)?;
        if !meta.is_file() || meta.file_type().is_symlink() {
            bail!("Missing regular declared stage output");
        }
        let to = files::checked(destination, &path)?;
        files::private_dir(to.parent().unwrap())?;
        if to.exists() {
            cache.retain(|_, previous| previous != &to);
            std::fs::remove_file(&to)?;
        }
        // Only link immutable frozen copies, never live engine files. This
        // shares snapshot bytes even on filesystems without copy-on-write.
        let identity = identities.get(&path);
        let linked = identity
            .and_then(|key| cache.get(key))
            .is_some_and(|previous| std::fs::hard_link(previous, &to).is_ok());
        if !linked {
            reflink_copy::reflink_or_copy(from, &to)?;
        }
        if let Some(identity) = identity {
            cache.insert(identity.clone(), to);
        }
    }
    Ok(())
}
// A completed reconstruction is no longer needed to resume after densification.
// Preserve already published manifests exactly; only skip products which have
// never been committed. Its small stage log still records successful execution.
fn prepare_snapshot(
    snapshot: &Snapshot,
    durable: &BTreeMap<String, Value>,
    inherited: &[Artifact],
) -> Result<Value> {
    let mut state: Value =
        serde_json::from_slice(&std::fs::read(snapshot.root.join("platform-state.json"))?)?;
    if let Some(stages) = state["completed"].as_object_mut() {
        let dense = stages.contains_key("densification");
        for (name, stage) in stages.iter_mut() {
            if let Some(saved) = durable.get(name) {
                *stage = saved.clone();
                continue;
            }
            let inherited_stage = stage["files"].as_array().is_some_and(|files| {
                !files.is_empty()
                    && files.iter().all(|f| {
                        inherited
                            .iter()
                            .any(|a| Some(a.relative_path.as_str()) == f["path"].as_str())
                    })
            });
            if name == "reconstruction" && dense && !inherited_stage {
                let log = format!(
                    "{}/stage.log",
                    stage["directory"]
                        .as_str()
                        .context("Missing stage directory")?
                );
                if let Some(files) = stage["files"].as_array_mut() {
                    if files.iter().any(|file| file["path"] == log) {
                        files.retain(|file| file["path"] == log);
                    }
                }
            }
        }
    }
    files::write_private(
        &snapshot.root.join("platform-state.json"),
        &serde_json::to_vec(&state)?,
    )?;
    Ok(state)
}
enum CheckpointResult {
    Saved,
    Superseded,
}
async fn superseded(queue: &Queue, snapshot: &Snapshot) -> bool {
    queue
        .pending
        .lock()
        .await
        .as_ref()
        .is_some_and(|next| next.sequence > snapshot.sequence)
}
async fn checkpoint(
    client: &Client,
    fence: &Fence,
    snapshot: &Snapshot,
    inherited: &[Artifact],
    cache: &mut BTreeMap<String, (String, Option<String>)>,
    cancel: &CancellationToken,
    queue: &Queue,
    state: &Value,
    commit_started: &mut bool,
) -> Result<CheckpointResult> {
    let commit_path = snapshot.root.join("commit.json");
    if commit_path.exists() {
        let commit: Value = serde_json::from_slice(&std::fs::read(&commit_path)?)?;
        *commit_started = true;
        let _: Value = client.post("checkpoint", &commit, cancel).await?;
        return Ok(CheckpointResult::Saved);
    }
    let required = declared(&state)?;
    let exclusions = exclusions(&state)?;
    let mut retained: BTreeMap<_, _> = inherited
        .iter()
        .filter(|f| {
            f.role != "checkpoint"
                && f.relative_path != "platform-state.json"
                && required.contains(&f.relative_path)
                && !excluded(&f.relative_path, &exclusions)
        })
        .map(|f| (f.relative_path.clone(), f))
        .collect();
    let mut paths = required;
    if snapshot.root.join("logs/worker.log").exists() {
        paths.insert("logs/worker.log".into());
    }
    let total = paths
        .iter()
        .filter(|p| !excluded(p, &exclusions))
        .filter_map(|p| std::fs::metadata(snapshot.root.join(p)).ok())
        .map(|m| m.len())
        .sum();
    let mut current = 0;
    report(queue, "uploading", snapshot, current, total).await;
    let mut ids = vec![];
    for relative in paths {
        if !*commit_started && superseded(queue, snapshot).await {
            return Ok(CheckpointResult::Superseded);
        }
        if excluded(&relative, &exclusions) {
            continue;
        }
        let path = files::checked(&snapshot.root, &relative)?;
        if !path.exists() {
            if retained.contains_key(&relative) {
                continue;
            }
            bail!("Missing checkpoint file");
        }
        let size = std::fs::metadata(&path)?.len();
        if let Some(previous) = retained.get(&relative) {
            if files::hash(&path)? == (previous.size, previous.sha256.clone()) {
                current += size;
                report(queue, "uploading", snapshot, current, total).await;
                continue;
            }
        }
        retained.remove(&relative);
        let entry = cache
            .entry(relative.clone())
            .or_insert_with(|| (uuid::Uuid::new_v4().to_string(), None));
        if entry.1.is_none() {
            let role = if relative == "platform-state.json" {
                "checkpoint"
            } else if relative.ends_with(".sog") || relative.ends_with(".spz") {
                "export"
            } else if relative.ends_with(".log") || relative.ends_with(".jsonl") {
                "log"
            } else {
                "intermediate"
            };
            entry.1 = Some(
                archive_file(
                    client,
                    fence,
                    UploadFile {
                        path: &path,
                        relative: &relative,
                        role,
                        checkpoint: Some(&snapshot.id),
                        client_id: &entry.0,
                    },
                    cancel,
                )
                .await?,
            );
        }
        ids.push(entry.1.clone().unwrap());
        current += size;
        report(queue, "uploading", snapshot, current, total).await;
    }
    if !*commit_started && superseded(queue, snapshot).await {
        return Ok(CheckpointResult::Superseded);
    }
    report(queue, "completing", snapshot, total, total).await;
    let inherited_ids: Vec<_> = retained.values().map(|f| &f.artifact_id).collect();
    let mut commit = body(
        fence,
        json!({"checkpointId":snapshot.id,"sequence":snapshot.sequence,"artifactIds":ids,"state":state}),
    );
    if !inherited_ids.is_empty() {
        commit["inheritedArtifactIds"] = json!(inherited_ids);
    }
    // Once commit is attempted its outcome may be ambiguous on transport error.
    // Retry the identical commit before moving on to a replacement snapshot.
    files::write_private(&commit_path, &serde_json::to_vec(&commit)?)?;
    *commit_started = true;
    let _: Value = client.post("checkpoint", &commit, cancel).await?;
    Ok(CheckpointResult::Saved)
}
#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct Snapshot {
    id: String,
    sequence: u64,
    root: PathBuf,
    stages: Vec<String>,
}
fn recover_snapshots(output: &Path) -> Result<VecDeque<Snapshot>> {
    let archives = output.join(".worker/archives");
    let entries = match std::fs::read_dir(&archives) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(VecDeque::new()),
        Err(error) => return Err(error.into()),
    };
    let mut snapshots = vec![];
    for entry in entries {
        let entry = entry?;
        let descriptor = entry.path().join("snapshot.json");
        if !descriptor.exists() {
            continue;
        }
        let mut snapshot: Snapshot = serde_json::from_slice(&std::fs::read(descriptor)?)?;
        if !valid_id(&snapshot.id) || entry.file_name().to_str() != Some(&snapshot.id) {
            bail!("Invalid retained checkpoint identity");
        }
        snapshot.root = files::checked(output, &format!(".worker/archives/{}", snapshot.id))?;
        snapshots.push(snapshot);
    }
    snapshots.sort_by_key(|s| s.sequence);
    Ok(snapshots.into())
}
pub type Observer = Arc<
    dyn Fn(Value) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<()>> + Send>>
        + Send
        + Sync,
>;
async fn report(queue: &Queue, status: &str, snapshot: &Snapshot, current: u64, total: u64) {
    let (current, total) = {
        let mut progress = queue.progress.lock().unwrap();
        match status {
            "saved" => {
                let (_, total) = progress.remove(&snapshot.id).unwrap_or((current, total));
                (total, total)
            }
            "retrying" => progress
                .get(&snapshot.id)
                .copied()
                .unwrap_or((current, total)),
            _ => {
                progress.insert(snapshot.id.clone(), (current, total));
                (current, total)
            }
        }
    };
    let observer = queue.observer.lock().unwrap().clone();
    if let Some(observer) = observer {
        let _=observer(json!({"status":status,"checkpointId":snapshot.id,"checkpointSequence":snapshot.sequence,"stages":snapshot.stages,"current":current,"total":total,"unit":"bytes"})).await;
    }
}
struct Queue {
    pending: tokio::sync::Mutex<Option<Snapshot>>,
    recovered: tokio::sync::Mutex<VecDeque<Snapshot>>,
    busy: AtomicBool,
    notify: tokio::sync::Notify,
    error: tokio::sync::Mutex<Option<String>>,
    observer: std::sync::Mutex<Option<Observer>>,
    progress: std::sync::Mutex<BTreeMap<String, (u64, u64)>>,
}
pub struct BackgroundArchive {
    queue: Arc<Queue>,
    output: PathBuf,
    cancel: CancellationToken,
    task: tokio::task::JoinHandle<()>,
    inherited: Vec<Artifact>,
    frozen: std::sync::Mutex<FrozenFiles>,
}
impl BackgroundArchive {
    pub fn new(
        client: Client,
        fence: Fence,
        output: PathBuf,
        inherited: Vec<Artifact>,
        cancel: CancellationToken,
    ) -> Self {
        let recovery = recover_snapshots(&output);
        let (recovered, error) = match recovery {
            Ok(snapshots) => (snapshots, None),
            Err(error) => (VecDeque::new(), Some(error.to_string())),
        };
        let queue = Arc::new(Queue {
            pending: tokio::sync::Mutex::new(None),
            recovered: tokio::sync::Mutex::new(recovered),
            busy: AtomicBool::new(false),
            notify: tokio::sync::Notify::new(),
            error: tokio::sync::Mutex::new(error),
            observer: std::sync::Mutex::new(None),
            progress: std::sync::Mutex::new(BTreeMap::new()),
        });
        let q = queue.clone();
        let out = output.clone();
        let stop = cancel.clone();
        let restored = inherited.clone();
        let task = tokio::spawn(async move {
            let mut durable = BTreeMap::new();
            loop {
                let ready = q.notify.notified();
                let snapshot = {
                    let mut pending = q.pending.lock().await;
                    let next = q
                        .recovered
                        .lock()
                        .await
                        .pop_front()
                        .or_else(|| pending.take());
                    if next.is_some() {
                        q.busy.store(true, Ordering::SeqCst);
                    }
                    next
                };
                let Some(snapshot) = snapshot else {
                    tokio::select! {_=stop.cancelled()=>break,_=ready=>continue}
                };
                let state = match prepare_snapshot(&snapshot, &durable, &restored) {
                    Ok(state) => state,
                    Err(error) => {
                        *q.error.lock().await = Some(error.to_string());
                        q.busy.store(false, Ordering::SeqCst);
                        q.notify.notify_waiters();
                        break;
                    }
                };
                let mut cache = BTreeMap::new();
                let mut retries = 0;
                let mut commit_started = snapshot.root.join("commit.json").exists();
                loop {
                    match checkpoint(
                        &client,
                        &fence,
                        &snapshot,
                        &restored,
                        &mut cache,
                        &stop,
                        &q,
                        &state,
                        &mut commit_started,
                    )
                    .await
                    {
                        Ok(CheckpointResult::Saved) => {
                            if let Some(stages) = state["completed"].as_object() {
                                durable.extend(
                                    stages
                                        .iter()
                                        .map(|(name, stage)| (name.clone(), stage.clone())),
                                );
                            }
                            let result = files::write_private(
                                &out.join(".archive-acks").join(&snapshot.id),
                                b"committed",
                            )
                            .and_then(|_| {
                                files::remove(&out.join(".worker/archives"), &snapshot.id)
                            });
                            if let Err(error) = result {
                                *q.error.lock().await = Some(error.to_string());
                            } else {
                                report(&q, "saved", &snapshot, 0, 0).await;
                            }
                            break;
                        }
                        Ok(CheckpointResult::Superseded) => {
                            q.progress.lock().unwrap().remove(&snapshot.id);
                            if let Err(error) =
                                files::remove(&out.join(".worker/archives"), &snapshot.id)
                            {
                                *q.error.lock().await = Some(error.to_string());
                            }
                            break;
                        }
                        Err(error) => {
                            if stop.is_cancelled() {
                                break;
                            }
                            if error
                                .downcast_ref::<crate::api::ApiError>()
                                .is_some_and(|s| s.0 < 500 && s.0 != 429)
                            {
                                *q.error.lock().await = Some(error.to_string());
                                break;
                            }
                            report(&q, "retrying", &snapshot, 0, 0).await;
                            eprintln!(
                                "Upload delayed; retained checkpoint {} will retry",
                                snapshot.id
                            );
                            let delay = Duration::from_secs((5u64 << retries.min(4)).min(60));
                            retries += 1;
                            if !commit_started && superseded(&q, &snapshot).await {
                                continue;
                            }
                            tokio::select! {_=stop.cancelled()=>break,_=q.notify.notified(), if !commit_started => {},_ = tokio::time::sleep(delay)=>{}}
                        }
                    }
                }
                q.busy.store(false, Ordering::SeqCst);
                q.notify.notify_waiters();
                if stop.is_cancelled() || q.error.lock().await.is_some() {
                    break;
                }
            }
        });
        Self {
            queue,
            output,
            cancel,
            task,
            inherited,
            frozen: std::sync::Mutex::new(BTreeMap::new()),
        }
    }
    pub async fn stop(&mut self) {
        self.cancel.cancel();
        self.task.abort();
        let _ = (&mut self.task).await;
    }
    pub fn observe(&self, observer: Observer) {
        *self.queue.observer.lock().unwrap() = Some(observer);
    }
    pub async fn enqueue(&self, id: &str, sequence: u64) -> Result<()> {
        if !valid_id(id) {
            bail!("Invalid checkpoint identity");
        }
        if self.cancel.is_cancelled() {
            bail!("Checkpoint interrupted");
        }
        let root = files::checked(&self.output, &format!(".worker/archives/{id}"))?;
        freeze_cached(
            &self.output,
            &root,
            &self.inherited,
            &mut self.frozen.lock().unwrap(),
        )?;
        let state: Value =
            serde_json::from_slice(&std::fs::read(root.join("platform-state.json"))?)?;
        let mut stages = BTreeSet::new();
        for group in ["completed", "stageCheckpoints"] {
            if let Some(items) = state[group].as_object() {
                stages.extend(
                    items
                        .keys()
                        .filter(|s| {
                            matches!(
                                s.as_str(),
                                "selection"
                                    | "reconstruction"
                                    | "densification"
                                    | "training"
                                    | "export"
                            )
                        })
                        .cloned(),
                );
            }
        }
        let snapshot = Snapshot {
            id: id.into(),
            sequence,
            root,
            stages: stages.into_iter().collect(),
        };
        files::write_private(
            &snapshot.root.join("snapshot.json"),
            &serde_json::to_vec(&snapshot)?,
        )?;
        report(&self.queue, "queued", &snapshot, 0, 0).await;
        let mut pending = self.queue.pending.lock().await;
        if let Some(previous) = pending.replace(snapshot) {
            self.queue.progress.lock().unwrap().remove(&previous.id);
            files::remove(&self.output.join(".worker/archives"), &previous.id)?;
        }
        files::write_private(
            &self
                .output
                .join(".archive-acks")
                .join(format!("{id}.ready")),
            b"snapshot ready",
        )?;
        drop(pending);
        self.queue.notify.notify_one();
        Ok(())
    }
    pub async fn drain(&self) -> Result<()> {
        loop {
            let notified = self.queue.notify.notified();
            if let Some(error) = self.queue.error.lock().await.clone() {
                bail!("Checkpoint failed: {error}");
            }
            if self.cancel.is_cancelled() {
                bail!("Upload interrupted; local files retained");
            }
            if self.queue.pending.lock().await.is_none()
                && self.queue.recovered.lock().await.is_empty()
                && !self.queue.busy.load(Ordering::SeqCst)
            {
                return Ok(());
            }
            tokio::select! {_=self.cancel.cancelled()=>bail!("Upload interrupted"),_=notified=>{}}
        }
    }
}
impl Drop for BackgroundArchive {
    fn drop(&mut self) {
        self.cancel.cancel();
        self.task.abort();
    }
}
pub async fn restore(
    client: &Client,
    fence: &Fence,
    manifest: &[Artifact],
    output: &Path,
    paths: &[String],
    restored: &mut BTreeSet<String>,
    cancel: &CancellationToken,
) -> Result<()> {
    for path in paths {
        if !files::safe_relative(path) {
            bail!("Invalid restore scope");
        }
    }
    for file in manifest {
        if restored.contains(&file.artifact_id)
            || !paths.iter().any(|p| {
                file.relative_path == *p || file.relative_path.starts_with(&format!("{p}/"))
            })
        {
            continue;
        }
        let destination = files::checked(output, &file.relative_path)?;
        if files::hash(&destination)
            .is_ok_and(|identity| identity == (file.size, file.sha256.clone()))
        {
            restored.insert(file.artifact_id.clone());
            continue;
        }
        if download(
            client,
            &file.url,
            &destination,
            file.size,
            &file.sha256,
            cancel,
        )
        .await
        .is_err()
        {
            if cancel.is_cancelled() {
                bail!("Restore interrupted");
            }
            let fresh: Value = client
                .post(
                    "refresh",
                    &body(fence, json!({"artifactIds":[file.artifact_id]})),
                    cancel,
                )
                .await?;
            let fresh: Vec<Artifact> = serde_json::from_value(fresh["artifacts"].clone())?;
            let item = fresh
                .iter()
                .find(|a| a.artifact_id == file.artifact_id)
                .context("Missing refreshed artifact")?;
            if item.relative_path != file.relative_path
                || item.size != file.size
                || item.sha256 != file.sha256
            {
                bail!("Resume artifact identity changed");
            }
            download(
                client,
                &item.url,
                &destination,
                file.size,
                &file.sha256,
                cancel,
            )
            .await?;
        }
        restored.insert(file.artifact_id.clone());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshots_share_frozen_content_without_linking_live_engine_files() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("run");
        files::private_dir(&source.join("selection-a")).unwrap();
        std::fs::write(source.join("selection-a/frame.jpg"), b"image").unwrap();
        let (size, sha) = files::hash(&source.join("selection-a/frame.jpg")).unwrap();
        let state = json!({"completed":{"selection":{"directory":"selection-a","files":[{"path":"selection-a/frame.jpg","size":size,"sha256":sha}]}}});
        std::fs::write(source.join("platform-state.json"), state.to_string()).unwrap();
        let a = temp.path().join("a");
        let b = temp.path().join("b");
        let mut cache = BTreeMap::new();
        freeze_cached(&source, &a, &[], &mut cache).unwrap();
        freeze_cached(&source, &b, &[], &mut cache).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(a.join("selection-a/frame.jpg"))
                    .unwrap()
                    .ino(),
                std::fs::metadata(b.join("selection-a/frame.jpg"))
                    .unwrap()
                    .ino()
            );
        }
        std::fs::write(source.join("selection-a/frame.jpg"), b"changed").unwrap();
        std::fs::remove_dir_all(a).unwrap();
        assert_eq!(
            std::fs::read(b.join("selection-a/frame.jpg")).unwrap(),
            b"image"
        );
        // Retrying a partially frozen destination must replace its files too.
        let (size, sha) = files::hash(&source.join("selection-a/frame.jpg")).unwrap();
        let mut updated = state;
        updated["completed"]["selection"]["files"][0]["size"] = json!(size);
        updated["completed"]["selection"]["files"][0]["sha256"] = json!(sha);
        std::fs::write(source.join("platform-state.json"), updated.to_string()).unwrap();
        freeze_cached(&source, &b, &[], &mut cache).unwrap();
        assert_eq!(
            std::fs::read(b.join("selection-a/frame.jpg")).unwrap(),
            b"changed"
        );
    }

    #[test]
    fn superseded_reconstruction_keeps_history_but_never_changes_a_saved_manifest() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().to_path_buf();
        let reconstruction = json!({"directory":"reconstruction-a","files":[
            {"path":"reconstruction-a/sparse/0/points3D.bin"}, {"path":"reconstruction-a/stage.log"}
        ]});
        let state = json!({"completed":{"reconstruction":reconstruction,"densification":{"directory":"densification-a","files":[]}}});
        let snapshot = Snapshot {
            id: "a".into(),
            sequence: 1,
            root: root.clone(),
            stages: vec![],
        };
        std::fs::write(root.join("platform-state.json"), state.to_string()).unwrap();
        let projected = prepare_snapshot(&snapshot, &BTreeMap::new(), &[]).unwrap();
        assert_eq!(
            projected["completed"]["reconstruction"]["files"],
            json!([{"path":"reconstruction-a/stage.log"}])
        );
        let durable = BTreeMap::from([("reconstruction".into(), reconstruction.clone())]);
        let preserved = prepare_snapshot(&snapshot, &durable, &[]).unwrap();
        assert_eq!(preserved["completed"]["reconstruction"], reconstruction);
        let inherited: Vec<_> = reconstruction["files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| Artifact {
                artifact_id: "saved".into(),
                relative_path: f["path"].as_str().unwrap().into(),
                role: "intermediate".into(),
                size: 0,
                sha256: String::new(),
                url: String::new(),
            })
            .collect();
        assert_eq!(
            prepare_snapshot(&snapshot, &BTreeMap::new(), &inherited).unwrap()["completed"]
                ["reconstruction"],
            reconstruction
        );
    }
}

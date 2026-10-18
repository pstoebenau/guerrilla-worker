use crate::{
    api::{body, transfer_url, Client},
    files,
    protocol::{valid_id, Artifact, Fence, Intent},
};
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};
use tokio_util::{io::ReaderStream, sync::CancellationToken};

pub async fn download(
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
    let response = tokio::select! {_=cancel.cancelled()=>bail!("Download interrupted"),r=client.http.get(transfer_url(url)?).timeout(Duration::from_secs(600)).send()=>r.map_err(|_|anyhow::anyhow!("Download transport failed"))?};
    if !response.status().is_success() {
        bail!("Download failed ({})", response.status().as_u16());
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
        loop {let chunk=tokio::select!{_=cancel.cancelled()=>bail!("Download interrupted"),r=response.chunk()=>r.map_err(|_|anyhow::anyhow!("Download stream failed"))?};let Some(chunk)=chunk else {break};bytes+=chunk.len() as u64;if bytes>size{bail!("Download exceeded manifest size");}hash.update(&chunk);file.write_all(&chunk).await?;}
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
    let response = tokio::select! {_=cancel.cancelled()=>bail!("Upload interrupted"),r=request.send()=>r.map_err(|_|anyhow::anyhow!("Upload transport failed"))?};
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
    let state: Value = serde_json::from_slice(&std::fs::read(source.join("platform-state.json"))?)?;
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
        std::fs::copy(from, to)?;
    }
    Ok(())
}
async fn checkpoint(
    client: &Client,
    fence: &Fence,
    snapshot: &Snapshot,
    inherited: &[Artifact],
    cache: &mut BTreeMap<String, (String, Option<String>)>,
    cancel: &CancellationToken,
    queue: &Queue,
) -> Result<()> {
    let state: Value =
        serde_json::from_slice(&std::fs::read(snapshot.root.join("platform-state.json"))?)?;
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
    report(queue, "completing", snapshot, total, total).await;
    let inherited_ids: Vec<_> = retained.values().map(|f| &f.artifact_id).collect();
    let mut commit = body(
        fence,
        json!({"checkpointId":snapshot.id,"sequence":snapshot.sequence,"artifactIds":ids,"state":state}),
    );
    if !inherited_ids.is_empty() {
        commit["inheritedArtifactIds"] = json!(inherited_ids);
    }
    let _: Value = client.post("checkpoint", &commit, cancel).await?;
    Ok(())
}
#[derive(Clone)]
struct Snapshot {
    id: String,
    sequence: u64,
    root: PathBuf,
    stages: Vec<String>,
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
}
impl BackgroundArchive {
    pub fn new(
        client: Client,
        fence: Fence,
        output: PathBuf,
        inherited: Vec<Artifact>,
        cancel: CancellationToken,
    ) -> Self {
        let queue = Arc::new(Queue {
            pending: tokio::sync::Mutex::new(None),
            busy: AtomicBool::new(false),
            notify: tokio::sync::Notify::new(),
            error: tokio::sync::Mutex::new(None),
            observer: std::sync::Mutex::new(None),
            progress: std::sync::Mutex::new(BTreeMap::new()),
        });
        let q = queue.clone();
        let out = output.clone();
        let stop = cancel.clone();
        let restored = inherited.clone();
        let task = tokio::spawn(async move {
            loop {
                let ready = q.notify.notified();
                let snapshot = {
                    let mut pending = q.pending.lock().await;
                    let next = pending.take();
                    if next.is_some() {
                        q.busy.store(true, Ordering::SeqCst);
                    }
                    next
                };
                let Some(snapshot) = snapshot else {
                    tokio::select! {_=stop.cancelled()=>break,_=ready=>continue}
                };
                let mut cache = BTreeMap::new();
                let mut retries = 0;
                loop {
                    match checkpoint(&client, &fence, &snapshot, &restored, &mut cache, &stop, &q)
                        .await
                    {
                        Ok(()) => {
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
                            tokio::select! {_=stop.cancelled()=>break,_=tokio::time::sleep(delay)=>{}}
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
        }
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
        freeze(&self.output, &root, &self.inherited)?;
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
            if self.queue.pending.lock().await.is_none() && !self.queue.busy.load(Ordering::SeqCst)
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

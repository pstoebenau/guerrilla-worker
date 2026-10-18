use crate::{
    artifacts::{
        self, copy_tree, excluded, gaussian_count, hash, manifest, relative, safe, save, verify,
        Artifact,
    },
    emit,
    engine::{training_config, Engine},
    process::{ManagedChild, Runtime},
};
use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Request {
    pub scan_id: String,
    pub attempt_id: String,
    pub input_path: PathBuf,
    pub output_path: PathBuf,
    pub backend: Engine,
    pub max_cap: u64,
    #[serde(default = "empty_object")]
    pub settings: Value,
    #[serde(default)]
    pub resume: bool,
    #[serde(default)]
    pub selective_resume: bool,
    #[serde(default)]
    pub input_sha256: Option<String>,
    #[serde(default)]
    pub runtime_versions: Option<Value>,
    #[serde(default)]
    pub archive_ack: bool,
    #[serde(default)]
    pub background_archive: bool,
    #[serde(default = "archive_timeout")]
    pub archive_timeout_seconds: u64,
}
fn empty_object() -> Value {
    json!({})
}
fn archive_timeout() -> u64 {
    3600
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Identity {
    backend: Engine,
    settings: Value,
    max_cap: u64,
    input_sha256: String,
    runtime_versions: Value,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Stage {
    pub directory: String,
    pub files: Vec<Artifact>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArchivedCheckpoint {
    pub path: String,
    pub sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub config_sha256: Option<String>,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Retention {
    pub version: u8,
    pub exclude_paths: Vec<String>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct State {
    pub version: u8,
    pub identity: Identity,
    pub completed: BTreeMap<String, Stage>,
    pub stages: BTreeMap<String, String>,
    #[serde(default)]
    pub stage: String,
    #[serde(default)]
    pub checkpoints: Vec<ArchivedCheckpoint>,
    #[serde(default)]
    pub stage_checkpoints: BTreeMap<String, Stage>,
    #[serde(default)]
    pub retention: Retention,
    #[serde(default)]
    pub result: Value,
    #[serde(default)]
    pub status: String,
}
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RetentionPlan {
    pub version: u8,
    pub exclude_paths: Vec<String>,
    pub latest_checkpoint: Option<ArchivedCheckpoint>,
    pub final_ply: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retained_files: Option<Vec<String>>,
}

pub struct Runner {
    pub request: Request,
    pub state: State,
    pub output: PathBuf,
    source: PathBuf,
    options: Value,
    runtime: Runtime,
    stage_started: Instant,
    last_checkpoint: Instant,
    last_progress: Instant,
    progress_offset: u64,
    progress_fragment: String,
    progress_values: Value,
    progress_baseline: Option<(Instant, f64)>,
    pending: Vec<(String, RetentionPlan)>,
}
impl Runner {
    pub fn new(request: Request, runtime: Runtime, versions: Value) -> Result<Self> {
        ensure!(
            !request.scan_id.is_empty() && !request.attempt_id.is_empty(),
            "scanId and attemptId are required"
        );
        ensure!(
            request.max_cap > 0 && request.max_cap <= 30000000,
            "maxCap must be within 1..30000000"
        );
        let options = artifacts::validate_settings(
            request.backend.name(),
            &request.settings,
            request.max_cap,
        )?;
        ensure!(
            !request.input_path.is_symlink() && !request.output_path.is_symlink(),
            "Input/output may not be symlinks"
        );
        fs::create_dir_all(&request.output_path)?;
        let output = request.output_path.canonicalize()?;
        let source = if request.input_path.is_file() {
            request.input_path.canonicalize()?
        } else {
            ensure!(
                request.selective_resume
                    && request.resume
                    && request.input_sha256.as_ref().is_some_and(|s| s.len() == 64
                        && s.bytes()
                            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())),
                "Stored input is not a regular file"
            );
            request.input_path.clone()
        };
        ensure!(
            source != output && !source.starts_with(&output),
            "Output must not contain the stored input"
        );
        if let Some(recorded) = &request.runtime_versions {
            ensure!(
                *recorded == versions,
                "The recorded runtime is unavailable; resume cannot change versions"
            );
        }
        let identity = Identity {
            backend: request.backend,
            settings: request.settings.clone(),
            max_cap: request.max_cap,
            input_sha256: if request.selective_resume {
                request
                    .input_sha256
                    .clone()
                    .context("Selective resume requires inputSha256")?
            } else {
                hash(&source)?
            },
            runtime_versions: versions.clone(),
        };
        let state_path = output.join("platform-state.json");
        let state = if state_path.is_file() {
            ensure!(request.resume, "An existing job requires resume");
            let mut state: State = serde_json::from_slice(&fs::read(&state_path)?)?;
            ensure!(state.version == 1, "Unsupported attempt manifest version");
            let portable = request.selective_resume && state.checkpoints.is_empty();
            ensure!(
                state.identity.backend == identity.backend
                    && state.identity.settings == identity.settings
                    && state.identity.max_cap == identity.max_cap
                    && state.identity.input_sha256 == identity.input_sha256
                    && (portable || state.identity.runtime_versions == identity.runtime_versions),
                "Resume input, settings, cap or engine versions changed"
            );
            state.identity = identity;
            state
                .stage_checkpoints
                .retain(|stage, _| stage == "training");
            if !request.selective_resume {
                for stage in state.completed.values() {
                    verify(&output, &stage.files)?;
                }
                for checkpoint in &state.checkpoints {
                    verify_checkpoint(&output, request.backend, checkpoint)?;
                }
            }
            state
        } else {
            State {
                version: 1,
                identity,
                completed: BTreeMap::new(),
                stages: BTreeMap::new(),
                stage: String::new(),
                checkpoints: vec![],
                stage_checkpoints: BTreeMap::new(),
                retention: Retention::default(),
                result: Value::Null,
                status: String::new(),
            }
        };
        save(&state_path, &state)?;
        emit("runtime", json!({"runtimeVersions": versions}));
        Ok(Self {
            request,
            state,
            output,
            source,
            options,
            runtime,
            stage_started: Instant::now(),
            last_checkpoint: Instant::now() - Duration::from_secs(11),
            last_progress: Instant::now() - Duration::from_secs(2),
            progress_offset: 0,
            progress_fragment: String::new(),
            progress_values: json!({}),
            progress_baseline: None,
            pending: vec![],
        })
    }
    fn save(&self) -> Result<()> {
        save(&self.output.join("platform-state.json"), &self.state)
    }
    fn path(&self, relative: &str) -> Result<PathBuf> {
        safe(&self.output, relative)
    }
    fn wait_ack(&self, ack: &Path, message: &str) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(self.request.archive_timeout_seconds);
        while !ack.is_file() {
            self.runtime.check()?;
            ensure!(
                Instant::now() < deadline,
                "{message}; scratch has been retained"
            );
            thread::sleep(Duration::from_millis(100));
        }
        ensure!(!ack.is_symlink(), "Acknowledgement cannot be a symlink");
        Ok(())
    }
    fn restore(&self, paths: &[PathBuf]) -> Result<()> {
        if !self.request.selective_resume {
            return Ok(());
        }
        let names = paths
            .iter()
            .map(|path| relative(&self.output, path))
            .collect::<Result<Vec<_>>>()?;
        for name in &names {
            self.path(name)?;
        }
        let restore_id = uuid::Uuid::new_v4().simple().to_string();
        emit(
            "restore",
            json!({"stage":self.state.stage,"restoreId":restore_id,"paths":names}),
        );
        self.wait_ack(
            &self.output.join(".worker/restored").join(&restore_id),
            "Stage input restoration was not acknowledged",
        )?;
        for stage in self
            .state
            .completed
            .values()
            .chain(self.state.stage_checkpoints.values())
        {
            verify(
                &self.output,
                &stage
                    .files
                    .iter()
                    .filter(|file| {
                        names.iter().any(|name| {
                            file.path == *name || file.path.starts_with(&(name.clone() + "/"))
                        })
                    })
                    .cloned()
                    .collect::<Vec<_>>(),
            )?;
        }
        for checkpoint in &self.state.checkpoints {
            if names.iter().any(|name| {
                checkpoint.path == *name || checkpoint.path.starts_with(&(name.clone() + "/"))
            }) {
                verify_checkpoint(&self.output, self.request.backend, checkpoint)?;
            }
        }
        Ok(())
    }
    fn checkpoint(&mut self, stage: &str, plan: Option<RetentionPlan>) -> Result<()> {
        if !self.request.archive_ack {
            if let Some(plan) = plan {
                self.pending.clear();
                self.pending.push((String::new(), plan));
            }
            return Ok(());
        }
        let checkpoint_id = uuid::Uuid::new_v4().simple().to_string();
        let mut event = json!({"stage":stage,"checkpointId":checkpoint_id,"manifestPath":"platform-state.json"});
        if let Some(result) = self.state.result.as_object() {
            for (key, value) in result {
                event[key] = value.clone();
            }
        }
        emit("checkpoint", event);
        let suffix = if self.request.background_archive {
            ".ready"
        } else {
            ""
        };
        self.wait_ack(
            &self
                .output
                .join(".archive-acks")
                .join(format!("{checkpoint_id}{suffix}")),
            "Artifact archival was not acknowledged",
        )?;
        if let Some(plan) = plan {
            // Newer plans enumerate all superseded local snapshots. A frozen
            // replacement can supersede pending bookkeeping, never the bytes:
            // pruning still waits for that replacement's committed archive.
            self.pending.clear();
            self.pending.push((checkpoint_id, plan));
        }
        Ok(())
    }
    fn stage<F>(&mut self, name: &str, resumable: bool, function: F) -> Result<PathBuf>
    where
        F: FnOnce(&mut Self, &Path) -> Result<()>,
    {
        if let Some(stage) = self.state.completed.get(name) {
            emit(
                "stage",
                json!({"stage":name,"status":"completed","resumed":true}),
            );
            return self.path(&stage.directory);
        }
        let resumable = resumable && name == "training" && !self.state.checkpoints.is_empty();
        let previous = self.state.stages.get(name).cloned();
        if !resumable {
            if let Some(previous) = &previous {
                let previous = self.path(previous)?;
                if previous.is_dir() {
                    fs::remove_dir_all(previous)?;
                }
            }
            self.state.stage_checkpoints.remove(name);
        }
        let folder = if let Some(previous) = previous.filter(|_| resumable) {
            self.path(&previous)?
        } else {
            self.output.join(format!(
                "{name}-{}",
                &uuid::Uuid::new_v4().simple().to_string()[..12]
            ))
        };
        if resumable && self.state.stage_checkpoints.contains_key(name) {
            self.restore(std::slice::from_ref(&folder))?;
        }
        self.state
            .stages
            .insert(name.into(), relative(&self.output, &folder)?);
        self.state.stage = name.into();
        self.save()?;
        self.stage_started = Instant::now();
        self.progress_values = json!({"substep":crate::progress::description(name)});
        self.progress_baseline = None;
        self.last_progress = Instant::now() - Duration::from_secs(2);
        emit(
            "stage",
            json!({"stage":name,"status":"running","elapsedSeconds":0,"substep":description(name)}),
        );
        function(self, &folder)?;
        let mut plan = None;
        if name == "training" {
            preserve_final(&folder, self.request.backend)?;
            self.poll_training(None)?;
            let retention = retention_plan(&self.output, &self.state)?;
            self.state.retention = Retention {
                version: 1,
                exclude_paths: retention.exclude_paths.clone(),
            };
            plan = Some(retention);
        }
        let mut files = manifest(&self.output, &folder)?
            .into_iter()
            .filter(|item| !excluded(&item.path, &self.state.retention.exclude_paths))
            .collect::<Vec<_>>();
        if name == "densification" {
            files.extend(manifest(
                &self.output,
                &folder.with_file_name(format!(
                    "{}-dataset",
                    folder.file_name().unwrap().to_string_lossy()
                )),
            )?);
        }
        ensure!(!files.is_empty(), "{name} produced no artifacts");
        self.state.completed.insert(
            name.into(),
            Stage {
                directory: relative(&self.output, &folder)?,
                files,
            },
        );
        self.state.stage_checkpoints.remove(name);
        self.save()?;
        emit(
            "stage",
            json!({"stage":name,"status":"completed","elapsedSeconds":self.stage_started.elapsed().as_secs_f64()}),
        );
        self.checkpoint(name, plan)?;
        self.prune_acknowledged(None)?;
        Ok(folder)
    }
    fn native(&mut self, mut request: Value, folder: &Path, python: Option<String>) -> Result<()> {
        let control = self
            .output
            .join(".worker/native")
            .join(format!("{}.json", uuid::Uuid::new_v4()));
        if request["action"] == "densification" {
            request["engineBin"] = json!(self.request.backend.api_bin()?);
        }
        if python.is_some() {
            let report=crate::process::run_capture(&[crate::python(),"-c".into(),"import sys,sysconfig,json;print(json.dumps({'path':sysconfig.get_path('purelib'),'version':list(sys.version_info[:2])}))".into()],Duration::from_secs(30))?;
            let runtime: Value = serde_json::from_str(report.trim())?;
            request["pythonSitePackages"] = runtime["path"].clone();
            request["pythonVersion"] = runtime["version"].clone();
        }
        save(&control, &request)?;
        let args = vec![
            python.unwrap_or_else(crate::python),
            "-u".into(),
            crate::pipeline_root()
                .join("native_bindings.py")
                .to_string_lossy()
                .to_string(),
            "--request".into(),
            control.to_string_lossy().to_string(),
        ];
        let log = self.output.join(format!(
            "{}.log",
            folder.file_name().unwrap().to_string_lossy()
        ));
        let result = self.command(&args, &log, None);
        let _ = fs::remove_file(control);
        result?;
        let text = fs::read_to_string(log)?;
        let report: Value = serde_json::from_str(
            text.lines()
                .rev()
                .find(|line| line.starts_with("{\"version\":"))
                .context("Python binding omitted its typed result")?,
        )?;
        ensure!(
            report["version"] == 1 && report["ok"] == true,
            "Python binding rejected its operation: {}",
            report["error"]
        );
        Ok(())
    }
    fn command(&mut self, args: &[String], log: &Path, timeout: Option<Duration>) -> Result<()> {
        self.runtime.check()?;
        if log.is_file() {
            fs::rename(
                log,
                log.with_extension(format!("previous-{}.log", uuid::Uuid::new_v4().simple())),
            )?;
        }
        self.progress_fragment.clear();
        self.progress_offset = 0;
        emit(
            "log",
            json!({"stage":self.state.stage,"message":format!("Starting {}",args[0])}),
        );
        let mut process = ManagedChild::spawn(args, log)?;
        let started = Instant::now();
        loop {
            self.runtime.check()?;
            if let Some(limit) = timeout {
                ensure!(
                    started.elapsed() < limit,
                    "Command timed out; see {}",
                    log.display()
                );
            }
            self.progress(log, false)?;
            if let Some(status) = process.child.try_wait()? {
                self.progress(log, true)?;
                ensure!(
                    status.success(),
                    "Command exited {status}; see {}\n{}",
                    log.display(),
                    tail(log, 4000)?
                );
                break;
            }
            if self.last_checkpoint.elapsed() >= Duration::from_secs(10) {
                self.last_checkpoint = Instant::now();
                if self.state.stage == "training" {
                    self.poll_training(Some(&mut process))?;
                }
            }
            self.prune_acknowledged(Some(&mut process))?;
            thread::sleep(Duration::from_millis(250));
        }
        Ok(())
    }
    fn progress(&mut self, log: &Path, force: bool) -> Result<()> {
        if !force && self.last_progress.elapsed() < Duration::from_secs(1) {
            return Ok(());
        }
        self.last_progress = Instant::now();
        if log.is_file() {
            let mut file = File::open(log)?;
            let size = file.metadata()?.len();
            if size < self.progress_offset {
                self.progress_offset = 0;
            }
            file.seek(SeekFrom::Start(
                self.progress_offset.max(size.saturating_sub(65536)),
            ))?;
            let mut bytes = vec![];
            file.read_to_end(&mut bytes)?;
            self.progress_offset = file.stream_position()?;
            let text = self.progress_fragment.clone() + &String::from_utf8_lossy(&bytes);
            let boundary = text.rfind(['\n', '\r']).map(|index| index + 1).unwrap_or(0);
            self.progress_fragment = if force {
                String::new()
            } else {
                text[boundary..]
                    .chars()
                    .rev()
                    .take(65536)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect()
            };
            let update = crate::progress::parse(
                &self.state.stage,
                if force { &text } else { &text[..boundary] },
            );
            if !update.as_object().unwrap().is_empty() {
                if update["substep"] != self.progress_values["substep"] {
                    self.progress_values = json!({});
                    self.progress_baseline = None;
                }
                for (key, value) in update.as_object().unwrap() {
                    self.progress_values[key] = value.clone();
                }
                self.progress_values["measuredAt"] = json!(std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)?
                    .as_secs_f64());
            }
        }
        let mut values = self.progress_values.clone();
        values["stage"] = json!(self.state.stage);
        values["elapsedSeconds"] = json!(self.stage_started.elapsed().as_secs_f64());
        if values["unit"] != "percent" {
            if let (Some(current), Some(total)) =
                (values["current"].as_f64(), values["total"].as_f64())
            {
                if self
                    .progress_baseline
                    .is_none_or(|(_, previous)| current < previous)
                {
                    self.progress_baseline = Some((Instant::now(), current));
                }
                if let Some((started, previous)) = self.progress_baseline {
                    let elapsed = started.elapsed().as_secs_f64();
                    let advanced = current - previous;
                    if elapsed >= 2. && advanced > 0. {
                        let rate = advanced / elapsed;
                        values["rate"] = json!(rate);
                        if values.get("remainingSeconds").is_none() {
                            values["remainingSeconds"] = json!((total - current) / rate);
                        }
                    }
                }
            }
        }
        emit("progress", values);
        Ok(())
    }
    fn poll_training(&mut self, mut process: Option<&mut ManagedChild>) -> Result<()> {
        let folder = self.path(
            self.state
                .stages
                .get("training")
                .context("Training path missing")?,
        )?;
        let candidates = self.request.backend.checkpoints(&folder)?;
        if candidates.is_empty() {
            return Ok(());
        }
        let frozen = process.as_mut().map(|child| child.freeze()).transpose()?;
        let open_paths = frozen
            .as_ref()
            .map(|f| f.open_paths())
            .transpose()?
            .unwrap_or_default();
        let old_step = self
            .state
            .checkpoints
            .last()
            .map(|item| {
                self.request
                    .backend
                    .checkpoint(self.path(&item.path).unwrap())
                    .step
            })
            .unwrap_or(-1);
        let mut snapshot = None;
        for native in candidates {
            if native.step < old_step
                || open_paths.iter().any(|path| {
                    *path == native.source
                        || path.starts_with(&native.path)
                        || native.config.as_ref().is_some_and(|p| p == path)
                })
            {
                continue;
            }
            if !self
                .request
                .backend
                .valid_checkpoint(&native, self.request.max_cap)?
            {
                continue;
            }
            let digest = hash(&native.source)?;
            if self
                .state
                .checkpoints
                .iter()
                .any(|old| old.sha256 == digest)
            {
                continue;
            }
            if let Some(config) = &native.config {
                if !config.is_file() {
                    continue;
                }
            }
            let directory = folder
                .join("retained-checkpoints")
                .join(uuid::Uuid::new_v4().simple().to_string());
            let path = directory.join(native.path.file_name().unwrap());
            copy_tree(&native.path, &path)?;
            let copied = self.request.backend.checkpoint(path.clone());
            let config_sha256 = if let Some(config) = &native.config {
                let destination = copied
                    .config
                    .as_ref()
                    .context("Checkpoint config destination missing")?;
                copy_tree(config, destination)?;
                Some(hash(destination)?)
            } else {
                None
            };
            ensure!(
                hash(&copied.source)? == digest
                    && self
                        .request
                        .backend
                        .valid_checkpoint(&copied, self.request.max_cap)?,
                "Frozen checkpoint copy failed verification"
            );
            let recorded = ArchivedCheckpoint {
                path: relative(&self.output, &path)?,
                sha256: digest,
                config_sha256,
            };
            self.state.checkpoints = vec![recorded];
            snapshot = Some(directory);
            break;
        }
        drop(frozen);
        if let Some(snapshot) = snapshot {
            self.training_preview()?;
            let plan = retention_plan(&self.output, &self.state)?;
            self.state.retention = Retention {
                version: 1,
                exclude_paths: plan.exclude_paths.clone(),
            };
            self.state.stage_checkpoints.insert(
                "training".into(),
                Stage {
                    directory: relative(&self.output, &folder)?,
                    files: {
                        let mut files = manifest(&self.output, &snapshot)?;
                        files.extend(manifest(&self.output, &folder.join("previews"))?);
                        files.retain(|file| !excluded(&file.path, &plan.exclude_paths));
                        files
                    },
                },
            );
            self.save()?;
            self.checkpoint("training", Some(plan))?;
        }
        Ok(())
    }
    fn auxiliary(&self, args: &[String], log: &Path, deadline: Instant) -> Result<()> {
        let mut child = ManagedChild::spawn(args, log)?;
        loop {
            self.runtime.check()?;
            ensure!(Instant::now() < deadline, "Checkpoint preview timed out");
            if let Some(status) = child.child.try_wait()? {
                ensure!(
                    status.success(),
                    "Checkpoint preview command failed; see {}",
                    log.display()
                );
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    fn training_preview(&self) -> Result<()> {
        let Some(latest) = self.state.checkpoints.last() else {
            return Ok(());
        };
        let folder = self.path(
            self.state
                .stages
                .get("training")
                .context("Training folder missing")?,
        )?;
        let checkpoint = self.request.backend.checkpoint(self.path(&latest.path)?);
        if !checkpoint.preview.is_file() {
            return Ok(());
        }
        let name = format!(
            "step-{:09}-{}",
            checkpoint.step.max(0),
            &latest.sha256[..12]
        );
        let preview = folder.join("previews").join(format!("{name}.sog"));
        if preview.is_file() {
            gaussian_count(&preview, self.request.max_cap)?;
            return Ok(());
        }
        let temporary = folder
            .join(".worker")
            .join(format!("preview-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&temporary)?;
        fs::create_dir_all(preview.parent().unwrap())?;
        let deadline = Instant::now() + Duration::from_secs(300);
        let log = preview.with_extension("log");
        let result = (|| {
            let source = if self.request.backend == Engine::Lichtfeld {
                let ply = temporary.join("checkpoint.ply");
                self.auxiliary(
                    &[
                        self.request.backend.path().to_string_lossy().to_string(),
                        "convert".into(),
                        checkpoint.preview.to_string_lossy().to_string(),
                        ply.to_string_lossy().to_string(),
                    ],
                    &log,
                    deadline,
                )?;
                ply
            } else {
                checkpoint.preview
            };
            let expected = gaussian_count(&source, self.request.max_cap)?;
            let candidate = temporary.join("scene.sog");
            let mut args = crate::converter()?;
            args.extend(["--gpu-backend".into(), crate::converter_backend()]);
            if let Ok(gpu) = std::env::var("SPLAT_TRANSFORM_GPU") {
                args.extend(["--gpu".into(), gpu]);
            }
            args.extend([
                source.to_string_lossy().to_string(),
                candidate.to_string_lossy().to_string(),
            ]);
            self.auxiliary(&args, &log, deadline)?;
            ensure!(
                gaussian_count(&candidate, self.request.max_cap)? == expected,
                "Preview Gaussian counts disagree"
            );
            fs::rename(candidate, &preview)?;
            let sidecar = source.with_extension("ppisp");
            if sidecar.is_file() {
                copy_tree(&sidecar, &preview.with_extension("ppisp"))?;
            }
            Ok(())
        })();
        let _ = fs::remove_dir_all(temporary);
        if let Err(error) = result {
            emit(
                "log",
                json!({"stage":"training","message":format!("Checkpoint preview could not be saved: {error}")}),
            );
        }
        Ok(())
    }
    fn prune_acknowledged(&mut self, mut process: Option<&mut ManagedChild>) -> Result<()> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let acknowledged = self
            .pending
            .iter()
            .filter(|(id, _)| {
                !self.request.archive_ack || self.output.join(".archive-acks").join(id).is_file()
            })
            .map(|(id, plan)| (id.clone(), plan.clone()))
            .collect::<Vec<_>>();
        if acknowledged.is_empty() {
            return Ok(());
        }
        let frozen = process.as_mut().map(|p| p.freeze()).transpose()?;
        let open_paths = frozen
            .as_ref()
            .map(|f| f.open_paths())
            .transpose()?
            .unwrap_or_default();
        let latest_step = self
            .state
            .checkpoints
            .last()
            .map(|c| {
                self.request
                    .backend
                    .checkpoint(self.path(&c.path).unwrap())
                    .step
            })
            .unwrap_or(-1);
        for (id, plan) in acknowledged {
            for relative in plan.exclude_paths {
                let path = self.path(&relative)?;
                if !path.exists()
                    || open_paths
                        .iter()
                        .any(|opened| opened == &path || opened.starts_with(&path))
                {
                    continue;
                }
                if process.is_some()
                    && path.extension().and_then(|s| s.to_str())
                        == Some(self.request.backend.suffix())
                    && !relative.contains("retained-checkpoints")
                    && self.request.backend.checkpoint(path.clone()).step > latest_step
                {
                    continue;
                }
                if path.is_dir() {
                    fs::remove_dir_all(path)?
                } else {
                    fs::remove_file(path)?
                }
            }
            self.pending.retain(|(pending, _)| pending != &id);
        }
        Ok(())
    }
    pub fn run(&mut self) -> Result<()> {
        let finished = self.state.completed.get("export").cloned();
        let trained = self.state.completed.get("training").cloned();
        let ply = if finished.is_some() {
            None
        } else if let Some(trained) = trained {
            let folder = self.path(&trained.directory)?;
            self.restore(&[folder.join("final")])?;
            Some(self.request.backend.training_output(&folder)?.0)
        } else {
            Some(match self.request.backend {
                Engine::Spirula => self.spirula()?,
                Engine::Lichtfeld => self.lichtfeld()?,
            })
        };
        let expected = if let Some(finished) = finished {
            let sog = self.path(&finished.directory)?.join("result.sog");
            self.restore(std::slice::from_ref(&sog))?;
            gaussian_count(&sog, self.request.max_cap)?
        } else {
            gaussian_count(ply.as_ref().unwrap(), self.request.max_cap)?
        };
        let exported = self.stage("export", false, |runner, folder| {
            fs::create_dir_all(folder)?;
            let ply = ply.as_ref().context("Final PLY unavailable")?;
            let mut command = crate::converter()?;
            command.extend(["--gpu-backend".into(), crate::converter_backend()]);
            if let Ok(gpu) = std::env::var("SPLAT_TRANSFORM_GPU") {
                command.extend(["--gpu".into(), gpu]);
            }
            command.extend([
                ply.to_string_lossy().to_string(),
                folder.join("result.sog").to_string_lossy().to_string(),
            ]);
            runner.command(&command, &folder.join("sog.log"), None)?;
            ensure!(
                gaussian_count(&folder.join("result.sog"), runner.request.max_cap)? == expected,
                "Export Gaussian counts disagree"
            );
            let sidecar = ply.with_extension("ppisp");
            if sidecar.is_file() {
                copy_tree(&sidecar, &folder.join("result.ppisp"))?;
            }
            Ok(())
        })?;
        let count = gaussian_count(&exported.join("result.sog"), self.request.max_cap)?;
        ensure!(count == expected, "Export Gaussian counts disagree");
        self.state.result =
            json!({"sog":relative(&self.output,&exported.join("result.sog"))?,"gaussians":count});
        self.state.status = "completed".into();
        self.save()?;
        let files = manifest(&self.output, &self.output)?
            .into_iter()
            .filter(|item| !excluded(&item.path, &self.state.retention.exclude_paths))
            .collect::<Vec<_>>();
        save(
            &self.output.join("artifact-manifest.json"),
            &json!({"version":1,"files":files}),
        )?;
        self.checkpoint("finalizing", None)?;
        emit("completed", self.state.result.clone());
        Ok(())
    }
    fn spirula(&mut self) -> Result<PathBuf> {
        let executable = self.request.backend.path().to_string_lossy().to_string();
        let mut extraction = json!({"skip":10,"keep":5,"max_frames":300,"quality":95,"scale":1});
        if let Some(values) = self.options["extraction"].as_object() {
            for (key, value) in values {
                extraction[key] = value.clone();
            }
        }
        let selected = self.stage("selection", false, |runner, folder| {
            let mut args = vec![
                executable.clone(),
                "sam".into(),
                "extract".into(),
                runner.source.to_string_lossy().to_string(),
                "--out".into(),
                folder.to_string_lossy().to_string(),
            ];
            for (key, value) in extraction.as_object().unwrap() {
                args.extend([format!("--{}", key.replace('_', "-")), value.to_string()]);
            }
            runner.command(
                &args,
                &runner.output.join(format!(
                    "{}.log",
                    folder.file_name().unwrap().to_string_lossy()
                )),
                None,
            )?;
            let images = artifacts::files(folder)?
                .iter()
                .filter(|p| {
                    matches!(
                        p.extension()
                            .and_then(|s| s.to_str())
                            .map(str::to_lowercase)
                            .as_deref(),
                        Some("jpg" | "jpeg" | "png")
                    )
                })
                .count();
            ensure!(
                images >= 3,
                "Spirula extraction produced fewer than three images"
            );
            Ok(())
        })?;
        let dataset = self.stage("reconstruction", true, |runner, folder| {
            runner.restore(std::slice::from_ref(&selected))?;
            runner.command(
                &[
                    executable.clone(),
                    "sfm".into(),
                    "auto".into(),
                    selected.to_string_lossy().to_string(),
                    "-o".into(),
                    folder.to_string_lossy().to_string(),
                    "--progress-dir".into(),
                    folder.join("progress").to_string_lossy().to_string(),
                    "--no-masks".into(),
                ],
                &runner.output.join(format!(
                    "{}.log",
                    folder.file_name().unwrap().to_string_lossy()
                )),
                None,
            )?;
            let files = artifacts::files(folder)?;
            ensure!(
                files
                    .iter()
                    .any(|p| p.file_name().and_then(|s| s.to_str()) == Some("cameras.bin"))
                    && files
                        .iter()
                        .any(|p| p.file_name().and_then(|s| s.to_str()) == Some("points3D.bin")),
                "Spirula did not produce COLMAP reconstruction"
            );
            copy_tree(&selected, &folder.join("images"))?;
            Ok(())
        })?;
        let training = self.stage("training", true, |runner, folder| {
            runner.restore(&[dataset.join("images"), dataset.join("sparse")])?;
            fs::create_dir_all(folder)?;
            let checkpoint = runner
                .state
                .checkpoints
                .last()
                .map(|item| runner.path(&item.path))
                .transpose()?;
            if checkpoint.is_none() && folder.join("run").exists() {
                fs::remove_dir_all(folder.join("run"))?;
            }
            let mut args = vec![
                executable.clone(),
                "train".into(),
                "--data".into(),
                dataset.to_string_lossy().to_string(),
                "--data-format".into(),
                "colmap".into(),
                "--output-dir-prefix".into(),
                folder.to_string_lossy().to_string(),
                "--output-dir-name".into(),
                "run".into(),
                "--cap-max".into(),
                runner.request.max_cap.to_string(),
                "--num-iterations".into(),
                runner.options["training"]["iterations"]
                    .as_u64()
                    .unwrap_or(30000)
                    .to_string(),
                "--save-full-checkpoint".into(),
                "1".into(),
                "--save-only-latest-checkpoint".into(),
                "0".into(),
                "--disable-viewer".into(),
                "1".into(),
                "--keep-viewer-alive".into(),
                "0".into(),
            ];
            if let Some(path) = checkpoint {
                runner.restore(&[path.clone(), path.parent().unwrap().join("config.json")])?;
                args.extend(["--resume".into(), path.to_string_lossy().to_string()]);
            }
            runner.command(
                &args,
                &runner.output.join(format!(
                    "{}.log",
                    folder.file_name().unwrap().to_string_lossy()
                )),
                None,
            )
        })?;
        Ok(self.request.backend.training_output(&training)?.0)
    }
    fn lichtfeld(&mut self) -> Result<PathBuf> {
        let selected=self.stage("selection",false,|runner,folder|runner.native(json!({"action":"selection","source":runner.source,"output":folder,"options":runner.options["selection"]}),folder,None))?;
        let reconstruction=self.stage("reconstruction",true,|runner,folder|{runner.restore(std::slice::from_ref(&selected))?;runner.native(json!({"action":"reconstruction","images":selected,"output":folder,"options":runner.options["reconstruction"]}),folder,None)})?;
        let dense=self.stage("densification",true,|runner,folder|{
            let result_file=reconstruction.join("colmap-result.json");runner.restore(std::slice::from_ref(&result_file))?;
            let report:Value=serde_json::from_slice(&fs::read(result_file)?)?;let dataset=safe(&reconstruction,report["dataset_relative"].as_str().unwrap_or("dataset"))?;
            runner.restore(&[dataset.join("images"),dataset.join("sparse")])?;
            let staged=folder.with_file_name(format!("{}-dataset",folder.file_name().unwrap().to_string_lossy()));if !staged.exists(){copy_tree(&dataset,&staged)?;}
            relocate_config(folder,&staged)?;
            let python=if cfg!(windows){Some(runner.request.backend.path().parent().unwrap().join("python.exe").to_string_lossy().to_string())}else{None};
            let mut options=runner.options["densification"].clone();options["sampson_thresh"]=json!(5.0);options["min_parallax_deg"]=json!(0.5);options["min_track_length"]=json!(1);
            runner.native(json!({"action":"densification","dataset":staged,"output":folder,"maxCap":runner.request.max_cap,"options":options,"plugin":crate::densification_plugin()}),folder,python)
        })?;
        let dataset = dense.with_file_name(format!(
            "{}-dataset",
            dense.file_name().unwrap().to_string_lossy()
        ));
        let config = training_config(&self.options, self.request.max_cap);
        let training = self.stage("training", true, |runner, folder| {
            runner.restore(&[dataset.join("images"), dataset.join("sparse")])?;
            let checkpoint = runner
                .state
                .checkpoints
                .last()
                .map(|item| runner.path(&item.path))
                .transpose()?;
            let executable = runner.request.backend.path().to_string_lossy().to_string();
            let mut command = vec![
                executable,
                "--headless".into(),
                "--train".into(),
                "-d".into(),
                dataset.to_string_lossy().to_string(),
                "-o".into(),
                folder.to_string_lossy().to_string(),
                "--max-cap".into(),
                runner.request.max_cap.to_string(),
            ];
            if let Some(checkpoint) = checkpoint {
                runner.restore(std::slice::from_ref(&checkpoint))?;
                command.extend(["--resume".into(), checkpoint.to_string_lossy().to_string()]);
            } else {
                if folder.exists() {
                    fs::rename(
                        folder,
                        folder.with_file_name(format!(
                            "{}-failed-{}",
                            folder.file_name().unwrap().to_string_lossy(),
                            uuid::Uuid::new_v4().simple()
                        )),
                    )?;
                }
                fs::create_dir_all(folder)?;
                save(&folder.join("config.json"), &config)?;
                command.extend([
                    "--config".into(),
                    folder.join("config.json").to_string_lossy().to_string(),
                    "-r".into(),
                    "1".into(),
                    "--max-width".into(),
                    "0".into(),
                ]);
                for (key, flag) in [
                    ("gut", "--gut"),
                    ("use_bilateral_grid", "--bilateral-grid"),
                    ("mip_filter", "--enable-mip"),
                    ("use_ppisp", "--ppisp"),
                    ("enable_sparsity", "--enable-sparsity"),
                    ("undistort", "--undistort"),
                ] {
                    if config[key] == true {
                        command.push(flag.into());
                    }
                }
            }
            runner.command(&command, &folder.join("training.log"), None)?;
            let final_ply = runner.request.backend.training_output(folder)?.0;
            let expected = config["iterations"].as_u64().unwrap()
                + if config["enable_sparsity"] == true {
                    config["sparsify_steps"].as_u64().unwrap()
                } else {
                    0
                };
            ensure!(
                final_ply.file_name().unwrap().to_string_lossy() == format!("splat_{expected}.ply"),
                "LichtFeld did not finish configured training duration"
            );
            Ok(())
        })?;
        Ok(self.request.backend.training_output(&training)?.0)
    }
}

fn description(stage: &str) -> &str {
    crate::progress::description(stage)
}
fn tail(path: &Path, limit: u64) -> Result<String> {
    if !path.is_file() {
        return Ok(String::new());
    }
    let mut file = File::open(path)?;
    let length = file.metadata()?.len();
    file.seek(SeekFrom::Start(length.saturating_sub(limit)))?;
    let mut bytes = vec![];
    file.read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).to_string())
}
fn verify_checkpoint(root: &Path, engine: Engine, item: &ArchivedCheckpoint) -> Result<()> {
    let checkpoint = engine.checkpoint(safe(root, &item.path)?);
    ensure!(
        checkpoint.source.is_file() && hash(&checkpoint.source)? == item.sha256,
        "Archived native checkpoint changed"
    );
    if let Some(expected) = &item.config_sha256 {
        let config = checkpoint.config.context("Checkpoint config missing")?;
        ensure!(
            config.is_file() && hash(&config)? == *expected,
            "Archived checkpoint configuration changed"
        );
    }
    Ok(())
}
fn preserve_final(folder: &Path, engine: Engine) -> Result<()> {
    let (ply, config) = engine.training_output(folder)?;
    let final_dir = folder.join("final");
    if ply.starts_with(&final_dir) {
        return Ok(());
    }
    copy_tree(&ply, &final_dir.join(ply.file_name().unwrap()))?;
    if let Some(config) = config {
        if config.is_file() {
            copy_tree(&config, &final_dir.join("config.json"))?;
        }
    }
    Ok(())
}
fn relocate_config(folder: &Path, dataset: &Path) -> Result<()> {
    let path = folder.join("config.json");
    if !path.is_file() {
        return Ok(());
    }
    let mut config: Value = serde_json::from_slice(&fs::read(&path)?)?;
    for (key, target, suffix) in [
        (
            "scene_root",
            folder.to_path_buf(),
            folder.file_name().unwrap().to_string_lossy().to_string(),
        ),
        (
            "images_subdir",
            dataset.join("images"),
            format!("{}/images", dataset.file_name().unwrap().to_string_lossy()),
        ),
    ] {
        let previous = config[key].as_str().context("Densification path missing")?;
        ensure!(
            previous
                .replace('\\', "/")
                .trim_end_matches('/')
                .ends_with(&format!("/{suffix}")),
            "Densification resume has unexpected {key}"
        );
        config[key] = json!(target);
    }
    copy_tree(
        &path,
        &folder.join(format!(
            "config-before-relocation-{}.json",
            uuid::Uuid::new_v4()
        )),
    )?;
    save(&path, &config)
}

pub fn retention_plan(root: &Path, state: &State) -> Result<RetentionPlan> {
    let mut plan = RetentionPlan {
        version: 1,
        exclude_paths: vec![],
        latest_checkpoint: None,
        final_ply: None,
        retained_files: None,
    };
    let Some(training) = state.stages.get("training") else {
        return Ok(plan);
    };
    let folder = safe(root, training)?;
    let engine = state.identity.backend;
    let mut latest = state.checkpoints.last().cloned();
    if state.completed.contains_key("training") {
        for native in engine.checkpoints(&folder)? {
            if latest.as_ref().is_some_and(|old| {
                native.step <= engine.checkpoint(safe(root, &old.path).unwrap()).step
            }) {
                break;
            }
            if !engine.valid_checkpoint(&native, state.identity.max_cap)? {
                continue;
            }
            latest = Some(ArchivedCheckpoint {
                path: relative(root, &native.path)?,
                sha256: hash(&native.source)?,
                config_sha256: native.config.as_ref().map(|path| hash(path)).transpose()?,
            });
            break;
        }
    }
    if let Some(checkpoint) = &latest {
        verify_checkpoint(root, engine, checkpoint)?;
        let keep = safe(root, &checkpoint.path)?;
        let retained = folder.join("retained-checkpoints");
        if retained.is_dir() {
            for previous in fs::read_dir(&retained)? {
                let previous = previous?.path();
                ensure!(!previous.is_symlink(), "Retention snapshot is a symlink");
                if !keep.starts_with(&previous) {
                    plan.exclude_paths.push(relative(root, &previous)?);
                }
            }
        }
        for native in engine.checkpoints(&folder)? {
            if native.path != keep && native.step <= engine.checkpoint(keep.clone()).step {
                plan.exclude_paths.push(relative(root, &native.path)?);
            }
        }
        plan.latest_checkpoint = latest;
    }
    if let Ok((ply, _)) = engine.training_output(&folder) {
        let keep_checkpoint = plan
            .latest_checkpoint
            .as_ref()
            .map(|checkpoint| safe(root, &checkpoint.path))
            .transpose()?;
        let keep = ply;
        plan.final_ply = Some(relative(root, &keep)?);
        for previous in artifacts::files(&folder)? {
            if previous.extension().and_then(|s| s.to_str()) == Some("ply")
                && previous != keep
                && !keep_checkpoint
                    .as_ref()
                    .is_some_and(|keep| previous.starts_with(keep))
                && !previous
                    .components()
                    .any(|c| c.as_os_str() == "retained-checkpoints")
            {
                plan.exclude_paths.push(relative(root, &previous)?);
            }
        }
    }
    let previews = folder.join("previews");
    let keep_preview = artifacts::files(&previews)?
        .into_iter()
        .rev()
        .find(|path| {
            path.extension().and_then(|s| s.to_str()) == Some("sog")
                && gaussian_count(path, state.identity.max_cap).is_ok()
        })
        .and_then(|path| path.file_stem().and_then(|s| s.to_str()).map(str::to_owned));
    for previous in artifacts::files(&previews)? {
        if keep_preview
            .as_ref()
            .is_none_or(|keep| previous.file_stem().and_then(|s| s.to_str()) != Some(keep.as_str()))
        {
            plan.exclude_paths.push(relative(root, &previous)?);
        }
    }
    plan.exclude_paths.sort();
    plan.exclude_paths.dedup();
    let all = plan.exclude_paths.clone();
    plan.exclude_paths.retain(|path| {
        !all.iter()
            .any(|other| other != path && path.starts_with(&(other.clone() + "/")))
    });
    for name in &plan.exclude_paths {
        safe(root, name)?;
    }
    Ok(plan)
}

pub fn compact_state(root: &Path) -> Result<RetentionPlan> {
    let mut state: State = serde_json::from_slice(&fs::read(root.join("platform-state.json"))?)?;
    let plan = retention_plan(root, &state)?;
    for stage in state.completed.values() {
        let retained = stage
            .files
            .iter()
            .filter(|file| !excluded(&file.path, &plan.exclude_paths))
            .cloned()
            .collect::<Vec<_>>();
        verify(root, &retained)?;
    }
    if state.completed.contains_key("training") {
        preserve_final(
            &safe(
                root,
                state
                    .stages
                    .get("training")
                    .context("Training path missing")?,
            )?,
            state.identity.backend,
        )?;
    }
    if let Some(latest) = plan.latest_checkpoint {
        let source = safe(root, &latest.path)?;
        let folder = safe(root, state.stages.get("training").unwrap())?;
        if !source.starts_with(folder.join("retained-checkpoints")) {
            let target = folder
                .join("retained-checkpoints")
                .join(uuid::Uuid::new_v4().simple().to_string())
                .join(source.file_name().unwrap());
            copy_tree(&source, &target)?;
            let native = state.identity.backend.checkpoint(source);
            let copied = state.identity.backend.checkpoint(target.clone());
            if let Some(config) = native.config {
                copy_tree(
                    &config,
                    copied
                        .config
                        .as_ref()
                        .context("Checkpoint config missing")?,
                )?;
            }
            state.checkpoints = vec![ArchivedCheckpoint {
                path: relative(root, &target)?,
                ..latest
            }];
        } else {
            state.checkpoints = vec![latest];
        }
    }
    let mut plan = retention_plan(root, &state)?;
    state.checkpoints = plan.latest_checkpoint.clone().into_iter().collect();
    state.retention = Retention {
        version: 1,
        exclude_paths: plan.exclude_paths.clone(),
    };
    if let Some(training) = state.completed.get_mut("training") {
        training.files = manifest(root, &safe(root, &training.directory)?)?
            .into_iter()
            .filter(|file| !excluded(&file.path, &plan.exclude_paths))
            .collect();
    }
    save(&root.join("platform-state.json"), &state)?;
    let files = manifest(root, root)?
        .into_iter()
        .filter(|file| !excluded(&file.path, &plan.exclude_paths))
        .collect::<Vec<_>>();
    plan.retained_files = Some(files.iter().map(|file| file.path.clone()).collect());
    save(
        &root.join("artifact-manifest.json"),
        &json!({"version":1,"files":files}),
    )?;
    Ok(plan)
}

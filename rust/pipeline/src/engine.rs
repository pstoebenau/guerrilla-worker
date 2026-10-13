use crate::{
    artifacts::{self, hash},
    process::run_capture,
};
use anyhow::{ensure, Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Engine {
    Lichtfeld,
    Spirula,
}
impl Engine {
    pub fn name(self) -> &'static str {
        match self {
            Self::Lichtfeld => "lichtfeld",
            Self::Spirula => "spirula",
        }
    }
    pub fn path(self) -> PathBuf {
        let variable = match self {
            Self::Lichtfeld => "LICHTFELD_BIN",
            Self::Spirula => "SPIRULA_BIN",
        };
        if let Some(path) = std::env::var_os(variable) {
            return PathBuf::from(path);
        }
        #[cfg(windows)]
        if self == Self::Lichtfeld {
            let local = std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default())
                        .join("AppData/Local")
                });
            return local.join("Programs/LichtFeld Studio/bin/LichtFeld-Studio.exe");
        }
        let filename = match (self, cfg!(windows)) {
            (Self::Lichtfeld, true) => "LichtFeld-Studio.exe",
            (Self::Lichtfeld, false) => "LichtFeld-Studio",
            (Self::Spirula, true) => "spirula.exe",
            (Self::Spirula, false) => "spirula",
        };
        if let Some(path) = crate::which(filename) {
            return path;
        }
        PathBuf::from(match self {
            Self::Lichtfeld => "/opt/lichtfeld/bin/run_lichtfeld.sh",
            Self::Spirula => "/opt/spirula/spirula",
        })
    }
    pub fn api_bin(self) -> Result<PathBuf> {
        self.path()
            .canonicalize()?
            .parent()
            .map(Path::to_path_buf)
            .context("Installed engine executable has no parent directory")
    }
    pub fn suffix(self) -> &'static str {
        match self {
            Self::Lichtfeld => "resume",
            Self::Spirula => "ckpt",
        }
    }
    pub fn checkpoint(self, path: PathBuf) -> Checkpoint {
        let step = step(&path);
        match self {
            Self::Lichtfeld => Checkpoint {
                source: path.clone(),
                preview: path.clone(),
                path,
                config: None,
                step,
            },
            Self::Spirula => Checkpoint {
                source: path.join("state.tar"),
                preview: path.join("splat.ply"),
                config: path.parent().map(|p| p.join("config.json")),
                path,
                step,
            },
        }
    }
    pub fn checkpoints(self, folder: &Path) -> Result<Vec<Checkpoint>> {
        let mut candidates = Vec::new();
        for path in artifacts::files(folder)? {
            if path
                .components()
                .any(|c| c.as_os_str() == "retained-checkpoints")
            {
                continue;
            }
            match self {
                Self::Lichtfeld if path.extension().and_then(|s| s.to_str()) == Some("resume") => {
                    candidates.push(self.checkpoint(path))
                }
                Self::Spirula if path.file_name().and_then(|s| s.to_str()) == Some("state.tar") => {
                    candidates.push(self.checkpoint(path.parent().unwrap().to_path_buf()))
                }
                _ => (),
            }
        }
        candidates.sort_by_key(|item| {
            (
                item.step,
                item.source.metadata().ok().and_then(|m| m.modified().ok()),
            )
        });
        candidates.reverse();
        Ok(candidates)
    }
    pub fn valid_checkpoint(self, checkpoint: &Checkpoint, cap: u64) -> Result<bool> {
        if !checkpoint.source.is_file() || checkpoint.source.metadata()?.len() == 0 {
            return Ok(false);
        }
        if self == Self::Lichtfeld {
            return Ok(true);
        }
        let mut file = File::open(&checkpoint.source)?;
        let length = file.metadata()?.len();
        if length < 1024 || length % 512 != 0 {
            return Ok(false);
        }
        file.seek(SeekFrom::End(-1024))?;
        let mut footer = [0; 1024];
        file.read_exact(&mut footer)?;
        if footer != [0; 1024] {
            return Ok(false);
        }
        file.seek(SeekFrom::Start(0))?;
        let mut archive = tar::Archive::new(file);
        let mut full_resume = false;
        let mut world = false;
        for entry in archive.entries()? {
            let mut entry = match entry {
                Ok(entry) => entry,
                Err(_) => return Ok(false),
            };
            let path = entry.path()?.to_string_lossy().to_string();
            ensure!(
                entry.raw_file_position() + entry.size() <= length,
                "Checkpoint is truncated"
            );
            if path == "state.json" {
                if entry.size() > 1024 * 1024 {
                    return Ok(false);
                }
                let metadata: Value = match serde_json::from_reader(&mut entry) {
                    Ok(value) => value,
                    Err(_) => return Ok(false),
                };
                let count = metadata["cur_num_splats"]
                    .as_u64()
                    .context("Spirula checkpoint Gaussian count missing")?;
                ensure!(
                    count <= cap && count > 0,
                    "Spirula exceeded the Gaussian cap"
                );
                full_resume =
                    metadata["full_resume"] == true || metadata["full_resume"].as_u64() == Some(1);
            }
            if path == "world.means.npy" || path == "world.opacities.npy" {
                world = true;
            }
            std::io::copy(&mut entry, &mut std::io::sink())?;
        }
        Ok(full_resume && world && checkpoint.config.as_ref().is_some_and(|p| p.is_file()))
    }
    pub fn training_output(self, folder: &Path) -> Result<(PathBuf, Option<PathBuf>)> {
        let final_dir = folder.join("final");
        let mut candidates = artifacts::files(if final_dir.exists() {
            &final_dir
        } else {
            folder
        })?
        .into_iter()
        .filter(|path| {
            if path.extension().and_then(|s| s.to_str()) != Some("ply") {
                return false;
            }
            if final_dir.exists() {
                return true;
            }
            match self {
                Self::Lichtfeld => regex::Regex::new(r"^splat_\d+\.ply$")
                    .unwrap()
                    .is_match(&path.file_name().unwrap().to_string_lossy()),
                Self::Spirula => {
                    path.file_name().and_then(|s| s.to_str()) == Some("splat.ply")
                        && path.starts_with(folder.join("run"))
                        && path
                            .parent()
                            .and_then(|p| p.extension())
                            .and_then(|s| s.to_str())
                            == Some("ckpt")
                }
            }
        })
        .collect::<Vec<_>>();
        candidates.sort_by_key(|path| {
            (
                if self == Self::Spirula {
                    path.parent().map(step).unwrap_or(-1)
                } else {
                    step(path)
                },
                path.clone(),
            )
        });
        let ply = candidates.pop().context("Training produced no final PLY")?;
        let config = if self == Self::Spirula && !ply.starts_with(&final_dir) {
            ply.parent()
                .and_then(|p| p.parent())
                .map(|p| p.join("config.json"))
        } else {
            ply.parent().map(|p| p.join("config.json"))
        };
        Ok((ply, config))
    }
    pub fn versions(self) -> Result<Value> {
        let path = self.path();
        ensure!(
            path.is_file(),
            "{} executable is unavailable; install the engine and select its path",
            self.name()
        );
        let mut versions = json!({});
        match self {
            Self::Lichtfeld => {
                versions["lichtfeld"] = json!(run_capture(
                    &[path.to_string_lossy().to_string(), "--version".into()],
                    Duration::from_secs(30)
                )?
                .trim());
                versions["lichtfeldBinarySha256"] = json!(hash(&path)?);
            }
            Self::Spirula => {
                versions["spirula"] = json!("user-installed");
                versions["spirulaBinarySha256"] = json!(hash(&path)?);
            }
        }
        versions["pipelineProtocol"] = json!("rust-v1");
        versions["pipelineBinarySha256"] = json!(hash(&std::env::current_exe()?)?);
        let native = crate::pipeline_root().join("native_bindings.py");
        ensure!(
            native.is_file(),
            "Bundled Python integration helpers are unavailable"
        );
        let mut helpers = fs::read_dir(crate::pipeline_root())?
            .map(|item| item.map(|entry| entry.path()))
            .collect::<std::io::Result<Vec<_>>>()?
            .into_iter()
            .filter(|path| {
                path.extension().and_then(|s| s.to_str()) == Some("py") && path.is_file()
            })
            .collect::<Vec<_>>();
        helpers.sort();
        let mut source_identity = String::new();
        for helper in helpers {
            source_identity.push_str(helper.file_name().unwrap().to_string_lossy().as_ref());
            source_identity.push_str(&hash(&helper)?);
        }
        use sha2::{Digest, Sha256};
        versions["pythonBindingsSha256"] =
            json!(format!("{:x}", Sha256::digest(source_identity.as_bytes())));
        if let Some(worker) = std::env::current_exe()?
            .parent()
            .map(|parent| {
                parent.join(if cfg!(windows) {
                    "guerrilla-worker.exe"
                } else {
                    "guerrilla-worker"
                })
            })
            .filter(|path| path.is_file())
        {
            versions["workerBinarySha256"] = json!(hash(&worker)?);
        }
        versions["pythonRuntime"] = json!(run_capture(
            &[crate::python(), "--version".into()],
            Duration::from_secs(30)
        )?
        .trim());
        let package_probe="import importlib.metadata,json; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))";
        let packages = run_capture(
            &[crate::python(), "-c".into(), package_probe.into()],
            Duration::from_secs(30),
        )?;
        versions["pythonEnvironmentSha256"] =
            json!(format!("{:x}", Sha256::digest(packages.trim().as_bytes())));
        if let Some(runtime) = std::env::var_os("WORKER_RUNTIME_ROOT").map(PathBuf::from) {
            if runtime.join("manifest.json").is_file() {
                versions["bundledRuntimeManifestSha256"] =
                    json!(hash(&runtime.join("manifest.json"))?);
            }
        }
        if self == Self::Lichtfeld {
            let plugin = crate::densification_plugin();
            let mut sources = walkdir::WalkDir::new(&plugin)
                .into_iter()
                .filter_entry(|entry| {
                    !matches!(entry.file_name().to_str(), Some(".venv" | "__pycache__"))
                })
                .filter_map(|entry| entry.ok())
                .filter(|entry| {
                    entry.file_type().is_file()
                        && entry.path().extension().and_then(|s| s.to_str()) == Some("py")
                })
                .map(|entry| entry.into_path())
                .collect::<Vec<_>>();
            sources.sort();
            let mut identity = String::new();
            for path in sources {
                identity.push_str(&path.strip_prefix(&plugin)?.to_string_lossy());
                identity.push_str(&hash(&path)?);
            }
            versions["densificationPlugin"] =
                json!(format!("{:x}", Sha256::digest(identity.as_bytes())));
            let torch_home = std::env::var_os("TORCH_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    PathBuf::from(
                        std::env::var_os("HOME")
                            .or_else(|| std::env::var_os("USERPROFILE"))
                            .unwrap_or_default(),
                    )
                    .join(".cache/torch")
                });
            let weights = torch_home.join("hub/checkpoints/romav2.pt");
            if weights.is_file() {
                versions["romaWeightsSha256"] = json!(hash(&weights)?);
            }
            let dinov3 = torch_home
                .join("hub/facebookresearch_dinov3_adc254450203739c8149213a7a69d8d905b4fcfa");
            if dinov3.is_dir() {
                let mut files = walkdir::WalkDir::new(&dinov3)
                    .into_iter()
                    .filter_map(|entry| entry.ok())
                    .filter(|entry| {
                        entry.file_type().is_file()
                            && entry.path().extension().and_then(|s| s.to_str()) == Some("py")
                    })
                    .map(|entry| entry.into_path())
                    .collect::<Vec<_>>();
                files.sort();
                let mut identity = String::new();
                for file in files {
                    identity.push_str(&file.strip_prefix(&dinov3)?.to_string_lossy());
                    identity.push_str(&hash(&file)?);
                }
                versions["dinov3SourceSha256"] =
                    json!(format!("{:x}", Sha256::digest(identity.as_bytes())));
            }
        }
        let converter = crate::converter()?;
        let binary = Path::new(&converter[0]);
        if binary.is_file() {
            versions["converterRuntimeSha256"] = json!(hash(binary)?);
        }
        if let Some(entry) = converter
            .get(1)
            .map(Path::new)
            .filter(|path| path.is_file())
        {
            versions["converterEntrySha256"] = json!(hash(entry)?);
        }
        let bundled_lock = std::env::var_os("WORKER_RUNTIME_ROOT")
            .map(PathBuf::from)
            .map(|root| root.join("converter/package-lock.json"));
        let converter_lock = converter.last().map(Path::new).and_then(|entry| {
            entry
                .ancestors()
                .map(|parent| parent.join("package-lock.json"))
                .find(|path| path.is_file())
        });
        let lock = bundled_lock
            .filter(|path| path.is_file())
            .or(converter_lock)
            .unwrap_or_else(|| crate::pipeline_root().join("docker/converter/package-lock.json"));
        if lock.is_file() {
            versions["converterLockSha256"] = json!(hash(&lock)?);
        }
        let mut args = converter;
        args.push("--version".into());
        versions["splatTransform"] = json!(run_capture(&args, Duration::from_secs(30))?.trim());
        versions["splatTransformGpuBackend"] = json!(crate::converter_backend());
        versions["splatTransformGpu"] =
            json!(std::env::var("SPLAT_TRANSFORM_GPU").unwrap_or_else(|_| "auto".into()));
        let runtime = crate::pipeline_root().join("runtime-versions.json");
        if runtime.is_file() {
            let recorded: Value = serde_json::from_slice(&fs::read(runtime)?)?;
            for (key, value) in recorded
                .as_object()
                .context("Runtime versions must be an object")?
            {
                let engine_specific = if self == Self::Spirula {
                    key.starts_with("lichtfeld")
                        || key.starts_with("roma")
                        || key.starts_with("dinov3")
                        || matches!(key.as_str(), "densificationPlugin" | "reconstructionEngine")
                } else {
                    key.starts_with("spirula")
                };
                if versions.get(key).is_none() && !engine_specific {
                    versions[key] = value.clone();
                }
            }
        }
        Ok(versions)
    }
    pub fn preflight(self) -> Result<Value> {
        let versions = self.versions()?;
        let gpus = match self {
            Self::Lichtfeld => {
                ensure!(
                    !cfg!(target_os = "macos"),
                    "LichtFeld requires NVIDIA CUDA; Mac workers support Spirula only"
                );
                let request = std::env::temp_dir().join(format!(
                    "guerrilla-native-probe-{}.json",
                    uuid::Uuid::new_v4()
                ));
                artifacts::save(
                    &request,
                    &json!({"action":"probe","engineBin":self.api_bin()?}),
                )?;
                let result = run_capture(
                    &[
                        crate::python(),
                        crate::pipeline_root()
                            .join("native_bindings.py")
                            .to_string_lossy()
                            .to_string(),
                        "--request".into(),
                        request.to_string_lossy().to_string(),
                    ],
                    Duration::from_secs(90),
                );
                let _ = fs::remove_file(request);
                let report = result?;
                let report: Value = serde_json::from_str(
                    report
                        .lines()
                        .rev()
                        .find(|line| line.starts_with('{'))
                        .context("Native probe omitted its result")?,
                )?;
                ensure!(report["ok"] == true, "Native engine probe failed");
                nvidia_devices()?
            }
            Self::Spirula => {
                let absent = std::env::temp_dir()
                    .join(format!("guerrilla-preflight-{}", uuid::Uuid::new_v4()));
                let arguments = vec![
                    self.path().to_string_lossy().to_string(),
                    "train".into(),
                    "--device".into(),
                    "0".into(),
                    "--data".into(),
                    absent.to_string_lossy().to_string(),
                    "--cap-max".into(),
                    "1".into(),
                    "--num-iterations".into(),
                    "1".into(),
                    "--disable-viewer".into(),
                    "1".into(),
                    "--keep-viewer-alive".into(),
                    "0".into(),
                ];
                let report =
                    crate::process::capture_unchecked(&arguments, Duration::from_secs(30))?;
                ensure!(
                    report.contains("dataset path does not exist"),
                    "Spirula GPU initialization failed: {}",
                    report
                        .chars()
                        .rev()
                        .take(1200)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect::<String>()
                );
                let re = Regex::new(
                    r"\[([0-9]+)\]\s+([^\n]+?)\s+\((?:discrete|integrated),\s*([0-9]+) MB\)\s*\n\s*UUID:\s*uuid:([0-9a-fA-F-]+)",
                )?;
                let devices = re.captures_iter(&report).collect::<Vec<_>>();
                ensure!(
                    devices.len() == 1 && &devices[0][1] == "0",
                    "Worker requires exactly one hardware GPU at device 0"
                );
                let memory: u64 = devices[0][3].parse()?;
                ensure!(memory > 0, "GPU memory unavailable");
                let mut gpu = json!([{"id":format!("gpu-{}", devices[0][4].to_lowercase()), "name":devices[0][2].to_string(), "memoryBytes":memory*1024*1024}]);
                if devices[0][2].to_uppercase().contains("NVIDIA") {
                    gpu = nvidia_devices()?;
                }
                gpu
            }
        };
        Ok(
            json!({"available":true,"workflowVerified":false,"runtimeVersions":versions,"gpus":gpus}),
        )
    }
}

pub struct Checkpoint {
    pub path: PathBuf,
    pub source: PathBuf,
    pub preview: PathBuf,
    pub config: Option<PathBuf>,
    pub step: i64,
}
pub fn step(path: &Path) -> i64 {
    let text = path.file_name().unwrap_or_default().to_string_lossy();
    Regex::new(r"(?:step[-_]|splat_|checkpoint[-_]?)(\d+)")
        .unwrap()
        .captures(&text)
        .and_then(|m| m[1].parse().ok())
        .or_else(|| {
            Regex::new(r"(\d+)")
                .unwrap()
                .captures(&text)
                .and_then(|m| m[1].parse().ok())
        })
        .unwrap_or(-1)
}
pub fn nvidia_devices() -> Result<Value> {
    let report = run_capture(
        &[
            "nvidia-smi".into(),
            "--query-gpu=uuid,name,memory.total".into(),
            "--format=csv,noheader,nounits".into(),
        ],
        Duration::from_secs(30),
    )?;
    let lines = report.trim().lines().collect::<Vec<_>>();
    ensure!(lines.len() == 1, "Worker requires one visible NVIDIA GPU");
    let fields = lines[0].split(',').map(str::trim).collect::<Vec<_>>();
    ensure!(
        fields.len() == 3 && !fields[0].is_empty(),
        "Invalid NVIDIA GPU inventory"
    );
    let memory: u64 = fields[2].parse()?;
    ensure!(memory > 0, "Invalid GPU memory");
    Ok(json!([{"id":fields[0],"name":fields[1],"memoryBytes":memory*1024*1024}]))
}

pub fn training_config(options: &Value, cap: u64) -> Value {
    let mut config: Value = serde_json::from_str(include_str!("training-defaults.json")).unwrap();
    if let Some(options) = options["training"].as_object() {
        for (key, value) in options {
            config[key] = value.clone();
        }
    }
    let iterations = config["iterations"].as_u64().unwrap();
    let end = iterations
        + if config["enable_sparsity"] == true {
            config["sparsify_steps"].as_u64().unwrap()
        } else {
            0
        };
    config["max_cap"] = json!(cap);
    config["grow_until_iter"] = json!(iterations);
    config["stop_refine"] = json!((iterations as f64 * 1.1).round() as u64);
    config["save_steps"] = json!(if end == iterations {
        vec![iterations]
    } else {
        vec![iterations, end]
    });
    config
}

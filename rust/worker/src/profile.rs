use crate::{
    files,
    protocol::{Credentials, Gpu, Registration},
};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Profile {
    pub control_url: String,
    pub scratch_directory: String,
    pub python_path: String,
    pub ffmpeg_path: String,
    pub node_path: String,
    pub converter_path: String,
    pub lichtfeld_path: String,
    pub densification_plugin_path: String,
    pub spirula_path: String,
    pub start_at_login: bool,
}
#[derive(Clone)]
pub struct ContextPaths {
    pub profile: Profile,
    pub config: PathBuf,
    pub state: PathBuf,
    pub scratch: PathBuf,
    pub pipeline: PathBuf,
}
impl ContextPaths {
    pub fn load(config: PathBuf) -> Result<Self> {
        if !config.is_absolute() {
            bail!("--config must be an absolute profile path");
        }
        let mut profile: Profile = if config.exists() {
            serde_json::from_slice(&std::fs::read(&config)?)?
        } else {
            Profile::default()
        };
        if profile.control_url.is_empty() {
            profile.control_url = std::env::var("WORKER_CONTROL_URL").unwrap_or_default();
        }
        let state = config
            .parent()
            .context("Missing profile parent")?
            .to_path_buf();
        files::private_dir(&state)?;
        let scratch = std::env::var_os("WORKER_SCRATCH")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if profile.scratch_directory.is_empty() {
                    dirs::cache_dir()
                        .unwrap_or_else(|| state.clone())
                        .join("guerrilla-worker")
                } else {
                    PathBuf::from(&profile.scratch_directory)
                }
            });
        if !scratch.is_absolute() {
            bail!("Scratch directory must be absolute");
        }
        let executable = std::env::current_exe()?;
        let pipeline = std::env::var_os("WORKER_PIPELINE_BIN")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                executable.parent().unwrap().join(if cfg!(windows) {
                    "guerrilla-pipeline.exe"
                } else {
                    "guerrilla-pipeline"
                })
            });
        let result = Self {
            profile,
            config,
            state,
            scratch,
            pipeline,
        };
        if let Some(credentials) = result.credentials()? {
            if !result.profile.control_url.is_empty()
                && crate::api::origin(&result.profile.control_url)? != credentials.origin
            {
                bail!("Disconnect worker before changing control plane origin");
            }
        }
        Ok(result)
    }
    pub fn credential_path(&self) -> PathBuf {
        std::env::var_os("WORKER_CREDENTIAL_FILE")
            .map(PathBuf::from)
            .unwrap_or_else(|| self.state.join("credentials.json"))
    }
    pub fn credentials(&self) -> Result<Option<Credentials>> {
        match std::fs::read(self.credential_path()) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }
    pub fn runtime_root(&self) -> PathBuf {
        std::env::var_os("WORKER_RUNTIME_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                self.pipeline
                    .parent()
                    .unwrap_or(Path::new("."))
                    .join("runtime")
            })
    }
    pub fn command(&self) -> tokio::process::Command {
        let mut command = tokio::process::Command::new(&self.pipeline);
        command
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONNOUSERSITE", "1");
        // Bundled bindings and converter imports must not depend on the user's shell setup.
        // Keep engine paths and native-library search paths inherited for user-installed engines.
        for key in [
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
            "PYTHONPLATLIBDIR",
            "PYTHONEXECUTABLE",
            "__PYVENV_LAUNCHER__",
            "NODE_OPTIONS",
            "NODE_PATH",
        ] {
            command.env_remove(key);
        }
        let runtime = self.runtime_root();
        let installed = guerrilla_runtime::installed(&runtime, &self.state);
        let managed_python = self.profile.python_path
            == guerrilla_runtime::python(&runtime).to_string_lossy()
            || self.profile.python_path.is_empty() && std::env::var_os("PYTHON").is_none()
            || installed.as_ref().is_some_and(|gpu| {
                self.profile.python_path == guerrilla_runtime::python(gpu).to_string_lossy()
            });
        let gpu = installed.filter(|_| managed_python);
        let selected_python = gpu
            .as_ref()
            .map(|root| {
                guerrilla_runtime::python(root)
                    .to_string_lossy()
                    .into_owned()
            })
            .unwrap_or_else(|| self.profile.python_path.clone());
        let configs = [
            (
                "PYTHON",
                &selected_python,
                if cfg!(windows) {
                    "python/python.exe"
                } else {
                    "python/bin/python3"
                },
            ),
            (
                "FFMPEG",
                &self.profile.ffmpeg_path,
                if cfg!(windows) {
                    "ffmpeg/ffmpeg.exe"
                } else {
                    "ffmpeg/ffmpeg"
                },
            ),
            (
                "NODE_BIN",
                &self.profile.node_path,
                if cfg!(windows) {
                    "node/node.exe"
                } else {
                    "node/bin/node"
                },
            ),
            (
                "SPLAT_TRANSFORM_BIN",
                &self.profile.converter_path,
                "converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs",
            ),
        ];
        for (key, value, fallback) in configs {
            if !value.is_empty() {
                command.env(key, value);
            } else if std::env::var_os(key).is_none() && runtime.join(fallback).exists() {
                command.env(key, runtime.join(fallback));
            }
        }
        for (key, value) in [
            ("LICHTFELD_BIN", &self.profile.lichtfeld_path),
            (
                "LICHTFELD_DENSIFICATION_PLUGIN",
                &self.profile.densification_plugin_path,
            ),
            ("SPIRULA_BIN", &self.profile.spirula_path),
        ] {
            if !value.is_empty() {
                command.env(key, value);
            }
        }
        command.env_remove("WORKER_GPU_RUNTIME_ROOT");
        let model_runtime = gpu.as_ref().unwrap_or(&runtime);
        if let Some(gpu) = &gpu {
            command.env("WORKER_GPU_RUNTIME_ROOT", gpu);
        }
        if model_runtime.join("models").exists() && std::env::var_os("TORCH_HOME").is_none() {
            command.env("TORCH_HOME", model_runtime.join("models"));
        }
        if runtime.join("windows-crt").is_dir() {
            command.env("WORKER_CRT_ROOT", runtime.join("windows-crt"));
        }
        let mut paths: Vec<PathBuf> = [
            runtime.join("ffmpeg"),
            runtime.join(if cfg!(windows) { "node" } else { "node/bin" }),
        ]
        .into_iter()
        .filter(|p| p.exists())
        .collect();
        if !self.profile.ffmpeg_path.is_empty() {
            if let Some(parent) = Path::new(&self.profile.ffmpeg_path).parent() {
                paths.insert(0, parent.into());
            }
        }
        if let Some(existing) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&existing));
        }
        if let Ok(path) = std::env::join_paths(paths) {
            command.env("PATH", path);
        }
        command.env("WORKER_PARENT_PID", std::process::id().to_string());
        command
    }
    pub async fn preflight(&self) -> Result<serde_json::Value> {
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(120),
            self.command().arg("--preflight").output(),
        )
        .await
        .context("Dependency check timed out")??;
        if !result.status.success() {
            bail!(
                "Dependency check failed: {}",
                crate::protocol::redact(&String::from_utf8_lossy(&result.stderr))
            );
        }
        serde_json::from_slice(&result.stdout).context("Invalid dependency report")
    }
    pub fn registration(report: &serde_json::Value) -> Result<Registration> {
        if std::env::var("WORKER_GPU_INDEX").is_ok_and(|x| x != "0") {
            bail!("Isolate one GPU at index 0 before running worker");
        }
        let gpus: Vec<Gpu> =
            serde_json::from_value(report["gpus"].clone()).context("No usable hardware GPU")?;
        if gpus.len() != 1
            || gpus[0].memory_bytes == 0
            || gpus[0].name.is_empty()
            || gpus[0].name.len() > 200
            || gpus[0].id.is_empty()
            || gpus[0].id.len() > 128
            || !gpus[0]
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
        {
            bail!("Exactly one usable hardware GPU is required");
        }
        let mut engines = std::collections::BTreeMap::new();
        for engine in ["spirula", "lichtfeld"] {
            let capability = &report["backends"][engine];
            if capability["available"] == true {
                let versions: std::collections::BTreeMap<String, String> =
                    serde_json::from_value(capability["runtimeVersions"].clone())
                        .context("Engine runtime identity missing")?;
                engines.insert(
                    engine.into(),
                    hex::encode(Sha256::digest(serde_json::to_vec(&versions)?)),
                );
            }
        }
        Ok(Registration {
            healthy: !engines.is_empty(),
            engines,
            gpus,
            ..Self::enrollment_identity()
        })
    }
    pub fn enrollment_identity() -> Registration {
        Registration {
            protocol_version: 2,
            build: env!("CARGO_PKG_VERSION").into(),
            runtime: format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
            healthy: false,
            engines: Default::default(),
            gpus: vec![],
        }
    }
}

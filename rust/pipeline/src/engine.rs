use crate::{
    artifacts::{self, hash},
    process::run_capture,
};
use anyhow::{ensure, Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    time::Duration,
};

pub const SPIRULA_VERSION: &str = "2026.10.16";
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Engine {
    Spirula,
}
impl Engine {
    pub fn name(self) -> &'static str {
        "spirula"
    }
    pub fn path(self) -> PathBuf {
        if let Some(path) = std::env::var_os("SPIRULA_BIN") {
            return PathBuf::from(path);
        }
        crate::which(if cfg!(windows) {
            "spirula.exe"
        } else {
            "spirula"
        })
        .unwrap_or_else(|| PathBuf::from("/opt/spirula/spirula"))
    }
    pub fn suffix(self) -> &'static str {
        "ckpt"
    }
    pub fn checkpoint(self, path: PathBuf) -> Checkpoint {
        Checkpoint {
            source: path.join("state.tar"),
            preview: path.join("splat.ply"),
            config: path.parent().map(|p| p.join("config.json")),
            step: step(&path),
            path,
        }
    }
    pub fn checkpoints(self, folder: &Path) -> Result<Vec<Checkpoint>> {
        let mut candidates = artifacts::files(folder)?
            .into_iter()
            .filter(|path| {
                path.file_name().and_then(|s| s.to_str()) == Some("state.tar")
                    && !path
                        .components()
                        .any(|c| c.as_os_str() == "retained-checkpoints")
            })
            .map(|path| self.checkpoint(path.parent().unwrap().to_path_buf()))
            .collect::<Vec<_>>();
        candidates.sort_by_key(|checkpoint| (checkpoint.step, checkpoint.path.clone()));
        candidates.reverse();
        Ok(candidates)
    }
    pub fn valid_checkpoint(self, checkpoint: &Checkpoint, cap: u64) -> Result<bool> {
        if !checkpoint.source.is_file() || checkpoint.source.metadata()?.len() == 0 {
            return Ok(false);
        }
        // A frozen process may be between writes. Denying write sharing catches
        // open Windows writers; complete tar framing rejects interrupted saves.
        #[cfg(windows)]
        let mut file = {
            use std::os::windows::fs::OpenOptionsExt;
            match fs::OpenOptions::new()
                .read(true)
                .share_mode(1)
                .open(&checkpoint.source)
            {
                Ok(file) => file,
                Err(error) if error.raw_os_error() == Some(32) => return Ok(false),
                Err(error) => return Err(error.into()),
            }
        };
        #[cfg(not(windows))]
        let mut file = fs::File::open(&checkpoint.source)?;
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
        // Spirula's native config permits Infinity (e.g. outlier_threshold).
        // Preserve it as engine-owned bytes rather than parsing strict JSON.
        let valid_config = checkpoint.config.as_ref().is_some_and(|path| {
            path.is_file() && path.metadata().is_ok_and(|metadata| metadata.len() > 0)
        });
        Ok(full_resume && world && valid_config)
    }
    pub fn training_output(self, folder: &Path) -> Result<(PathBuf, Option<PathBuf>)> {
        let final_dir = folder.join("final");
        let ply = final_dir.join("splat.ply");
        if ply.is_file() {
            return Ok((ply, Some(final_dir.join("config.json"))));
        }
        self.run_output(folder)
    }
    pub fn run_output(self, folder: &Path) -> Result<(PathBuf, Option<PathBuf>)> {
        let latest = self
            .checkpoints(&folder.join("run"))?
            .into_iter()
            .find(|checkpoint| checkpoint.preview.is_file())
            .context("Training produced no final PLY")?;
        Ok((latest.preview, latest.config))
    }
    pub fn versions(self) -> Result<Value> {
        let path = self.path();
        ensure!(
            path.is_file(),
            "Spirula executable is unavailable; install the managed runtime"
        );
        let version = run_capture(
            &[path.to_string_lossy().to_string(), "--version".into()],
            Duration::from_secs(30),
        )?;
        ensure!(
            version.split_whitespace().next() == Some(SPIRULA_VERSION),
            "Expected Spirula {SPIRULA_VERSION}, got {}",
            version.trim()
        );
        let mut versions = json!({"spirula": version.trim(), "spirulaBinarySha256":hash(&path)?,
            "pipelineProtocol":"rust-v2", "pipelineBinarySha256":hash(&std::env::current_exe()?)?});
        if let Some(model) = std::env::var_os("SPIRULA_ROMA_MODEL") {
            versions["romaModelSha256"] = json!(hash(Path::new(&model))?);
        }
        let converter = crate::converter()?;
        let mut args = converter.clone();
        args.push("--version".into());
        versions["splatTransform"] = json!(run_capture(&args, Duration::from_secs(30))?.trim());
        versions["converterSha256"] = json!(hash(Path::new(converter.last().unwrap()))?);
        if converter.len() > 1 {
            versions["nodeSha256"] = json!(hash(Path::new(&converter[0]))?);
        }
        versions["splatTransformGpuBackend"] = json!(crate::converter_backend());
        versions["splatTransformGpu"] =
            json!(std::env::var("SPLAT_TRANSFORM_GPU").unwrap_or_else(|_| "auto".into()));
        Ok(versions)
    }
    pub fn preflight(self) -> Result<Value> {
        let versions = self.versions()?;
        let gpus = {
            let absent =
                std::env::temp_dir().join(format!("guerrilla-preflight-{}", uuid::Uuid::new_v4()));
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
            let report = crate::process::capture_unchecked(&arguments, Duration::from_secs(30))?;
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
    Regex::new(r"step[-_](\d+)")
        .unwrap()
        .captures(&path.file_name().unwrap_or_default().to_string_lossy())
        .and_then(|m| m[1].parse().ok())
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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_config_infinity_does_not_reject_a_full_checkpoint() {
        let root = tempfile::tempdir().unwrap();
        let directory = root.path().join("step-000001500.ckpt");
        fs::create_dir(&directory).unwrap();
        fs::write(
            root.path().join("config.json"),
            br#"{"outlier_threshold":Infinity}"#,
        )
        .unwrap();
        let mut archive = tar::Builder::new(fs::File::create(directory.join("state.tar")).unwrap());
        for (name, contents) in [
            (
                "state.json",
                br#"{"full_resume":1,"cur_num_splats":4}"#.as_slice(),
            ),
            ("world.means.npy", b"native world tensor".as_slice()),
        ] {
            let mut header = tar::Header::new_ustar();
            header.set_size(contents.len() as u64);
            header.set_mode(0o644);
            header.set_cksum();
            archive.append_data(&mut header, name, contents).unwrap();
        }
        archive.finish().unwrap();
        let checkpoint = Engine::Spirula.checkpoint(directory);
        #[cfg(windows)]
        assert!(!Engine::Spirula.valid_checkpoint(&checkpoint, 4).unwrap());
        drop(archive);
        assert!(Engine::Spirula.valid_checkpoint(&checkpoint, 4).unwrap());
    }
}

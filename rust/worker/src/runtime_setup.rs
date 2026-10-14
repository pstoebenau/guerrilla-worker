use crate::{daemon, profile::ContextPaths, protocol::redact};
use anyhow::{bail, Context, Result};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{io::Write, process::Stdio};
use tokio::io::{AsyncBufReadExt, BufReader};

pub async fn install(paths: &ContextPaths) -> Result<Value> {
    if daemon::status(paths)?.running {
        bail!("Stop the worker before installing GPU dependencies");
    }
    let base = paths.runtime_root();
    if !guerrilla_runtime::status(&base, &paths.state).supported {
        bail!("This platform uses the bundled base runtime; NVIDIA setup is available on Windows and Linux x64");
    }
    let script = base.join("setup/runtime_setup.py");
    let descriptor: Value =
        serde_json::from_slice(&std::fs::read(base.join("setup/manifest.json"))?)?;
    let hash = format!("{:x}", Sha256::digest(std::fs::read(&script)?));
    if descriptor["installerSha256"].as_str() != Some(&hash) {
        bail!("GPU setup helper failed integrity verification; reinstall the desktop app");
    }
    let mut command = tokio::process::Command::new(guerrilla_runtime::python(&base));
    command
        .arg("-I")
        .arg(script)
        .arg("--resources")
        .arg(&base)
        .arg("--data-directory")
        .arg(&paths.state)
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .env_remove("WORKER_ENROLLMENT_TOKEN")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("__PYVENV_LAUNCHER__")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let mut child = command
        .spawn()
        .context("Cannot start the bundled GPU setup helper")?;
    let mut lines = BufReader::new(child.stdout.take().context("Missing setup output")?).lines();
    let mut result = None;
    let mut failure = None;
    while let Some(line) = lines.next_line().await? {
        if line.len() > 16_384 {
            bail!("Invalid GPU setup response");
        }
        let mut event: Value = serde_json::from_str(&line).context("Invalid GPU setup progress")?;
        if let Some(message) = event["message"].as_str() {
            event["message"] = serde_json::json!(redact(message));
        }
        match event["type"].as_str() {
            Some("runtime-setup-progress") => {
                println!("{event}");
                std::io::stdout().flush()?;
            }
            Some("runtime-setup-complete") => result = Some(event),
            Some("runtime-setup-error") => failure = event["message"].as_str().map(str::to_owned),
            _ => bail!("Unexpected GPU setup response"),
        }
    }
    if !child.wait().await?.success() {
        bail!(
            "{}",
            failure.unwrap_or_else(|| "GPU runtime setup failed; retry from Setup".into())
        );
    }
    result.context("GPU runtime setup did not confirm completion")
}

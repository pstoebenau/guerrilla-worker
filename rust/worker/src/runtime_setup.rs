use crate::{daemon, profile::ContextPaths};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::{io::Write, path::PathBuf};

/// Explicit paths support bundled container distributions and isolated native fixtures.
pub async fn ensure(paths: &ContextPaths) -> Result<()> {
    if std::env::var_os("SPIRULA_BIN")
        .map(PathBuf::from)
        .is_some_and(|p| p.is_file())
        && std::env::var_os("SPIRULA_ROMA_MODEL")
            .map(PathBuf::from)
            .is_some_and(|p| p.is_file())
    {
        return Ok(());
    }
    guerrilla_runtime::install(&paths.runtime_root(), &paths.state, |message| {
        eprintln!("{message}")
    })
    .await?;
    Ok(())
}
pub async fn install(paths: &ContextPaths) -> Result<Value> {
    if daemon::status(paths)?.running {
        bail!("Stop the worker before repairing its runtime");
    }
    let runtime = guerrilla_runtime::install(&paths.runtime_root(), &paths.state, |message| {
        println!(
            "{}",
            json!({"type":"runtime-setup-progress", "message":message})
        );
        let _ = std::io::stdout().flush();
    })
    .await?;
    Ok(
        json!({"type":"runtime-setup-complete", "installed":true, "root":runtime.root, "spirulaPath":runtime.executable, "modelPath":runtime.model}),
    )
}

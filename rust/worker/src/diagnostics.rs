use crate::{files, profile::ContextPaths, protocol::redact};
use anyhow::{bail, Result};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::Path,
    time::UNIX_EPOCH,
};

#[derive(Serialize)]
pub struct Tail {
    pub lines: Vec<String>,
    pub truncated: bool,
}

pub fn tail(path: &Path) -> Result<Tail> {
    let mut file = fs::File::open(path)?;
    let offset = file.metadata()?.len().saturating_sub(256 * 1024);
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = Vec::new();
    file.take(256 * 1024).read_to_end(&mut bytes)?;
    let text = String::from_utf8_lossy(&bytes);
    let text = if offset > 0 {
        text.split_once('\n').map(|(_, rest)| rest).unwrap_or("")
    } else {
        &text
    };
    let count = text.lines().count();
    let mut lines: Vec<_> = text.lines().rev().take(1000).map(redact).collect();
    lines.reverse();
    Ok(Tail {
        lines,
        truncated: offset > 0 || count > 1000,
    })
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Source {
    id: String,
    label: String,
    modified_at: u64,
}
fn is_log(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|s| s.to_str()),
        Some("log" | "jsonl")
    )
}
fn discover(
    root: &Path,
    directory: &Path,
    depth: usize,
    budget: &mut usize,
    sources: &mut Vec<Source>,
) -> Result<()> {
    if depth > 6 || *budget == 0 {
        return Ok(());
    }
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    let mut directories = Vec::new();
    for entry in entries {
        if *budget == 0 {
            break;
        }
        *budget -= 1;
        let entry = entry?;
        let kind = entry.file_type()?;
        let path = entry.path();
        if kind.is_symlink() || entry.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        if kind.is_dir() {
            directories.push((entry.metadata()?.modified()?, path));
        } else if kind.is_file() && is_log(&path) {
            let relative = path
                .strip_prefix(root)?
                .to_string_lossy()
                .replace('\\', "/");
            // The archived worker tail duplicates the full attempt log.
            if relative.ends_with("/run/logs/worker.log")
                || relative.ends_with("/run/logs/download.log")
            {
                continue;
            }
            sources.push(Source {
                id: format!("scratch/{relative}"),
                label: relative,
                modified_at: entry
                    .metadata()?
                    .modified()?
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis() as u64,
            });
        }
    }
    // Read direct logs before descending into potentially large stage datasets.
    directories.sort_by(|a, b| b.0.cmp(&a.0));
    for (_, path) in directories {
        discover(root, &path, depth + 1, budget, sources)?;
    }
    Ok(())
}
pub fn logs(paths: &ContextPaths, source: Option<&str>) -> Result<Value> {
    let mut sources = Vec::new();
    if paths.scratch.exists()
        && fs::symlink_metadata(&paths.scratch)?
            .file_type()
            .is_symlink()
    {
        bail!("Scratch paths cannot traverse links");
    }
    discover(&paths.scratch, &paths.scratch, 0, &mut 20_000, &mut sources)?;
    sources.sort_by(|a, b| b.modified_at.cmp(&a.modified_at).then(a.id.cmp(&b.id)));
    sources.push(Source {
        id: "service".into(),
        label: "Worker service · daemon.log".into(),
        modified_at: 0,
    });
    let selected = source.unwrap_or(&sources[0].id);
    let path = if selected == "service" {
        files::checked(&paths.state, "daemon.log")?
    } else if let Some(relative) = selected.strip_prefix("scratch/") {
        let path = files::checked(&paths.scratch, relative)?;
        if !is_log(&path) {
            bail!("Select a pipeline log");
        }
        path
    } else {
        bail!("Unknown log source");
    };
    let output = match tail(&path) {
        Ok(output) => output,
        Err(e)
            if selected == "service"
                && e.downcast_ref::<std::io::Error>()
                    .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            Tail {
                lines: vec![],
                truncated: false,
            }
        }
        Err(e) => return Err(e),
    };
    Ok(
        json!({"source":selected,"path":path,"sources":sources,"lines":output.lines,"truncated":output.truncated}),
    )
}

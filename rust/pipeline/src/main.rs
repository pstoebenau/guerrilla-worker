mod artifacts;
mod download;
mod engine;
mod process;
mod progress;
mod runner;

use anyhow::{ensure, Context, Result};
use clap::Parser;
use engine::Engine;
use serde_json::{json, Value};
use std::{io::Write, path::PathBuf};

#[derive(Parser)]
#[command(about = "Portable Guerrilla worker execution core")]
struct Cli {
    #[arg(long)]
    request: Option<PathBuf>,
    #[arg(long)]
    preflight: bool,
    #[arg(long, value_parser=["lichtfeld","spirula"])]
    engine: Option<String>,
    #[arg(long)]
    download: Option<String>,
    #[arg(long)]
    public_source: bool,
    #[arg(long)]
    destination: Option<PathBuf>,
    #[arg(long)]
    retention_plan: Option<PathBuf>,
    #[arg(long)]
    compact_state: Option<PathBuf>,
    #[arg(long, value_parser=["lichtfeld","spirula"])]
    runtime_versions: Option<String>,
}
pub fn emit(kind: &str, values: Value) {
    let mut event = values.as_object().cloned().unwrap_or_default();
    for key in ["message", "error"] {
        if let Some(message) = event.get(key).and_then(Value::as_str) {
            event.insert(key.into(), json!(redact(message)));
        }
    }
    event.insert("type".into(), json!(kind));
    event.insert(
        "timestamp".into(),
        json!(std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64()),
    );
    let stdout = std::io::stdout();
    let mut stream = stdout.lock();
    let _ = serde_json::to_writer(&mut stream, &event);
    let _ = stream.write_all(b"\n");
    let _ = stream.flush();
}
pub fn pipeline_root() -> PathBuf {
    std::env::var_os("GUERRILLA_PIPELINE_ROOT")
        .or_else(|| std::env::var_os("PIPELINE_ROOT"))
        .or_else(|| std::env::var_os("WORKER_PIPELINE_ROOT"))
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let exe = std::env::current_exe().unwrap_or_default();
            let parent = exe.parent().unwrap_or_else(|| std::path::Path::new("."));
            if parent.join("pipeline/native_bindings.py").is_file() {
                parent.join("pipeline")
            } else {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../pipeline")
            }
        })
}
pub fn python() -> String {
    std::env::var("PYTHON")
        .or_else(|_| std::env::var("PYTHON_BIN"))
        .unwrap_or_else(|_| {
            if cfg!(windows) {
                "python.exe".into()
            } else {
                "python3".into()
            }
        })
}
pub fn which(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|folder| folder.join(name))
            .find(|path| path.is_file())
    })
}
pub fn converter_backend() -> String {
    std::env::var("SPLAT_TRANSFORM_GPU_BACKEND").unwrap_or_else(|_| {
        if cfg!(target_os = "macos") {
            "metal".into()
        } else if cfg!(windows) {
            "d3d12".into()
        } else {
            "vulkan".into()
        }
    })
}
pub fn converter() -> Result<Vec<String>> {
    let local = pipeline_root()
        .join("docker/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs");
    let mut path = std::env::var_os("SPLAT_TRANSFORM_BIN")
        .map(PathBuf::from)
        .or_else(|| {
            if local.is_file() {
                Some(local)
            } else {
                which("splat-transform")
            }
        })
        .context("Bundled splat-transform is unavailable")?;
    if path.extension().and_then(|s| s.to_str()) == Some("cmd") {
        path = path
            .parent()
            .unwrap()
            .join("node_modules/@playcanvas/splat-transform/bin/cli.mjs");
    }
    if matches!(
        path.extension().and_then(|s| s.to_str()),
        Some("mjs" | "js" | "cjs")
    ) {
        ensure!(path.is_file(), "Converter JavaScript entry is missing");
        let node = std::env::var("NODE_BIN")
            .ok()
            .or_else(|| {
                which(if cfg!(windows) { "node.exe" } else { "node" })
                    .map(|p| p.to_string_lossy().to_string())
            })
            .context("Bundled Node runtime is missing")?;
        Ok(vec![node, path.to_string_lossy().to_string()])
    } else {
        Ok(vec![path.to_string_lossy().to_string()])
    }
}
pub fn densification_plugin() -> PathBuf {
    std::env::var_os("LICHTFELD_DENSIFICATION_PLUGIN")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(
                std::env::var_os("HOME")
                    .or_else(|| std::env::var_os("USERPROFILE"))
                    .unwrap_or_default(),
            )
            .join(".lichtfeld/plugins/densification")
        })
}
pub fn redact(message: &str) -> String {
    let urls = regex::Regex::new(r#"(?i)https?://[^\s\"'<>]+"#).unwrap();
    let bearer = regex::Regex::new(r"(?i)Bearer\s+[^\s]+").unwrap();
    bearer
        .replace_all(
            &urls.replace_all(message, "[redacted URL]"),
            "Bearer [redacted]",
        )
        .into_owned()
}
fn parse_engine(name: &str) -> Engine {
    if name == "spirula" {
        Engine::Spirula
    } else {
        Engine::Lichtfeld
    }
}
fn run(cli: Cli) -> Result<()> {
    if let Some(engine) = cli.runtime_versions {
        println!("{}", parse_engine(&engine).versions()?);
        return Ok(());
    }
    if let Some(root) = cli.compact_state {
        println!(
            "{}",
            serde_json::to_string(&runner::compact_state(&root.canonicalize()?)?)?
        );
        return Ok(());
    }
    if let Some(root) = cli.retention_plan {
        let root = root.canonicalize()?;
        let state = serde_json::from_slice(&std::fs::read(root.join("platform-state.json"))?)?;
        println!(
            "{}",
            serde_json::to_string(&runner::retention_plan(&root, &state)?)?
        );
        return Ok(());
    }
    if cli.preflight {
        let mut report = json!({"backends":{}});
        let engines = cli
            .engine
            .map(|name| vec![parse_engine(&name)])
            .unwrap_or_else(|| vec![Engine::Lichtfeld, Engine::Spirula]);
        for engine in engines {
            match engine.preflight() {
                Ok(mut capability) => {
                    report["gpus"] = capability["gpus"].take();
                    capability.as_object_mut().unwrap().remove("gpus");
                    report["backends"][engine.name()] = capability;
                }
                Err(error) => {
                    report["backends"][engine.name()] = json!({"available":false,"workflowVerified":false,"error":redact(&format!("{error:#}")).chars().take(2000).collect::<String>()})
                }
            }
        }
        println!("{report}");
        return Ok(());
    }
    let mut runtime = process::Runtime::new()?;
    if let Some(url) = cli.download {
        let destination = cli
            .destination
            .context("--destination is required for --download")?;
        download::download(&url, &destination, cli.public_source, &runtime)?;
        emit(
            "downloaded",
            json!({"path":destination,"sha256":artifacts::hash(&destination)?,"size":destination.metadata()?.len()}),
        );
        return Ok(());
    }
    let request: runner::Request = serde_json::from_slice(&std::fs::read(
        cli.request.context("--request is required")?,
    )?)?;
    runtime.lock_gpu()?;
    let preflight = request.backend.preflight()?;
    runner::Runner::new(request, runtime, preflight["runtimeVersions"].clone())?.run()
}
fn main() {
    std::env::set_var("PYTHONDONTWRITEBYTECODE", "1");
    std::env::set_var("PYTHONUNBUFFERED", "1");
    let args = std::env::args().collect::<Vec<_>>();
    if args.get(1).map(String::as_str) == Some("--guard-parent") {
        let result = (|| {
            let parent = args.get(2).context("Missing guardian parent")?.parse()?;
            ensure!(
                args.get(3).map(String::as_str) == Some("--"),
                "Invalid guardian command"
            );
            process::guard(parent, &args[4..])
        })();
        if let Err(error) = result {
            eprintln!("{}", redact(&format!("{error:#}")));
            std::process::exit(1)
        }
        return;
    }
    if let Err(error) = run(Cli::parse()) {
        emit(
            "failed",
            json!({"error":redact(&format!("{error:#}")).chars().take(2000).collect::<String>(),"interrupted":error.to_string().contains("interrupted")||error.to_string().contains("parent exited")}),
        );
        std::process::exit(1)
    }
}

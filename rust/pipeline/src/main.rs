mod artifacts;
mod download;
mod engine;
mod process;
mod progress;
mod runner;

use anyhow::{ensure, Context, Result};
use clap::{Args, Parser, Subcommand};
use engine::Engine;
use serde_json::{json, Value};
use std::{fs, io::Write, path::PathBuf};

#[derive(Parser)]
#[command(about = "Portable Guerrilla worker execution core")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
    #[arg(long)]
    request: Option<PathBuf>,
    #[arg(long)]
    preflight: bool,
    #[arg(long, value_parser=["spirula"])]
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
    #[arg(long, value_parser=["spirula"])]
    runtime_versions: Option<String>,
}
#[derive(Subcommand)]
enum Command {
    Scan(ScanArgs),
}
#[derive(Args)]
struct ScanArgs {
    /// Local video, video URL, or image folder.
    input: String,
    #[arg(long)]
    output: PathBuf,
    #[arg(long)]
    max_cap: u64,
    #[arg(long)]
    resume: bool,
    #[arg(long)]
    settings: Option<PathBuf>,
    #[arg(long)]
    no_densification: bool,
}
fn scan(args: ScanArgs, mut runtime: process::Runtime) -> Result<()> {
    use fs2::FileExt;
    use sha2::{Digest, Sha256};
    let mut settings: Value = args
        .settings
        .as_ref()
        .map(|path| -> Result<_> { Ok(serde_json::from_slice(&fs::read(path)?)?) })
        .transpose()?
        .unwrap_or_else(|| json!({}));
    if args.no_densification {
        ensure!(settings.is_object(), "Settings must be an object");
        if settings.get("densification").is_none() {
            settings["densification"] = json!({});
        }
        ensure!(
            settings["densification"].is_object(),
            "Densification settings must be an object"
        );
        settings["densification"]["enabled"] = json!(false);
    }
    let settings = artifacts::validate_settings("spirula", &settings, args.max_cap)?;
    ensure!(
        args.max_cap > 0 && args.max_cap <= 30000000,
        "--max-cap must be within 1..30000000"
    );
    let output = std::path::absolute(&args.output)?;
    ensure!(!output.is_symlink(), "Output may not be a symlink");
    if args.resume {
        ensure!(
            output.join(".worker/input.json").is_file(),
            "No scan to resume in this output folder"
        );
    }
    if output.exists() && !args.resume {
        ensure!(
            fs::read_dir(&output)?.next().is_none(),
            "Output must be new or empty; use --resume"
        );
    }
    let remote = args.input.starts_with("https://") || args.input.starts_with("http://");
    let local = (!remote)
        .then(|| fs::canonicalize(&args.input))
        .transpose()?;
    if let Some(input) = &local {
        ensure!(
            input != &output && !output.starts_with(input) && !input.starts_with(&output),
            "Input and output must be separate paths"
        );
    }
    ensure!(
        !output.join(".worker").is_symlink(),
        "Scan control directory may not be a symlink"
    );
    fs::create_dir_all(output.join(".worker"))?;
    let output = output.canonicalize()?;
    let lock = fs::File::options()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(artifacts::safe(&output, ".worker/scan.lock")?)?;
    lock.try_lock_exclusive()
        .context("Another scan is using this output folder")?;
    let source_identity = local
        .as_ref()
        .map(|path| path.to_string_lossy().to_string())
        .unwrap_or_else(|| args.input.clone());
    let identity = format!("{:x}", Sha256::digest(source_identity.as_bytes()));
    let identity_path = artifacts::safe(&output, ".worker/input.json")?;
    if identity_path.is_file() {
        let previous: Value = serde_json::from_slice(&fs::read(&identity_path)?)?;
        ensure!(
            args.resume && previous["sourceSha256"] == identity,
            "Scan input changed; use a new output folder"
        );
    } else {
        artifacts::save(&identity_path, &json!({"sourceSha256":identity}))?;
    }
    let source = if let Some(local) = local {
        local
    } else {
        let source = artifacts::safe(&output, ".worker/input/source.mp4")?;
        if !source.is_file() {
            download::download(&args.input, &source, false, &runtime)?;
        }
        source
    };
    runtime.lock_gpu()?;
    let preflight = Engine::Spirula.preflight()?;
    let request = runner::Request {
        scan_id: "local".into(),
        attempt_id: "local".into(),
        input_path: source,
        output_path: output,
        backend: Engine::Spirula,
        max_cap: args.max_cap,
        settings,
        resume: args.resume,
        standalone: true,
        selective_resume: false,
        input_sha256: None,
        runtime_versions: None,
        archive_ack: false,
        background_archive: false,
        archive_timeout_seconds: 3600,
    };
    runner::Runner::new(request, runtime, preflight["runtimeVersions"].clone())?.run()
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
    let runtime = std::env::var_os("WORKER_RUNTIME_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::current_exe()
                .unwrap_or_default()
                .parent()
                .unwrap_or(std::path::Path::new("."))
                .join("runtime")
        });
    let local = runtime.join("converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs");
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
fn run(cli: Cli) -> Result<()> {
    if cli.runtime_versions.is_some() {
        println!("{}", Engine::Spirula.versions()?);
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
        {
            let engine = Engine::Spirula;
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
    if let Some(Command::Scan(args)) = cli.command {
        return scan(args, runtime);
    }
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
    std::env::set_var("SS_NO_AUTO_FETCH", "1");
    std::env::set_var("SS_LANG", "en");
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

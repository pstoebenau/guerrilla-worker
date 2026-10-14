use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use guerrilla_worker::{daemon, profile::ContextPaths, protocol::redact};
use std::path::PathBuf;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};

#[derive(Parser)]
#[command(version, about = "Guerrilla enrolled background GPU worker")]
struct Cli {
    #[arg(long, env = "WORKER_CONFIG_FILE")]
    config: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
    #[arg(long)]
    preflight: bool,
}
#[derive(Subcommand)]
enum Command {
    /// Download and verify GPU libraries and models into this profile's runtime directory.
    SetupRuntime,
    Status,
    Preflight,
    Enroll {
        #[arg(long)]
        token_stdin: bool,
    },
    Start,
    Stop,
    Drain,
    Disconnect,
    Logs,
    Run,
}
#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!(
            "{}",
            serde_json::json!({"error":redact(&error.to_string())})
        );
        std::process::exit(1);
    }
}
async fn run() -> Result<()> {
    let cli = Cli::parse();
    let config = cli.config.unwrap_or_else(|| {
        std::env::var_os("WORKER_CREDENTIAL_FILE")
            .map(PathBuf::from)
            .and_then(|p| p.parent().map(|p| p.join("profile.json")))
            .unwrap_or_else(|| {
                dirs::config_dir()
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join("guerrilla-worker/profile.json")
            })
    });
    let paths = ContextPaths::load(config)?;
    let command = if cli.preflight {
        Command::Preflight
    } else {
        cli.command.unwrap_or(Command::Run)
    };
    let value = match command {
        Command::SetupRuntime => guerrilla_worker::runtime_setup::install(&paths).await?,
        Command::Status => serde_json::to_value(daemon::status(&paths)?)?,
        Command::Preflight => {
            let report = paths.preflight().await?;
            if cli.preflight {
                serde_json::to_value(ContextPaths::registration(&report)?)?
            } else {
                report
            }
        }
        Command::Enroll { token_stdin } => {
            let mut token = std::env::var("WORKER_ENROLLMENT_TOKEN").ok();
            std::env::remove_var("WORKER_ENROLLMENT_TOKEN");
            if token.is_none() && token_stdin {
                let mut line = String::new();
                BufReader::new(tokio::io::stdin().take(4098))
                    .read_line(&mut line)
                    .await?;
                token = Some(line.trim().to_owned());
            }
            serde_json::to_value(
                daemon::enroll(
                    &paths,
                    token
                        .as_deref()
                        .context("Use enroll --token-stdin and paste a token from Workers")?,
                )
                .await?,
            )?
        }
        Command::Start => serde_json::to_value(daemon::start(&paths).await?)?,
        Command::Stop => serde_json::to_value(daemon::control(&paths, "stop").await?)?,
        Command::Drain => serde_json::to_value(daemon::control(&paths, "drain").await?)?,
        Command::Disconnect => serde_json::to_value(daemon::disconnect(&paths).await?)?,
        Command::Logs => daemon::logs(&paths)?,
        Command::Run => {
            if paths.credentials()?.is_none() {
                if let Ok(token) = std::env::var("WORKER_ENROLLMENT_TOKEN") {
                    std::env::remove_var("WORKER_ENROLLMENT_TOKEN");
                    daemon::enroll(&paths, &token).await?;
                }
            }
            std::env::remove_var("WORKER_ENROLLMENT_TOKEN");
            daemon::run(paths).await?;
            return Ok(());
        }
    };
    println!("{}", serde_json::to_string(&value)?);
    Ok(())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let directory = std::env::args_os()
        .nth(1)
        .map(std::path::PathBuf::from)
        .ok_or_else(|| anyhow::anyhow!("Usage: guerrilla-runtime DATA_DIRECTORY"))?;
    let runtime = guerrilla_runtime::install(std::path::Path::new(""), &directory, |message| {
        eprintln!("{message}")
    })
    .await?;
    println!(
        "{}",
        serde_json::json!({"root":runtime.root,"executable":runtime.executable,"model":runtime.model})
    );
    Ok(())
}

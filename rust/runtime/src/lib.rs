//! Pinned native Spirula and reconstruction weights, shared by desktop and CLI.
use anyhow::{bail, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

pub const VERSION: &str = "2026.10.16";
pub const MODEL_SHA256: &str = "1557dec0d21b62366465f7ff4d5fdf228cc695d0582e196ad2b80e05230828b7";
pub const MODEL_URL: &str =
    "https://github.com/Parskatt/RoMaV2/releases/download/v2.0.1/romav2.0.1.pt";
const MODEL_BYTES: u64 = 1_095_883_548;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupStatus {
    pub supported: bool,
    pub installed: bool,
}
#[derive(Clone, Debug)]
pub struct Runtime {
    pub root: PathBuf,
    pub executable: PathBuf,
    pub model: PathBuf,
}
#[derive(Serialize, Deserialize)]
struct Manifest {
    version: String,
    target: String,
    executable: String,
    executable_sha256: String,
    model_sha256: String,
}

pub fn target() -> &'static str {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "darwin-arm64"
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        "win32-x64"
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        "linux-x64"
    } else {
        "unsupported"
    }
}
fn asset() -> Result<(String, &'static str)> {
    let (platform, sha) = match target() {
        "darwin-arm64" => (
            "macos-vulkan-arm64.dmg",
            "08b332e833ecafcc636e1be6b38dd0a732c6202a0fcf41d2c84209be5fdd6f03",
        ),
        "win32-x64" => (
            "windows-vulkan-x86_64.zip",
            "9b83ba6d78a4ace562d2ad2f964d2e3eb15aaa7a78918eab54e44d8a98a649d0",
        ),
        "linux-x64" => (
            "ubuntu-vulkan-x86_64.zip",
            "df5bb7acbfea63c16497c229b70c7a05ed13c22807554dee83cd6415aa411bf3",
        ),
        _ => bail!("Spirula requires Apple Silicon macOS or x64 Windows/Linux"),
    };
    Ok((format!("https://github.com/harry7557558/spirula-studio/releases/download/v{VERSION}/spirula-{VERSION}-{platform}"), sha))
}
pub fn hash(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut bytes = vec![0; 1024 * 1024];
    loop {
        let size = file.read(&mut bytes)?;
        if size == 0 {
            break;
        }
        hash.update(&bytes[..size]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn read_runtime(root: &Path) -> Option<Runtime> {
    if fs::symlink_metadata(root).ok()?.file_type().is_symlink() {
        return None;
    }
    let m: Manifest = serde_json::from_slice(&fs::read(root.join("manifest.json")).ok()?).ok()?;
    if m.version != VERSION
        || m.target != target()
        || m.model_sha256 != MODEL_SHA256
        || Path::new(&m.executable)
            .components()
            .any(|c| !matches!(c, std::path::Component::Normal(_)))
    {
        return None;
    }
    let runtime = Runtime {
        root: root.into(),
        executable: root.join(&m.executable),
        model: root.join("models/romav2.0.1.pt"),
    };
    if !runtime.executable.is_file() || fs::metadata(&runtime.model).ok()?.len() != MODEL_BYTES {
        return None;
    }
    Some(runtime)
}
pub fn installed(base: &Path, data: &Path) -> Option<PathBuf> {
    read_runtime(&data.join("runtimes").join(format!("spirula-{VERSION}")))
        .or_else(|| read_runtime(base))
        .map(|r| r.root)
}
pub fn runtime(base: &Path, data: &Path) -> Option<Runtime> {
    installed(base, data).and_then(|root| read_runtime(&root))
}
pub fn status(base: &Path, data: &Path) -> SetupStatus {
    SetupStatus {
        supported: target() != "unsupported",
        installed: installed(base, data).is_some(),
    }
}
fn verified(runtime: &Runtime) -> Result<bool> {
    let m: Manifest = serde_json::from_slice(&fs::read(runtime.root.join("manifest.json"))?)?;
    Ok(hash(&runtime.executable)? == m.executable_sha256 && hash(&runtime.model)? == MODEL_SHA256)
}
async fn download(
    client: &reqwest::Client,
    url: &str,
    digest: &str,
    cache: &Path,
    progress: &impl Fn(&str),
) -> Result<PathBuf> {
    fs::create_dir_all(cache)?;
    let file = cache.join(format!(
        "{digest}-{}",
        url.rsplit('/').next().context("Invalid asset URL")?
    ));
    if file.is_file() && hash(&file)? == digest {
        return Ok(file);
    }
    progress(&format!(
        "Downloading {}",
        url.rsplit('/').next().unwrap_or("runtime")
    ));
    let mut temporary = tempfile::NamedTempFile::new_in(cache)?;
    let mut response = client.get(url).send().await?.error_for_status()?;
    let mut hash = Sha256::new();
    while let Some(chunk) = response.chunk().await? {
        temporary.write_all(&chunk)?;
        hash.update(&chunk);
    }
    if format!("{:x}", hash.finalize()) != digest {
        bail!("Runtime download checksum mismatch");
    }
    temporary.as_file().sync_all()?;
    temporary.persist(&file).map_err(|e| e.error)?;
    Ok(file)
}
fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    for item in fs::read_dir(source)? {
        let item = item?;
        let path = item.path();
        let output = destination.join(item.file_name());
        if item.file_type()?.is_dir() {
            copy_tree(&path, &output)?;
        } else if item.file_type()?.is_symlink() {
            #[cfg(unix)]
            std::os::unix::fs::symlink(fs::read_link(&path)?, output)?;
            #[cfg(not(unix))]
            bail!("Unexpected runtime symbolic link");
        } else {
            fs::copy(path, output)?;
        }
    }
    Ok(())
}
fn extract(archive: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    if cfg!(target_os = "macos") {
        let mount = tempfile::tempdir()?;
        let output = std::process::Command::new("hdiutil")
            .args(["attach", "-readonly", "-nobrowse", "-mountpoint"])
            .arg(mount.path())
            .arg(archive)
            .output()?;
        if !output.status.success() {
            bail!(
                "Cannot mount Spirula disk image: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        let result = copy_tree(
            &mount.path().join("Spirula Studio.app"),
            &destination.join("Spirula Studio.app"),
        );
        let detached = std::process::Command::new("hdiutil")
            .args(["detach", "-quiet"])
            .arg(mount.path())
            .status()?;
        result?;
        if !detached.success() {
            bail!("Cannot detach Spirula disk image");
        }
    } else {
        let mut archive = zip::ZipArchive::new(fs::File::open(archive)?)?;
        archive.extract(destination)?;
    }
    Ok(())
}
fn find_executable(root: &Path) -> Result<PathBuf> {
    let name = if cfg!(windows) {
        "spirula.exe"
    } else {
        "spirula"
    };
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let path = entry.path();
        if entry.file_type()?.is_dir() {
            if let Ok(found) = find_executable(&path) {
                return Ok(found);
            }
        } else if entry.file_name() == name {
            return Ok(path);
        }
    }
    bail!("Spirula archive is missing its executable")
}
pub async fn install(base: &Path, data: &Path, progress: impl Fn(&str)) -> Result<Runtime> {
    asset()?;
    let parent = data.join("runtimes");
    fs::create_dir_all(&parent)?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(parent.join(".spirula-install.lock"))?;
    lock.lock_exclusive()?;
    if let Some(runtime) = runtime(base, data) {
        if verified(&runtime)? {
            progress("Spirula and reconstruction model verified");
            return Ok(runtime);
        }
    }
    let client = reqwest::Client::builder()
        .https_only(true)
        .user_agent("Guerrilla-Worker")
        .connect_timeout(Duration::from_secs(30))
        .timeout(Duration::from_secs(1800))
        .build()?;
    let cache = data.join("downloads");
    let (url, digest) = asset()?;
    let archive = download(&client, &url, digest, &cache, &progress).await?;
    let model = download(&client, MODEL_URL, MODEL_SHA256, &cache, &progress).await?;
    progress("Installing native Vulkan runtime");
    let staging = tempfile::tempdir_in(&parent)?;
    extract(&archive, &staging.path().join("engine"))?;
    let executable = find_executable(&staging.path().join("engine"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755))?;
    }
    fs::create_dir(staging.path().join("models"))?;
    fs::copy(model, staging.path().join("models/romav2.0.1.pt"))?;
    let manifest = Manifest {
        version: VERSION.into(),
        target: target().into(),
        executable: executable
            .strip_prefix(staging.path())?
            .to_string_lossy()
            .replace('\\', "/"),
        executable_sha256: hash(&executable)?,
        model_sha256: MODEL_SHA256.into(),
    };
    fs::write(
        staging.path().join("manifest.json"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    let destination = parent.join(format!("spirula-{VERSION}"));
    let previous = parent.join(format!(".spirula-{VERSION}-previous"));
    if previous.exists() {
        fs::remove_dir_all(&previous)?;
    }
    if destination.exists() {
        fs::rename(&destination, &previous)?;
    }
    if let Err(error) = fs::rename(staging.path(), &destination) {
        if previous.exists() {
            let _ = fs::rename(&previous, &destination);
        }
        return Err(error.into());
    }
    if previous.exists() {
        fs::remove_dir_all(previous)?;
    }
    read_runtime(&destination).context("Native runtime installation is incomplete")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sha256_streams_complete_files() {
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("payload");
        fs::write(&file, b"abc").unwrap();
        assert_eq!(
            hash(&file).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        fs::write(&file, b"changed").unwrap();
        assert_ne!(
            hash(&file).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
    #[test]
    fn incomplete_or_wrong_version_runtime_is_never_selected() {
        let directory = tempfile::tempdir().unwrap();
        let runtime = directory
            .path()
            .join("runtimes")
            .join(format!("spirula-{VERSION}"));
        fs::create_dir_all(&runtime).unwrap();
        let manifest = Manifest {
            version: "2026.9.30".into(),
            target: target().into(),
            executable: "engine/spirula".into(),
            executable_sha256: "a".repeat(64),
            model_sha256: MODEL_SHA256.into(),
        };
        fs::write(
            runtime.join("manifest.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        assert!(installed(Path::new("missing"), directory.path()).is_none());
        assert!(!status(Path::new("missing"), directory.path()).installed);
    }
    #[tokio::test]
    async fn verified_cached_download_works_without_a_network_connection() {
        let directory = tempfile::tempdir().unwrap();
        let payload = b"verified runtime archive";
        let digest = format!("{:x}", Sha256::digest(payload));
        let cached = directory.path().join(format!("{digest}-runtime.zip"));
        fs::write(&cached, payload).unwrap();
        let client = reqwest::Client::builder().https_only(true).build().unwrap();
        let restored = download(
            &client,
            "https://example.invalid/runtime.zip",
            &digest,
            directory.path(),
            &|_| panic!("cached asset must not download"),
        )
        .await
        .unwrap();
        assert_eq!(restored, cached);
        assert_eq!(fs::read(restored).unwrap(), payload);
    }
    #[tokio::test]
    async fn corrupt_download_never_publishes_cached_or_partial_bytes() {
        let cache = tempfile::tempdir().unwrap();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/runtime.zip", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut header = Vec::new();
            while !header.ends_with(b"\r\n\r\n") {
                let mut byte = [0];
                socket.read_exact(&mut byte).unwrap();
                header.push(byte[0]);
                assert!(header.len() < 8192);
            }
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nbad")
                .unwrap();
        });
        let expected = format!("{:x}", Sha256::digest(b"good"));
        let result = download(
            &reqwest::Client::new(),
            &url,
            &expected,
            cache.path(),
            &|_| {},
        )
        .await;
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("checksum mismatch"));
        server.join().unwrap();
        assert_eq!(fs::read_dir(cache.path()).unwrap().count(), 0);
    }
    #[test]
    fn repaired_profile_runtime_takes_precedence_over_a_bundled_installation() {
        fn create(root: &Path) {
            fs::create_dir_all(root.join("engine")).unwrap();
            fs::create_dir_all(root.join("models")).unwrap();
            fs::write(root.join("engine/spirula"), b"fixture").unwrap();
            fs::File::create(root.join("models/romav2.0.1.pt"))
                .unwrap()
                .set_len(MODEL_BYTES)
                .unwrap();
            let manifest = Manifest {
                version: VERSION.into(),
                target: target().into(),
                executable: "engine/spirula".into(),
                executable_sha256: "a".repeat(64),
                model_sha256: MODEL_SHA256.into(),
            };
            fs::write(
                root.join("manifest.json"),
                serde_json::to_vec(&manifest).unwrap(),
            )
            .unwrap();
        }
        let temporary = tempfile::tempdir().unwrap();
        let bundled = temporary.path().join("bundle");
        let data = temporary.path().join("profile");
        let repaired = data.join("runtimes").join(format!("spirula-{VERSION}"));
        create(&bundled);
        assert_eq!(installed(&bundled, &data), Some(bundled.clone()));
        create(&repaired);
        assert_eq!(installed(&bundled, &data), Some(repaired));
    }
}

use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Profile {
    pub control_url: String,
    pub scratch_directory: String,
    #[serde(default)]
    pub ffmpeg_path: String,
    #[serde(default)]
    pub node_path: String,
    #[serde(default)]
    pub converter_path: String,
    #[serde(default)]
    pub start_at_login: bool,
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

pub fn load_profile(path: &Path, directory: &Path) -> Result<Profile, String> {
    let recovery = path.with_extension("json.previous");
    let source = if path.exists() {
        Some(path)
    } else if recovery.exists() {
        Some(recovery.as_path())
    } else {
        None
    };
    let raw: serde_json::Value = match source {
        Some(path) => serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|e| format!("Cannot read worker profile: {e}"))?,
        None => {
            serde_json::json!({ "controlUrl": "https://guerrilla.dad", "scratchDirectory": directory.join("scratch"), "startAtLogin": false })
        }
    };
    serde_json::from_value(raw).map_err(|e| format!("Cannot read worker profile: {e}"))
}

pub fn normalize_server(value: &str) -> Result<String, String> {
    let url = url::Url::parse(value.trim()).map_err(|_| "Enter a server URL.")?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(
            "Enter the server's root HTTP(S) URL without a path, credentials, query or fragment."
                .into(),
        );
    }
    if url.scheme() != "https"
        && !matches!(
            url.host_str(),
            Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
        )
    {
        return Err("Use HTTPS for your server, or HTTP for localhost development.".into());
    }
    Ok(url.origin().ascii_serialization())
}

pub fn default_directory() -> Result<PathBuf, String> {
    if let Some(directory) = std::env::var_os("GUERRILLA_WORKER_DATA_DIR") {
        return override_directory(&directory);
    }
    #[cfg(target_os = "windows")]
    let root = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or("LOCALAPPDATA is unavailable")?
        .join("Guerrilla");
    #[cfg(target_os = "macos")]
    let root = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("Home directory is unavailable")?
        .join("Library/Application Support/Guerrilla");
    #[cfg(target_os = "linux")]
    let root = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/share")))
        .ok_or("User data directory is unavailable")?
        .join("guerrilla");
    Ok(root.join("worker"))
}

fn override_directory(value: &std::ffi::OsStr) -> Result<PathBuf, String> {
    use std::path::Component;
    let path = PathBuf::from(value);
    if !path.is_absolute()
        || path.parent().is_none()
        || !path
            .components()
            .any(|part| matches!(part, Component::Normal(_)))
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
    {
        return Err("GUERRILLA_WORKER_DATA_DIR must be an absolute folder path below a filesystem root, without parent traversal.".into());
    }
    Ok(path)
}

pub fn secure_directory(path: &Path) -> Result<(), String> {
    if fs::symlink_metadata(path).is_ok_and(|entry| entry.file_type().is_symlink()) {
        return Err(
            "The worker data directory must be a real folder, not a filesystem link.".into(),
        );
    }
    fs::create_dir_all(path).map_err(|e| format!("Cannot create worker data directory: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    #[cfg(windows)]
    {
        // The old tray worker used this same owner + SYSTEM boundary.
        let user = std::process::Command::new("whoami")
            .output()
            .map_err(|e| e.to_string())?;
        if !user.status.success() {
            return Err("Cannot determine current Windows user".into());
        }
        let user = String::from_utf8_lossy(&user.stdout).trim().to_owned();
        let result = std::process::Command::new("icacls")
            .arg(path)
            .args([
                "/inheritance:r",
                "/grant:r",
                &format!("{user}:(OI)(CI)F"),
                "*S-1-5-18:(OI)(CI)F",
            ])
            .output()
            .map_err(|e| e.to_string())?;
        if !result.status.success() {
            return Err("Cannot restrict worker data to your Windows account".into());
        }
    }
    Ok(())
}

pub fn atomic_save(path: &Path, profile: &Profile) -> Result<(), String> {
    let temp = path.with_extension("json.tmp");
    let data = serde_json::to_vec_pretty(profile).map_err(|e| e.to_string())?;
    use std::io::Write;
    let mut options = fs::OpenOptions::new();
    options.create(true).truncate(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temp).map_err(|e| e.to_string())?;
    file.write_all(&data)
        .and_then(|_| file.sync_all())
        .map_err(|e| e.to_string())?;
    drop(file);
    // Windows rename does not replace an existing file. Keep a recoverable backup
    // across replacement, rather than deleting the sole saved profile.
    #[cfg(windows)]
    {
        let backup = path.with_extension("json.previous");
        if path.exists() {
            if backup.exists() {
                fs::remove_file(&backup).map_err(|e| e.to_string())?;
            }
            fs::rename(path, &backup).map_err(|e| e.to_string())?;
        }
        if let Err(e) = fs::rename(&temp, path) {
            if backup.exists() {
                let _ = fs::rename(&backup, path);
            }
            return Err(e.to_string());
        }
        if backup.exists() {
            fs::remove_file(backup).map_err(|e| e.to_string())?;
        }
    }
    #[cfg(not(windows))]
    fs::rename(&temp, path).map_err(|e| e.to_string())?;
    Ok(())
}

pub fn bound_server(directory: &Path) -> Result<Option<String>, String> {
    let path = directory.join("credentials.json");
    if !path.exists() {
        return Ok(None);
    }
    let value: serde_json::Value =
        serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?).map_err(|_| {
            "Saved enrollment is invalid. Disconnect before changing servers.".to_string()
        })?;
    let origin = value
        .get("origin")
        .and_then(|v| v.as_str())
        .ok_or("Saved enrollment has no server origin")?;
    normalize_server(origin).map(Some)
}

pub fn validate_paths(profile: &Profile) -> Result<(), String> {
    let scratch = Path::new(&profile.scratch_directory);
    if !scratch.is_absolute() || (scratch.exists() && !scratch.is_dir()) {
        return Err("Choose an absolute scratch folder path.".into());
    }
    if fs::symlink_metadata(scratch).is_ok_and(|entry| entry.file_type().is_symlink()) {
        return Err("Choose a real scratch folder, not a filesystem link.".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn isolated_data_directory_rejects_relative_root_and_parent_traversal() {
        for path in ["", "relative/worker", "/", "/tmp/..", "/tmp/../worker"] {
            assert!(override_directory(std::ffi::OsStr::new(path)).is_err());
        }
        let expected = std::env::temp_dir().join("guerrilla-isolated-profile");
        assert_eq!(override_directory(expected.as_os_str()).unwrap(), expected);
    }
    #[test]
    fn server_url_cannot_carry_a_secret_or_non_web_scheme() {
        for url in [
            "file:///tmp/a",
            "https://token@example.com",
            "https://a.test?token=abc",
            "https://a.test/#secret",
            "https://a.test/base",
            "http://a.test",
        ] {
            assert!(normalize_server(url).is_err());
        }
        assert_eq!(
            normalize_server(" https://example.com/ ").unwrap(),
            "https://example.com"
        );
        assert_eq!(
            normalize_server("http://localhost:3000/").unwrap(),
            "http://localhost:3000"
        );
    }
    #[test]
    fn absent_engines_are_allowed_but_relative_scratch_is_not() {
        let mut p: Profile = serde_json::from_value(serde_json::json!({ "controlUrl":"https://a.test", "scratchDirectory":std::env::temp_dir() })).unwrap();
        assert!(validate_paths(&p).is_ok());
        p.scratch_directory = "scratch".into();
        assert!(validate_paths(&p).is_err());
    }
}

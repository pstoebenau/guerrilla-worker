use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{BufRead, BufReader, Read, Write},
    path::{Component, Path, PathBuf},
};
use walkdir::WalkDir;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Artifact {
    pub path: String,
    pub size: u64,
    pub sha256: String,
}

pub fn hash(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0; 1024 * 128];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

pub fn save(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path.parent().context("JSON path has no parent")?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap().to_string_lossy(),
        uuid::Uuid::new_v4()
    ));
    let result = (|| {
        let mut file = File::options()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        for attempt in 0..6 {
            match fs::rename(&temporary, path) {
                Ok(()) => {
                    #[cfg(unix)]
                    File::open(parent)?.sync_all()?;
                    return Ok(());
                }
                Err(error)
                    if attempt < 5 && error.kind() == std::io::ErrorKind::PermissionDenied =>
                {
                    std::thread::sleep(std::time::Duration::from_millis(50 << attempt))
                }
                Err(error) => return Err(error.into()),
            }
        }
        unreachable!()
    })();
    let _ = fs::remove_file(&temporary);
    result
}

pub fn safe(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = Path::new(relative);
    ensure!(
        root.is_absolute()
            && !relative.is_empty()
            && relative.len() <= 1024
            && !relative
                .bytes()
                .any(|byte| byte <= 31 || byte == b'\\' || byte == b':')
            && !path.is_absolute(),
        "Artifact path must be relative"
    );
    ensure!(
        path.components()
            .all(|part| matches!(part, Component::Normal(_))),
        "Artifact path escapes output"
    );
    ensure!(
        relative.split('/').all(|part| {
            let stem = part
                .split('.')
                .next()
                .unwrap_or_default()
                .to_ascii_lowercase();
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.ends_with(['.', ' '])
                && !matches!(stem.as_str(), "con" | "prn" | "aux" | "nul")
                && !(stem.len() == 4
                    && (stem.starts_with("com") || stem.starts_with("lpt"))
                    && matches!(stem.as_bytes()[3], b'1'..=b'9'))
        }),
        "Artifact path is not portable"
    );
    let mut current = root.to_path_buf();
    ensure!(!root.is_symlink(), "Artifact root is a symlink");
    for part in path.components() {
        current.push(part);
        ensure!(!current.is_symlink(), "Artifact path contains a symlink");
    }
    Ok(current)
}

pub fn files(root: &Path) -> Result<Vec<PathBuf>> {
    let mut result = Vec::new();
    if !root.exists() {
        return Ok(result);
    }
    for item in WalkDir::new(root).follow_links(false) {
        let item = item?;
        ensure!(
            !item.file_type().is_symlink(),
            "Artifact symlinks are not permitted: {}",
            item.path().display()
        );
        if item
            .path()
            .strip_prefix(root)?
            .components()
            .any(|part| matches!(part.as_os_str().to_str(), Some(".worker" | ".archive-acks")))
        {
            continue;
        }
        if item.file_type().is_file()
            && !matches!(
                item.file_name().to_str(),
                Some("platform-state.json" | "artifact-manifest.json")
            )
        {
            result.push(item.into_path());
        }
    }
    result.sort();
    Ok(result)
}

pub fn manifest(root: &Path, folder: &Path) -> Result<Vec<Artifact>> {
    files(folder)?
        .into_iter()
        .map(|path| {
            Ok(Artifact {
                path: relative(root, &path)?,
                size: path.metadata()?.len(),
                sha256: hash(&path)?,
            })
        })
        .collect()
}
pub fn relative(root: &Path, path: &Path) -> Result<String> {
    Ok(path
        .strip_prefix(root)?
        .to_str()
        .context("Non-UTF8 artifact path")?
        .replace('\\', "/"))
}
pub fn verify(root: &Path, items: &[Artifact]) -> Result<()> {
    for item in items {
        let path = safe(root, &item.path)?;
        ensure!(
            path.is_file() && path.metadata()?.len() == item.size && hash(&path)? == item.sha256,
            "Completed artifact changed: {}",
            item.path
        );
    }
    Ok(())
}
pub fn copy_tree(source: &Path, target: &Path) -> Result<()> {
    ensure!(!source.is_symlink(), "Cannot copy a symlink");
    if source.is_file() {
        fs::create_dir_all(target.parent().unwrap())?;
        fs::copy(source, target)?;
        File::open(target)?.sync_all()?;
        return Ok(());
    }
    for item in WalkDir::new(source).follow_links(false) {
        let item = item?;
        ensure!(!item.file_type().is_symlink(), "Cannot copy a symlink");
        let destination = target.join(item.path().strip_prefix(source)?);
        if item.file_type().is_dir() {
            fs::create_dir_all(destination)?;
        } else if item.file_type().is_file() {
            fs::copy(item.path(), &destination)?;
            File::open(destination)?.sync_all()?;
        }
    }
    Ok(())
}

pub fn gaussian_count(path: &Path, cap: u64) -> Result<u64> {
    let count;
    if path.extension().and_then(|s| s.to_str()) == Some("sog") {
        let mut archive = zip::ZipArchive::new(File::open(path)?)?;
        let meta: Value = {
            let file = archive.by_name("meta.json")?;
            ensure!(file.size() <= 1024 * 1024, "SOG metadata too large");
            serde_json::from_reader(file)?
        };
        count = meta["count"]
            .as_u64()
            .context("SOG Gaussian count is not a positive integer")?;
        for field in ["means", "scales", "quats", "sh0"] {
            let textures = meta[field]["files"]
                .as_array()
                .context("Missing SOG textures")?;
            ensure!(!textures.is_empty(), "Empty SOG texture set");
            for name in textures {
                let name = name.as_str().context("Invalid SOG texture name")?;
                let mut texture = archive.by_name(name)?;
                ensure!(texture.size() > 0, "Empty SOG texture");
                // Reading to EOF verifies CRC, not merely the central directory.
                std::io::copy(&mut texture, &mut std::io::sink())?;
            }
        }
    } else {
        let mut file = BufReader::new(File::open(path)?);
        let mut line = String::new();
        file.read_line(&mut line)?;
        ensure!(line.trim() == "ply", "Expected a PLY file");
        let mut vertices = None;
        let mut ended = false;
        for _ in 0..200 {
            line.clear();
            file.by_ref().take(4097).read_line(&mut line)?;
            ensure!(line.len() <= 4096 && !line.is_empty(), "Invalid PLY header");
            if let Some(value) = line.trim().strip_prefix("element vertex ") {
                vertices = Some(value.parse::<u64>()?);
            }
            if line.trim() == "end_header" {
                ended = true;
                break;
            }
        }
        ensure!(ended, "Invalid PLY header");
        count = vertices.context("PLY vertex count missing")?;
    }
    ensure!(
        count > 0 && count <= cap,
        "Gaussian count {count} is outside 1..{cap}"
    );
    Ok(count)
}

pub fn excluded(path: &str, prefixes: &[String]) -> bool {
    prefixes
        .iter()
        .any(|prefix| path == prefix || path.starts_with(&(prefix.clone() + "/")))
}

pub fn validate_settings(backend: &str, settings: &Value, cap: u64) -> Result<Value> {
    let supplied = settings.as_object().context("Settings must be an object")?;
    if backend == "spirula" {
        for (section, values) in supplied {
            ensure!(
                ["extraction", "reconstruction", "training"].contains(&section.as_str()),
                "Unknown Spirula settings section"
            );
            let values = values
                .as_object()
                .context("Settings sections must be objects")?;
            for (key, value) in values {
                let (low, high) = match (section.as_str(), key.as_str()) {
                    ("extraction", "skip" | "keep" | "max_frames") => (1., 100000.),
                    ("extraction", "quality") => (0., 100.),
                    ("extraction", "scale") => (0.01, 1.),
                    ("training", "iterations") => (1., 1000000.),
                    _ => bail!("Unsupported Spirula setting {section}.{key}"),
                };
                ensure!(
                    key == "scale" || value.as_u64().is_some(),
                    "{key} must be an integer"
                );
                let number = value.as_f64().context("Invalid numeric setting")?;
                ensure!(number >= low && number <= high, "Invalid {key}");
            }
        }
        return Ok(settings.clone());
    }
    ensure!(backend == "lichtfeld", "Unknown backend");
    let schema: Value =
        serde_json::from_str(include_str!("../../../pipeline/scan-settings.schema.json"))?;
    let mut result = json!({});
    for (section, definition) in schema["properties"].as_object().unwrap() {
        result[section] = json!({});
        for (key, field) in definition["properties"].as_object().unwrap() {
            result[section][key] = field["default"].clone();
        }
    }
    for (section, values) in supplied {
        let definition = schema["properties"]
            .get(section)
            .context("Unknown settings section")?;
        for (key, value) in values
            .as_object()
            .context("Settings sections must be objects")?
        {
            let field = definition["properties"]
                .get(key)
                .context("Unknown setting")?;
            let kinds = match &field["type"] {
                Value::Array(kinds) => kinds.clone(),
                kind => vec![kind.clone()],
            };
            let valid = kinds.iter().any(|kind| match kind.as_str() {
                Some("null") => value.is_null(),
                Some("integer") => value.as_i64().is_some() || value.as_u64().is_some(),
                Some("number") => value.is_number(),
                Some("boolean") => value.is_boolean(),
                Some("string") => value.is_string(),
                _ => false,
            });
            ensure!(valid, "Wrong type for {section}.{key}");
            if let Some(number) = value.as_f64() {
                ensure!(
                    field["minimum"].as_f64().is_none_or(|v| number >= v)
                        && field["maximum"].as_f64().is_none_or(|v| number <= v),
                    "Out of range setting {key}"
                );
            }
            if let Some(choices) = field["enum"].as_array() {
                ensure!(choices.contains(value), "Invalid setting {key}");
            }
            result[section][key] = value.clone();
        }
    }
    if let Some(value) = supplied.get("training").and_then(|v| v.get("max_cap")) {
        ensure!(
            value.as_u64() == Some(cap),
            "Training max_cap must equal the requested maximum"
        );
    }
    result["training"]["max_cap"] = json!(cap);
    if let Some(end) = result["selection"]["end_seconds"].as_f64() {
        ensure!(
            end > result["selection"]["start_seconds"].as_f64().unwrap(),
            "End time must exceed start time"
        );
    }
    ensure!(
        !(result["selection"]["preserve_coverage"] == true
            && !result["selection"]["max_images"].is_null()),
        "Preserve coverage cannot be combined with a maximum frame count"
    );
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn traversal_and_symlink_paths_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        for path in [
            "../escape",
            "/absolute",
            "a/../../b",
            "a\\b",
            "",
            "a//b",
            "con.txt",
            "folder/file:stream",
            "trailing.",
            "space ",
            "COM1",
        ] {
            assert!(safe(root.path(), path).is_err(), "{path}");
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("/tmp", root.path().join("link")).unwrap();
            assert!(safe(root.path(), "link/file").is_err());
            assert!(files(root.path()).is_err());
        }
    }
    #[test]
    fn gaussian_caps_and_sog_texture_integrity_are_enforced() {
        let root = tempfile::tempdir().unwrap();
        let ply = root.path().join("scene.ply");
        fs::write(
            &ply,
            "ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nend_header\n0\n",
        )
        .unwrap();
        assert_eq!(gaussian_count(&ply, 4).unwrap(), 4);
        assert!(gaussian_count(&ply, 3).is_err());
        let sog = root.path().join("scene.sog");
        let mut archive = zip::ZipWriter::new(File::create(&sog).unwrap());
        let options = zip::write::SimpleFileOptions::default();
        archive.start_file("meta.json", options).unwrap();
        archive.write_all(br#"{"count":4,"means":{"files":["missing.webp"]},"scales":{"files":["missing.webp"]},"quats":{"files":["missing.webp"]},"sh0":{"files":["missing.webp"]}}"#).unwrap();
        archive.finish().unwrap();
        assert!(gaussian_count(&sog, 4).is_err());
    }
    #[test]
    fn settings_validate_types_ranges_unknown_fields_and_cross_field_rules() {
        assert!(
            validate_settings("lichtfeld", &json!({"training":{"iterations":true}}), 4).is_err()
        );
        assert!(validate_settings(
            "lichtfeld",
            &json!({"selection":{"preserve_coverage":true,"max_images":3}}),
            4
        )
        .is_err());
        assert!(validate_settings("lichtfeld", &json!({"training":{"max_cap":5}}), 4).is_err());
        assert!(validate_settings("spirula", &json!({"training":{"max_cap":4}}), 4).is_err());
        assert!(validate_settings("spirula", &json!({"extraction":{"skip":0}}), 4).is_err());
        assert_eq!(
            validate_settings("lichtfeld", &json!({}), 4).unwrap()["training"]["max_cap"],
            4
        );
    }
    #[test]
    fn manifest_detects_corruption_and_atomic_json_replaces_cleanly() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("artifact");
        fs::write(&file, b"bytes").unwrap();
        let items = manifest(root.path(), root.path()).unwrap();
        verify(root.path(), &items).unwrap();
        fs::write(file, b"other").unwrap();
        assert!(verify(root.path(), &items).is_err());
        let state = root.path().join("state.json");
        save(&state, &json!({"revision":1})).unwrap();
        save(&state, &json!({"revision":2})).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(state).unwrap()).unwrap()["revision"],
            2
        );
        assert!(!fs::read_dir(root.path()).unwrap().any(|path| path
            .unwrap()
            .path()
            .extension()
            .and_then(|s| s.to_str())
            == Some("tmp")));
    }
}

use anyhow::{ensure, Context, Result};
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

pub fn input_images(root: &Path) -> Result<Vec<PathBuf>> {
    Ok(files(root)?
        .into_iter()
        .filter(|path| {
            matches!(
                path.extension()
                    .and_then(|s| s.to_str())
                    .map(str::to_ascii_lowercase)
                    .as_deref(),
                Some("jpg" | "jpeg" | "png" | "webp" | "tif" | "tiff" | "bmp" | "exr")
            )
        })
        .collect())
}
pub fn input_hash(input: &Path) -> Result<String> {
    if input.is_file() {
        return hash(input);
    }
    let images = input_images(input)?;
    ensure!(
        images.len() >= 3,
        "At least three input images are required"
    );
    let mut digest = Sha256::new();
    for image in images {
        digest.update(image.strip_prefix(input)?.to_string_lossy().as_bytes());
        digest.update([0]);
        digest.update(hash(&image)?.as_bytes());
        digest.update([0]);
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
    let result: Result<()> = (|| {
        let mut file = File::options()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all().context("Syncing temporary JSON file")?;
        for attempt in 0..6 {
            match fs::rename(&temporary, path) {
                Ok(()) => {
                    #[cfg(unix)]
                    File::open(parent)?
                        .sync_all()
                        .context("Syncing JSON parent directory")?;
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
    result.with_context(|| format!("Saving JSON {}", path.display()))
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
    fn copy_file(source: &Path, target: &Path) -> Result<()> {
        // clonefile requires a new destination. Unlink an existing output
        // before cloning so replacing it cannot modify another shared copy.
        ensure!(!target.is_symlink(), "Cannot replace a symlink");
        if target.exists() {
            fs::remove_file(target)?;
        }
        reflink_copy::reflink_or_copy(source, target)
            .with_context(|| format!("Copying dataset file {}", target.display()))?;
        // Windows FlushFileBuffers requires a handle opened for writing.
        File::options()
            .write(true)
            .open(target)?
            .sync_all()
            .with_context(|| format!("Syncing dataset file {}", target.display()))?;
        Ok(())
    }
    ensure!(!source.is_symlink(), "Cannot copy a symlink");
    if source.is_file() {
        fs::create_dir_all(target.parent().unwrap())?;
        copy_file(source, target)?;
        return Ok(());
    }
    for item in WalkDir::new(source).follow_links(false) {
        let item = item?;
        ensure!(!item.file_type().is_symlink(), "Cannot copy a symlink");
        let destination = target.join(item.path().strip_prefix(source)?);
        if item.file_type().is_dir() {
            fs::create_dir_all(destination)?;
        } else if item.file_type().is_file() {
            copy_file(item.path(), &destination)?;
        }
    }
    Ok(())
}

pub fn gaussian_count(path: &Path, cap: u64) -> Result<u64> {
    let count;
    if path.extension().and_then(|s| s.to_str()) == Some("spz") {
        let mut stream = flate2::read::GzDecoder::new(File::open(path)?);
        let mut header = [0u8; 16];
        stream.read_exact(&mut header)?;
        let magic = u32::from_le_bytes(header[0..4].try_into().unwrap());
        let version = u32::from_le_bytes(header[4..8].try_into().unwrap());
        count = u32::from_le_bytes(header[8..12].try_into().unwrap()) as u64;
        let degree = header[12] as u64;
        ensure!(
            magic == 0x5053474e
                && (1..=3).contains(&version)
                && degree <= 3
                && header[13] <= 24
                && header[14] & !1 == 0
                && header[15] == 0,
            "Invalid SPZ header"
        );
        ensure!(
            count > 0 && count <= cap,
            "SPZ Gaussian count is outside 1..{cap}"
        );
        let stride = (if version == 1 { 6 } else { 9 })
            + 3
            + (if version == 3 { 4 } else { 3 })
            + 1
            + 3
            + ((degree + 1).pow(2) - 1) * 3;
        let expected = count * stride;
        let actual = std::io::copy(
            &mut Read::by_ref(&mut stream).take(expected + 1),
            &mut std::io::sink(),
        )?;
        ensure!(
            actual == expected,
            "SPZ attributes are truncated or have trailing data"
        );
    } else if path.extension().and_then(|s| s.to_str()) == Some("sog") {
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

pub fn validate_settings(backend: &str, settings: &Value, _cap: u64) -> Result<Value> {
    ensure!(backend == "spirula", "Unknown backend");
    let supplied = settings.as_object().context("Settings must be an object")?;
    let schema: Value = serde_json::from_str(include_str!(
        "../../../packages/protocol/scan-settings.schema.json"
    ))?;
    let properties = schema["properties"].as_object().unwrap();
    let mut result = json!({});
    for (section, definition) in properties {
        result[section] = json!({});
        for (key, field) in definition["properties"].as_object().unwrap() {
            result[section][key] = field["default"].clone();
        }
    }
    for (section, values) in supplied {
        let definition = properties
            .get(section)
            .context("Unknown settings section")?;
        for (key, value) in values
            .as_object()
            .context("Settings sections must be objects")?
        {
            let field = definition["properties"]
                .get(key)
                .context("Unknown setting")?;
            let valid = match field["type"].as_str() {
                Some("integer") => value.as_u64().is_some(),
                Some("number") => value.is_number(),
                Some("boolean") => value.is_boolean(),
                _ => false,
            };
            ensure!(valid, "Wrong type for {section}.{key}");
            if let Some(number) = value.as_f64() {
                ensure!(
                    field["minimum"].as_f64().is_none_or(|v| number >= v)
                        && field["maximum"].as_f64().is_none_or(|v| number <= v),
                    "Out of range setting {key}"
                );
            }
            result[section][key] = value.clone();
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn copied_dataset_is_independent_when_an_engine_changes_a_file() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("source");
        let target = temp.path().join("target");
        fs::create_dir_all(source.join("images")).unwrap();
        fs::write(source.join("images/frame.jpg"), b"original").unwrap();
        copy_tree(&source, &target).unwrap();
        fs::write(target.join("images/frame.jpg"), b"changed").unwrap();
        assert_eq!(
            fs::read(source.join("images/frame.jpg")).unwrap(),
            b"original"
        );
        copy_tree(&source, &target).unwrap();
        assert_eq!(
            fs::read(target.join("images/frame.jpg")).unwrap(),
            b"original"
        );
    }
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
    fn spz_validates_count_payload_length_and_gzip_integrity() {
        use flate2::{write::GzEncoder, Compression};
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("scene.spz");
        let mut payload = Vec::new();
        payload.extend_from_slice(&0x5053474eu32.to_le_bytes());
        payload.extend_from_slice(&2u32.to_le_bytes());
        payload.extend_from_slice(&4u32.to_le_bytes());
        payload.extend_from_slice(&[0, 12, 0, 0]);
        payload.resize(16 + 19 * 4, 0);
        let write = |bytes: &[u8]| {
            let mut stream = GzEncoder::new(File::create(&path).unwrap(), Compression::default());
            stream.write_all(bytes).unwrap();
            stream.finish().unwrap();
        };
        write(&payload);
        assert_eq!(gaussian_count(&path, 4).unwrap(), 4);
        assert!(gaussian_count(&path, 3).is_err());
        write(&payload[..payload.len() - 1]);
        assert!(gaussian_count(&path, 4).is_err());
        payload.push(0);
        write(&payload);
        assert!(gaussian_count(&path, 4).is_err());
        payload.pop();
        write(&payload);
        let mut compressed = fs::read(&path).unwrap();
        let checksum = compressed.len() - 8;
        compressed[checksum] ^= 1;
        fs::write(&path, compressed).unwrap();
        assert!(gaussian_count(&path, 4).is_err());
    }
    #[test]
    fn settings_validate_types_ranges_unknown_fields_and_cross_field_rules() {
        for settings in [
            json!({"training":{"iterations":true}}),
            json!({"training":{"max_cap":4}}),
            json!({"extraction":{"skip":0}}),
            json!({"densification":{"enabled":1}}),
            json!({"densification":{"preset":"fast"}}),
            json!({"reconstruction":{"mode":"global"}}),
        ] {
            assert!(validate_settings("spirula", &settings, 4).is_err());
        }
        assert_eq!(
            validate_settings("spirula", &json!({}), 4).unwrap()["densification"]["enabled"],
            true
        );
        assert_eq!(
            validate_settings("spirula", &json!({"densification":{"enabled":false}}), 4).unwrap()
                ["densification"]["enabled"],
            false
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

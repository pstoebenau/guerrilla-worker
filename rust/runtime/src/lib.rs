use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupStatus {
    pub supported: bool,
    pub installed: bool,
}

#[derive(Deserialize)]
struct Descriptor {
    identity: String,
    target: String,
    supported: bool,
    models: serde_json::Value,
}

pub fn python(root: &Path) -> PathBuf {
    root.join(if cfg!(windows) {
        "python/python.exe"
    } else {
        "python/bin/python3"
    })
}

fn descriptor(base: &Path) -> Option<Descriptor> {
    let value: Descriptor =
        serde_json::from_slice(&std::fs::read(base.join("setup/manifest.json")).ok()?).ok()?;
    let identity = value.identity.strip_prefix("cuda-")?;
    if identity.len() != 64
        || !identity
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return None;
    }
    Some(value)
}

pub fn installed(base: &Path, data: &Path) -> Option<PathBuf> {
    let expected = descriptor(base)?;
    if !expected.supported {
        return None;
    }
    let directory = data.join("runtimes").join(&expected.identity);
    if std::fs::symlink_metadata(&directory)
        .ok()?
        .file_type()
        .is_symlink()
    {
        return None;
    }
    let manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(directory.join("manifest.json")).ok()?).ok()?;
    let source = expected.models.get("dinov3Commit")?.as_str()?;
    if manifest["identity"] != expected.identity
        || manifest["target"] != expected.target
        || !python(&directory).is_file()
        || !directory.join("models/hub/checkpoints/romav2.pt").is_file()
        || !directory
            .join("models/hub")
            .join(format!("facebookresearch_dinov3_{source}"))
            .is_dir()
    {
        return None;
    }
    Some(directory)
}

pub fn status(base: &Path, data: &Path) -> SetupStatus {
    SetupStatus {
        supported: descriptor(base).is_some_and(|value| value.supported),
        installed: installed(base, data).is_some(),
    }
}

pub fn selected_python(base: &Path, data: &Path) -> PathBuf {
    python(&installed(base, data).unwrap_or_else(|| base.to_path_buf()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn only_a_complete_matching_runtime_is_selected_and_old_versions_are_preserved() {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().join("base");
        let data = temp.path().join("profile");
        std::fs::create_dir_all(base.join("setup")).unwrap();
        let identity = format!("cuda-{}", "a".repeat(64));
        std::fs::write(base.join("setup/manifest.json"), json!({"identity":identity,"target":"fixture", "supported":true,"models":{"dinov3Commit":"commit"}}).to_string()).unwrap();
        let runtime = data.join("runtimes").join(&identity);
        std::fs::create_dir_all(python(&runtime).parent().unwrap()).unwrap();
        std::fs::write(python(&runtime), b"python").unwrap();
        std::fs::write(
            runtime.join("manifest.json"),
            json!({"identity":identity,"target":"other"}).to_string(),
        )
        .unwrap();
        assert_eq!(selected_python(&base, &data), python(&base));
        std::fs::write(
            runtime.join("manifest.json"),
            json!({"identity":identity,"target":"fixture"}).to_string(),
        )
        .unwrap();
        assert!(!status(&base, &data).installed);
        std::fs::create_dir_all(runtime.join("models/hub/checkpoints")).unwrap();
        std::fs::write(runtime.join("models/hub/checkpoints/romav2.pt"), b"model").unwrap();
        std::fs::create_dir_all(runtime.join("models/hub/facebookresearch_dinov3_commit")).unwrap();
        assert_eq!(selected_python(&base, &data), python(&runtime));
        let mut next: serde_json::Value =
            serde_json::from_slice(&std::fs::read(base.join("setup/manifest.json")).unwrap())
                .unwrap();
        next["identity"] = json!(format!("cuda-{}", "b".repeat(64)));
        std::fs::write(base.join("setup/manifest.json"), next.to_string()).unwrap();
        assert!(!status(&base, &data).installed);
        assert!(runtime.join("manifest.json").is_file());
    }
}

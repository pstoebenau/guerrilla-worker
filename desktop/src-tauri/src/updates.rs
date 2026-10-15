use semver::Version;
use serde::{Deserialize, Serialize};
use std::time::Duration;

const RELEASES_API: &str =
    "https://api.github.com/repos/pstoebenau/guerrilla-worker/releases?per_page=100";
const RELEASES_URL: &str = "https://github.com/pstoebenau/guerrilla-worker/releases/tag/";

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AvailableUpdate {
    version: String,
    release_url: String,
}

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    assets: Vec<Asset>,
}

#[derive(Deserialize)]
struct Asset {
    name: String,
}

fn release_version(tag: &str, platform: &str) -> Option<Version> {
    let tag = tag.strip_prefix(&format!("{platform}-")).unwrap_or(tag);
    let version = Version::parse(tag.strip_prefix('v').unwrap_or(tag)).ok()?;
    version.pre.is_empty().then_some(version)
}

fn desktop_asset(name: &str, platform: &str, arch: &str) -> bool {
    let name = name.to_ascii_lowercase().replace([' ', '_'], "-");
    // Published large packages include a checksummed reassembly manifest.
    let name = name.strip_suffix(".parts.json").unwrap_or(&name);
    if !name.starts_with("guerrilla-worker") {
        return false;
    }
    // An unmarked or universal package is usable on either architecture.
    let arm = name.contains("aarch64") || name.contains("arm64");
    let x64 = name.contains("x86-64") || name.contains("amd64") || name.contains("x64");
    if (arm && arch != "aarch64") || (x64 && arch != "x86_64") {
        return false;
    }
    match platform {
        "windows" => name.ends_with("-setup.exe") || name.ends_with(".msi"),
        "linux" => [".appimage", ".deb", ".rpm"]
            .iter()
            .any(|ext| name.ends_with(ext)),
        "macos" => name.ends_with(".dmg") || name.ends_with(".app.tar.gz"),
        _ => false,
    }
}

fn available_update(
    releases: &[Release],
    current: &str,
    platform: &str,
    arch: &str,
) -> Option<AvailableUpdate> {
    let current = Version::parse(current).ok()?;
    releases
        .iter()
        .filter(|release| !release.draft && !release.prerelease)
        .filter(|release| {
            release
                .assets
                .iter()
                .any(|asset| desktop_asset(&asset.name, platform, arch))
        })
        .filter_map(|release| Some((release_version(&release.tag_name, platform)?, release)))
        .filter(|(version, _)| version.cmp_precedence(&current).is_gt())
        .max_by(|(a, _), (b, _)| a.cmp_precedence(b))
        .map(|(version, release)| AvailableUpdate {
            version: version.to_string(),
            release_url: format!("{RELEASES_URL}{}", release.tag_name),
        })
}

pub fn valid_release_url(url: &str) -> bool {
    // Only links to worker version releases may be opened by this command.
    url.strip_prefix(RELEASES_URL).is_some_and(|tag| {
        ["windows", "linux", "macos"]
            .iter()
            .any(|platform| release_version(tag, platform).is_some())
    })
}

pub async fn check() -> Result<Option<AvailableUpdate>, String> {
    let client = reqwest::Client::builder()
        .user_agent(concat!("Guerrilla-Worker/", env!("CARGO_PKG_VERSION")))
        .timeout(Duration::from_secs(15))
        .https_only(true)
        .build()
        .map_err(|e| e.to_string())?;
    let mut response = client
        .get(RELEASES_API)
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?;
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        if body.len() + chunk.len() > 4_000_000 {
            return Err("Release response is too large".into());
        }
        body.extend_from_slice(&chunk);
    }
    let releases: Vec<Release> = serde_json::from_slice(&body).map_err(|e| e.to_string())?;
    Ok(available_update(
        &releases,
        env!("CARGO_PKG_VERSION"),
        std::env::consts::OS,
        std::env::consts::ARCH,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn release(tag: &str, asset: &str) -> Release {
        Release {
            tag_name: tag.into(),
            draft: false,
            prerelease: false,
            assets: vec![Asset { name: asset.into() }],
        }
    }

    #[test]
    fn canonical_release_names_match_supported_platforms() {
        for (name, platform, arch) in [
            ("guerrilla-worker-0.3.5-macos-arm64.dmg", "macos", "aarch64"),
            ("guerrilla-worker-0.3.5-macos-arm64.app.tar.gz", "macos", "aarch64"),
            ("guerrilla-worker-0.3.5-ubuntu-x86_64.AppImage", "linux", "x86_64"),
            ("guerrilla-worker-0.3.5-ubuntu-x86_64.deb", "linux", "x86_64"),
            ("guerrilla-worker-0.3.5-ubuntu-x86_64.rpm", "linux", "x86_64"),
            ("guerrilla-worker-0.3.5-windows-x86_64-setup.exe", "windows", "x86_64"),
        ] {
            assert!(desktop_asset(name, platform, arch));
            let other_arch = if arch == "aarch64" { "x86_64" } else { "aarch64" };
            assert!(!desktop_asset(name, platform, other_arch));
        }
    }

    #[test]
    fn selects_newest_version_numerically_and_ignores_current_or_older() {
        let releases = [
            release(
                "windows-v0.3.9",
                "Guerrilla-Worker-0.3.9-windows-x64-setup.exe",
            ),
            release("v0.3.10", "Guerrilla-Worker-0.3.10-windows-x64-setup.exe"),
        ];
        let update = available_update(&releases, "0.3.0", "windows", "x86_64").unwrap();
        assert_eq!(update.version, "0.3.10");
        assert_eq!(update.release_url, format!("{RELEASES_URL}v0.3.10"));
        assert!(available_update(&releases, "0.3.10", "windows", "x86_64").is_none());
        assert!(available_update(&releases, "0.4.0", "windows", "x86_64").is_none());
        assert!(available_update(&[], "0.3.0", "windows", "x86_64").is_none());
    }

    #[test]
    fn ignores_unreleased_versions_engines_and_other_platforms() {
        let mut draft = release("v9.0.0", "Guerrilla Worker_9.0.0_aarch64.dmg");
        draft.draft = true;
        let mut prerelease = release("v8.0.0", "Guerrilla Worker_8.0.0_aarch64.dmg");
        prerelease.prerelease = true;
        let releases = [
            draft,
            prerelease,
            release("v7.0.0-beta.1", "Guerrilla Worker_7.0.0-beta.1_aarch64.dmg"),
            release(
                "lichtfeld-linux-recipe-abc",
                "Guerrilla Worker_6.0.0_aarch64.dmg",
            ),
            release(
                "windows-v5.0.0",
                "Guerrilla-Worker-5.0.0-windows-x64-setup.exe",
            ),
            release("linux-v4.0.0", "guerrilla-worker-4.0.0.tar.gz"),
            release("v3.0.0", "Guerrilla Worker_3.0.0_x64.dmg"),
            release("macos-v0.3.1", "Guerrilla Worker_0.3.1_aarch64.dmg"),
        ];
        assert_eq!(
            available_update(&releases, "0.3.0", "macos", "aarch64")
                .unwrap()
                .version,
            "0.3.1"
        );
        assert!(available_update(&releases, "0.3.0", "linux", "x86_64").is_none());
        assert!(release_version("windows-v9.0.0", "macos").is_none());
    }

    #[test]
    fn accepts_desktop_packages_and_limits_links_to_worker_releases() {
        for name in [
            "Guerrilla Worker_0.3.1_amd64.AppImage",
            "Guerrilla Worker_0.3.1_amd64.deb",
            "Guerrilla Worker-0.3.1-1.x86_64.rpm",
            "Guerrilla Worker_0.3.1_amd64.AppImage.parts.json",
        ] {
            assert!(desktop_asset(name, "linux", "x86_64"));
        }
        assert!(desktop_asset(
            "Guerrilla Worker.app.tar.gz",
            "macos",
            "aarch64"
        ));
        assert!(!desktop_asset(
            "Guerrilla Worker_0.3.1_amd64.AppImage.part001",
            "linux",
            "x86_64"
        ));
        assert!(!desktop_asset(
            "Guerrilla Worker_0.3.1_amd64.AppImage.parts.json",
            "linux",
            "aarch64"
        ));
        assert!(valid_release_url(&format!("{RELEASES_URL}windows-v0.3.1")));
        for url in [
            "https://example.com/v0.3.1",
            "file:///tmp/v0.3.1",
            "https://github.com/pstoebenau/guerrilla-worker/releases/tag/v0.3.1/../../issues",
            "https://github.com/pstoebenau/guerrilla-worker/releases/tag/v0.3.1?other=1",
        ] {
            assert!(!valid_release_url(url));
        }
    }
}

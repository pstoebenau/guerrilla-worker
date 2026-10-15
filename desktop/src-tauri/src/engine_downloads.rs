// Keep browser navigation limited to official engine distribution pages.
pub fn url(engine: &str, platform: &str) -> Result<&'static str, String> {
    match (engine, platform) {
        ("spirula", "windows" | "linux" | "macos") => {
            Ok("https://github.com/harry7557558/spirula-studio/releases/latest")
        }
        ("lichtfeld", "windows") => Ok("https://portal.lichtfeld.io/"),
        ("lichtfeld", "linux") => Ok(
            "https://github.com/MrNeRF/LichtFeld-Studio/blob/master/docs/building_and_distribution.md",
        ),
        ("densification", "windows" | "linux") => {
            Ok("https://github.com/shadygm/lichtfeld-densification-plugin")
        }
        _ => Err("This engine is not supported on this platform".into()),
    }
}

#[derive(serde::Deserialize)]
struct Release {
    assets: Vec<Asset>,
}
#[derive(serde::Deserialize)]
struct Asset {
    name: String,
    browser_download_url: String,
}

fn spirula_asset(release: &Release, platform: &str) -> Option<String> {
    let suffix = match platform {
        "windows" => "-windows-vulkan-x86_64.zip",
        "linux" => "-ubuntu-vulkan-x86_64.zip",
        "macos" => "-macos-vulkan-arm64.dmg",
        _ => return None,
    };
    release
        .assets
        .iter()
        .find(|asset| {
            asset.name.starts_with("spirula-")
                && asset.name.ends_with(suffix)
                && asset.browser_download_url.starts_with(
                    "https://github.com/harry7557558/spirula-studio/releases/download/",
                )
        })
        .map(|asset| asset.browser_download_url.clone())
}

async fn latest_spirula(platform: &str) -> Option<String> {
    let client = reqwest::Client::builder()
        .user_agent(concat!("Guerrilla-Worker/", env!("CARGO_PKG_VERSION")))
        .timeout(std::time::Duration::from_secs(8))
        .https_only(true)
        .build()
        .ok()?;
    let mut response = client
        .get("https://api.github.com/repos/harry7557558/spirula-studio/releases/latest")
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?;
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if body.len() + chunk.len() > 1_000_000 {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    let release: Release = serde_json::from_slice(&body).ok()?;
    spirula_asset(&release, platform)
}

pub async fn destination(engine: &str, platform: &str) -> Result<String, String> {
    let fallback = url(engine, platform)?;
    if engine == "spirula"
        && let Some(download) = latest_spirula(platform).await
    {
        return Ok(download);
    }
    Ok(fallback.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spirula_download_matches_the_host_os() {
        let release = Release { assets: [
            "spirula-2026.10.6-macos-vulkan-arm64.dmg",
            "spirula-2026.10.6-windows-vulkan-x86_64.zip",
            "spirula-2026.10.6-ubuntu-vulkan-x86_64.zip",
        ].iter().map(|name| Asset {
            name: name.to_string(),
            browser_download_url: format!("https://github.com/harry7557558/spirula-studio/releases/download/v2026.10.6/{name}")
        }).collect() };
        for (platform, expected) in [
            ("windows", "windows-vulkan"),
            ("linux", "ubuntu-vulkan"),
            ("macos", "macos-vulkan"),
        ] {
            assert!(
                spirula_asset(&release, platform)
                    .unwrap()
                    .contains(expected)
            );
        }
        assert!(spirula_asset(&release, "android").is_none());
        assert!(spirula_asset(&Release { assets: vec![] }, "macos").is_none());
    }

    #[test]
    fn links_follow_platform_support_and_reject_arbitrary_urls() {
        assert_eq!(
            url("lichtfeld", "windows").unwrap(),
            "https://portal.lichtfeld.io/"
        );
        assert!(
            url("lichtfeld", "linux")
                .unwrap()
                .ends_with("building_and_distribution.md")
        );
        assert!(url("lichtfeld", "macos").is_err());
        assert!(url("densification", "macos").is_err());
        for platform in ["windows", "linux", "macos"] {
            assert!(
                url("spirula", platform)
                    .unwrap()
                    .ends_with("/releases/latest")
            );
        }
        assert!(url("https://example.com", "windows").is_err());
    }
}

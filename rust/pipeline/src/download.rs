use crate::process::Runtime;
use anyhow::{bail, ensure, Context, Result};
use reqwest::{
    blocking::Client,
    header::{CONTENT_LENGTH, CONTENT_TYPE, LOCATION},
    Url,
};
use std::{
    fs::{self, File},
    io::{Read, Write},
    net::{IpAddr, ToSocketAddrs},
    path::Path,
    time::Duration,
};

fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let [a, b, c, _] = ip.octets();
            !ip.is_private()
                && !ip.is_loopback()
                && !ip.is_link_local()
                && !ip.is_unspecified()
                && !ip.is_multicast()
                && a != 0
                && a < 224
                && !(a == 100 && (64..=127).contains(&b))
                && !(a == 192 && b == 0 && (c == 0 || c == 2))
                && !(a == 198 && (b == 18 || b == 19 || b == 51 && c == 100))
                && !(a == 203 && b == 0 && c == 113)
        }
        IpAddr::V6(ip) => {
            if let Some(mapped) = ip.to_ipv4_mapped() {
                return public_ip(IpAddr::V4(mapped));
            }
            let first = ip.segments()[0];
            !ip.is_loopback()
                && !ip.is_unspecified()
                && !ip.is_multicast()
                && first & 0xfe00 != 0xfc00
                && first & 0xffc0 != 0xfe80
                && first & 0xe000 == 0x2000
                && !(ip.segments()[0] == 0x2001 && ip.segments()[1] == 0x0db8)
        }
    }
}
fn response(mut url: Url, public: bool, runtime: &Runtime) -> Result<reqwest::blocking::Response> {
    for _ in 0..10 {
        runtime.check()?;
        ensure!(
            matches!(url.scheme(), "http" | "https"),
            "Download URL must use HTTP or HTTPS"
        );
        ensure!(
            url.username().is_empty() && url.password().is_none(),
            "Download URL cannot include credentials"
        );
        let host = url.host_str().context("Download host missing")?;
        let addresses = (
            host,
            url.port_or_known_default()
                .context("Download port missing")?,
        )
            .to_socket_addrs()?
            .collect::<Vec<_>>();
        ensure!(!addresses.is_empty(), "Download host has no addresses");
        if public {
            ensure!(
                addresses.iter().all(|address| public_ip(address.ip())),
                "Public video URLs may not access private or reserved addresses"
            );
        }
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(60))
            .no_proxy()
            .resolve_to_addrs(host, &addresses)
            .build()?;
        let result = client.get(url.clone()).send()?;
        if result.status().is_redirection() {
            let location = result
                .headers()
                .get(LOCATION)
                .context("Redirect omitted location")?
                .to_str()?;
            url = url.join(location)?;
            continue;
        }
        return Ok(result.error_for_status()?);
    }
    bail!("Too many video redirects")
}
pub fn download(url: &str, destination: &Path, public: bool, runtime: &Runtime) -> Result<()> {
    ensure!(
        !destination.exists(),
        "Refusing to replace an existing download"
    );
    ensure!(
        !destination.is_symlink(),
        "Download destination is a symlink"
    );
    fs::create_dir_all(
        destination
            .parent()
            .context("Download destination has no parent")?,
    )?;
    let mut url = Url::parse(url)?;
    if matches!(
        url.host_str(),
        Some("photos.app.goo.gl" | "photos.google.com")
    ) {
        let mut page = response(url, public, runtime)?;
        let mut bytes = vec![];
        page.by_ref()
            .take(10 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= 10 * 1024 * 1024,
            "Google Photos page too large"
        );
        let text = String::from_utf8(bytes)?;
        let tags = regex::Regex::new(r"(?i)<meta\s+[^>]*>")?;
        let property = regex::Regex::new(r#"(?i)property\s*=\s*["']og:video["']"#)?;
        let content = regex::Regex::new(r#"(?i)content\s*=\s*["']([^"']+)["']"#)?;
        let media = tags
            .find_iter(&text)
            .find_map(|tag| {
                if property.is_match(tag.as_str()) {
                    content
                        .captures(tag.as_str())
                        .map(|cap| cap[1].replace("&amp;", "&"))
                } else {
                    None
                }
            })
            .context("Google Photos did not expose a downloadable video")?;
        url = Url::parse(media.split('=').next().unwrap())?;
        ensure!(
            url.scheme() == "https"
                && url
                    .host_str()
                    .is_some_and(|host| host.ends_with(".googleusercontent.com")),
            "Invalid Google Photos media URL"
        );
        url = Url::parse(&(url.to_string() + "=dv"))?;
    }
    let mut response = response(url, public, runtime)?;
    let content = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|header| header.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("");
    ensure!(
        content.starts_with("video/")
            || matches!(content, "application/octet-stream" | "binary/octet-stream"),
        "Expected a video download, received {content}"
    );
    let expected = response
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|header| header.to_str().ok())
        .map(str::parse::<u64>)
        .transpose()?;
    let temporary = destination.with_extension(format!(
        "{}.{}.part",
        destination
            .extension()
            .unwrap_or_default()
            .to_string_lossy(),
        uuid::Uuid::new_v4()
    ));
    let result = (|| {
        let mut file = File::options()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        let mut buffer = [0; 128 * 1024];
        let mut written = 0;
        loop {
            runtime.check()?;
            let read = response.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            file.write_all(&buffer[..read])?;
            written += read as u64;
        }
        ensure!(
            written > 0 && expected.is_none_or(|value| value == written),
            "Video download was empty or incomplete"
        );
        file.sync_all()?;
        fs::rename(&temporary, destination)?;
        Ok(())
    })();
    let _ = fs::remove_file(temporary);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn public_sources_reject_private_reserved_and_mapped_networks() {
        for address in [
            "127.0.0.1",
            "10.1.2.3",
            "169.254.1.2",
            "100.64.0.1",
            "192.0.2.1",
            "198.51.100.2",
            "203.0.113.2",
            "0.1.2.3",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "2001:db8::1",
        ] {
            assert!(!public_ip(address.parse().unwrap()), "{address}");
        }
        for address in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"] {
            assert!(public_ip(address.parse().unwrap()), "{address}");
        }
    }
}

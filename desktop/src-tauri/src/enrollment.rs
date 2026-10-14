use std::process::ExitStatus;

pub fn failure(stderr: &[u8], status: &ExitStatus) -> String {
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::process::ExitStatusExt;
        if status.signal() == Some(9) {
            return "Enrollment failed: macOS terminated the bundled worker. Open Guerrilla Worker from Applications and check System Settings > Privacy & Security for a blocked app.".into();
        }
    }
    let _ = status;
    let report: serde_json::Value = serde_json::from_slice(stderr).unwrap_or_default();
    let message = match report["errorCode"].as_str() {
        Some("invalid_token") => {
            "The server rejected the enrollment token. Create a fresh token on this server's Workers page and try again; tokens expire after 15 minutes and can only be used once."
        }
        Some("invalid_registration") => {
            "The server rejected this worker's identity. Confirm that the server and worker support the same protocol."
        }
        Some("wrong_server") => {
            "The server has no worker enrollment endpoint. Check the Server URL in Setup."
        }
        Some("runtime_mismatch") => {
            "The server rejected this worker's runtime identity. Check the required build and runtime for this worker."
        }
        Some("rate_limited") => {
            "The server is limiting enrollment requests. Wait a moment and try again."
        }
        Some("server_unavailable") => {
            "The worker could not complete enrollment with the server. Check the Server URL, internet connection and server availability, then try again."
        }
        _ => "Check the server address and enrollment token, then try again.",
    };
    format!("Enrollment failed. {message}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_codes_can_supply_enrollment_messages() {
        let status = std::process::Command::new(if cfg!(windows) { "cmd" } else { "sh" })
            .args(if cfg!(windows) {
                vec!["/C", "exit 1"]
            } else {
                vec!["-c", "exit 1"]
            })
            .status()
            .unwrap();
        for output in [
            br#"{"error":"secret-token","errorCode":"invalid_token"}"#.as_slice(),
            br#"{"error":"secret-token","errorCode":"secret-token"}"#.as_slice(),
            b"third party output secret-token".as_slice(),
        ] {
            let message = failure(output, &status);
            assert!(!message.contains("secret-token"));
            assert!(message.starts_with("Enrollment failed."));
        }
        assert!(failure(br#"{"errorCode":"invalid_token"}"#, &status).contains("fresh token"));
    }
}

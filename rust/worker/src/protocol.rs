use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Gpu {
    pub id: String,
    pub name: String,
    pub memory_bytes: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Registration {
    pub protocol_version: u8,
    pub build: String,
    pub runtime: String,
    pub engines: BTreeMap<String, String>,
    pub gpus: Vec<Gpu>,
    pub healthy: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Fence {
    pub scan_id: String,
    pub attempt_id: String,
    pub lease_id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    pub artifact_id: String,
    pub relative_path: String,
    pub role: String,
    pub size: u64,
    pub sha256: String,
    pub url: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Input {
    pub kind: String,
    pub filename: String,
    pub url: String,
    pub sha256: Option<String>,
    pub size: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Assignment {
    #[serde(flatten)]
    pub fence: Fence,
    pub protocol_version: u8,
    pub lease_expires_at: String,
    pub request: Value,
    pub input: Input,
    #[serde(default)]
    pub resume: Vec<Artifact>,
    pub resume_state: Option<Value>,
    pub runtime_versions: Option<Value>,
}
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Poll {
    pub assignment: Option<Assignment>,
    pub command: String,
    pub poll_after_ms: u64,
    #[serde(default)]
    pub recovery_pending: bool,
    #[serde(default)]
    pub cleanup: Vec<Fence>,
}
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Heartbeat {
    pub lease_expires_at: String,
    pub command: String,
    pub reason: Option<String>,
}
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Intent {
    pub artifact_id: String,
    pub method: String,
    pub url: Option<String>,
    pub part_size: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Credentials {
    pub origin: String,
    pub worker_id: String,
    pub credential: String,
}

pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 100
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
pub fn redact(message: &str) -> String {
    let urls = regex::Regex::new(r#"(?i)https?://[^\s\"'<>]+"#).unwrap();
    let bearer = regex::Regex::new(r"(?i)Bearer\s+[^\s]+").unwrap();
    bearer
        .replace_all(
            &urls.replace_all(message, "[redacted URL]"),
            "Bearer [redacted]",
        )
        .into_owned()
}

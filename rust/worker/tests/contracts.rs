use guerrilla_worker::{
    api, archive, daemon, files,
    profile::ContextPaths,
    protocol::{redact, Artifact, Fence},
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tokio_util::sync::CancellationToken;

type Requests = Arc<Mutex<Vec<(String, Value)>>>;
async fn server(
    handler: impl Fn(&str, &[u8]) -> (u16, Vec<u8>) + Send + Sync + 'static,
) -> (String, Requests, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let requests: Requests = Default::default();
    let records = requests.clone();
    let handler = Arc::new(handler);
    let task = tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let handler = handler.clone();
            let records = records.clone();
            tokio::spawn(async move {
                let mut bytes = vec![];
                let mut buffer = [0u8; 4096];
                let header_end = loop {
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                    if let Some(i) = bytes.windows(4).position(|b| b == b"\r\n\r\n") {
                        break i + 4;
                    }
                };
                let header = String::from_utf8_lossy(&bytes[..header_end]);
                let route = header
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap()
                    .to_owned();
                let size = header
                    .lines()
                    .find_map(|line| {
                        line.to_lowercase()
                            .strip_prefix("content-length: ")
                            .and_then(|s| s.parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                while bytes.len() < header_end + size {
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                }
                let payload = &bytes[header_end..header_end + size];
                records.lock().unwrap().push((
                    route.clone(),
                    serde_json::from_slice(payload).unwrap_or(Value::Null),
                ));
                let (status, body) = handler(&route, payload);
                socket.write_all(format!("HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nETag: test-etag\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).as_bytes()).await.unwrap();
                socket.write_all(&body).await.unwrap();
            });
        }
    });
    (url, requests, task)
}
fn fence() -> Fence {
    Fence {
        scan_id: "scan".into(),
        attempt_id: "attempt".into(),
        lease_id: "lease".into(),
    }
}
#[test]
fn portable_retention_rejects_escape_reserved_and_links() {
    for value in [
        "../outside",
        "a/../b",
        "/root",
        "a\\b",
        "a:b",
        "a//b",
        "a/.",
        "nul.txt",
        "a/COM1.log",
        "a/trailing.",
        "a/trailing ",
    ] {
        assert!(!files::safe_relative(value), "{value}");
    }
    assert!(files::safe_relative("checkpoints/native_0001.ply"));
    #[cfg(unix)]
    {
        let temp = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(temp.path(), temp.path().join("linked")).unwrap();
        assert!(files::checked(temp.path(), "linked/output").is_err());
        assert!(files::remove(temp.path(), "linked").is_err());
    }
}
#[test]
fn credentials_origin_gpu_and_engine_identity_are_strict() {
    assert!(api::origin("http://example.com").is_err());
    assert!(api::origin("https://user:secret@example.com").is_err());
    assert_eq!(
        api::origin("http://127.0.0.1:123/path").unwrap(),
        "http://127.0.0.1:123"
    );
    let report = json!({"gpus":[{"id":"GPU-1","name":"GPU","memoryBytes":1024}],"backends":{"lichtfeld":{"available":true,"runtimeVersions":{"b":"2","a":"1"}},"spirula":{"available":false}}});
    let first = ContextPaths::registration(&report).unwrap();
    let mut changed = report;
    changed["backends"]["spirula"] = json!({"available":true,"runtimeVersions":{"other":"new"}});
    let second = ContextPaths::registration(&changed).unwrap();
    assert_eq!(first.engines["lichtfeld"], second.engines["lichtfeld"]);
    changed["gpus"] = json!([]);
    assert!(ContextPaths::registration(&changed).is_err());
    assert_eq!(
        redact("Bearer secret https://storage/private?signature=x"),
        "Bearer [redacted] [redacted URL]"
    );
}
#[test]
fn frozen_checkpoints_include_only_declared_products_and_skip_exclusions() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("run");
    std::fs::create_dir_all(source.join("frames")).unwrap();
    std::fs::create_dir_all(source.join("live")).unwrap();
    std::fs::write(source.join("frames/one.jpg"), b"image").unwrap();
    std::fs::write(source.join("live/unstable"), b"mutating").unwrap();
    std::fs::write(source.join("platform-state.json"),serde_json::to_vec(&json!({"completed":{"selection":{"files":[{"path":"frames/one.jpg"},{"path":"live/unstable"}]}},"retention":{"excludePaths":["live"]}})).unwrap()).unwrap();
    let snapshot = temp.path().join("snapshot");
    archive::freeze(&source, &snapshot, &[]).unwrap();
    assert!(snapshot.join("frames/one.jpg").exists());
    assert!(!snapshot.join("live/unstable").exists());
    std::fs::write(source.join("frames/one.jpg"), b"new").unwrap();
    assert_eq!(
        std::fs::read(snapshot.join("frames/one.jpg")).unwrap(),
        b"image"
    );
}
#[tokio::test]
async fn downloads_are_atomic_and_restore_identity_cannot_change() {
    let (url,_,task)=server(|route,_| if route=="/bad"{(200,b"bad".to_vec())}else{(200,serde_json::to_vec(&json!({"artifacts":[{"artifactId":"one","relativePath":"output/file","role":"intermediate","size":4,"sha256":"0".repeat(64),"url":"http://127.0.0.1/bad"}]})).unwrap())}).await;
    let client = api::Client::new(&url, "credential").unwrap();
    let temp = tempfile::tempdir().unwrap();
    let destination = temp.path().join("file");
    std::fs::write(&destination, b"previous").unwrap();
    assert!(archive::download(
        &client,
        &format!("{url}/bad"),
        &destination,
        4,
        &"a".repeat(64),
        &CancellationToken::new()
    )
    .await
    .is_err());
    assert_eq!(std::fs::read(&destination).unwrap(), b"previous");
    assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 1);
    let manifest = vec![Artifact {
        artifact_id: "one".into(),
        relative_path: "output/file".into(),
        role: "intermediate".into(),
        size: 4,
        sha256: "a".repeat(64),
        url: format!("{url}/bad"),
    }];
    assert!(archive::restore(
        &client,
        &fence(),
        &manifest,
        temp.path(),
        &["output".into()],
        &mut Default::default(),
        &CancellationToken::new()
    )
    .await
    .is_err());
    assert!(!temp.path().join("output/file").exists());
    task.abort();
}
#[tokio::test]
async fn multipart_upload_keeps_fence_identity_and_completes_verified_parts() {
    let base: Arc<Mutex<String>> = Default::default();
    let server_base = base.clone();
    let (url,requests,task)=server(move |route,bytes| {
        let response=match route {"/api/worker/artifacts"=>{let payload:Value=serde_json::from_slice(bytes).unwrap();if payload["action"]=="allocate"{json!({"artifactId":"artifact","method":"multipart","partSize":3})}else{let number=payload["partNumbers"][0].as_u64().unwrap();json!({"parts":[{"partNumber":number,"url":format!("{}/part/{number}",server_base.lock().unwrap())}]})}},_=>json!({"ok":true})};(200,serde_json::to_vec(&response).unwrap())
    }).await;
    *base.lock().unwrap() = url.clone();
    let client = api::Client::new(&url, "credential").unwrap();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("file");
    std::fs::write(&path, b"abcdefg").unwrap();
    let id = archive::archive_file(
        &client,
        &fence(),
        archive::UploadFile {
            path: &path,
            relative: "output/file",
            role: "intermediate",
            checkpoint: Some("checkpoint"),
            client_id: "stable-client",
        },
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(id, "artifact");
    let requests = requests.lock().unwrap();
    let allocation = &requests
        .iter()
        .find(|r| r.0 == "/api/worker/artifacts")
        .unwrap()
        .1;
    assert_eq!(allocation["clientId"], "stable-client");
    assert_eq!(allocation["scanId"], "scan");
    assert_eq!(allocation["size"], 7);
    assert_eq!(allocation["sha256"], files::hash(&path).unwrap().1);
    assert_eq!(allocation["reuseUploaded"], true);
    let completion = &requests
        .iter()
        .find(|r| r.0 == "/api/worker/complete")
        .unwrap()
        .1;
    assert_eq!(completion["parts"].as_array().unwrap().len(), 3);
    assert_eq!(completion["leaseId"], "lease");
    drop(requests);
    task.abort();
}
#[test]
fn status_does_not_expose_credentials_and_origin_cannot_change() {
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("profile.json");
    std::fs::write(&config, b"{\"controlUrl\":\"https://control.example\"}").unwrap();
    let paths = ContextPaths::load(config.clone()).unwrap();
    assert_eq!(daemon::status(&paths).unwrap().state, "unenrolled");
    std::fs::write(temp.path().join("credentials.json"),b"{\"origin\":\"https://control.example\",\"workerId\":\"worker\",\"credential\":\"private-token\"}").unwrap();
    let status = serde_json::to_string(&daemon::status(&paths).unwrap()).unwrap();
    assert!(!status.contains("private-token"));
    assert!(status.contains("worker"));
    std::fs::write(&config, b"{\"controlUrl\":\"https://other.example\"}").unwrap();
    assert!(ContextPaths::load(config).is_err());
}

#[cfg(unix)]
fn pipeline_fixture(temp: &tempfile::TempDir, script: &str) -> ContextPaths {
    use std::os::unix::fs::PermissionsExt;
    let config = temp.path().join("profile.json");
    let mut paths = ContextPaths::load(config).unwrap();
    paths.scratch = temp.path().join("scratch");
    paths.pipeline = temp.path().join("pipeline");
    std::fs::write(&paths.pipeline, format!("#!/usr/bin/env python3\n{script}")).unwrap();
    std::fs::set_permissions(&paths.pipeline, std::fs::Permissions::from_mode(0o700)).unwrap();
    paths
}
#[cfg(unix)]
fn assignment(seconds: i64) -> guerrilla_worker::protocol::Assignment {
    guerrilla_worker::protocol::Assignment {
        fence: fence(),
        protocol_version: 2,
        lease_expires_at: (chrono::Utc::now() + chrono::Duration::seconds(seconds)).to_rfc3339(),
        request: json!({"version":1,"backend":"lichtfeld","maxCap":100,"settings":{}}),
        input: guerrilla_worker::protocol::Input {
            kind: "stored".into(),
            filename: "input.mp4".into(),
            url: "http://unused.invalid".into(),
            sha256: None,
            size: None,
        },
        resume: vec![],
        resume_state: Some(json!({"completed":{"selection":{"files":[]}}})),
        runtime_versions: None,
    }
}
#[cfg(unix)]
#[tokio::test]
async fn execution_freezes_before_release_and_finishes_only_after_canonical_commit() {
    let base: Arc<Mutex<String>> = Default::default();
    let server_base = base.clone();
    let uploaded: Arc<Mutex<Vec<Vec<u8>>>> = Default::default();
    let output = uploaded.clone();
    let (url,requests,task)=server(move |route,payload| {let response=match route {
        "/api/worker/artifacts"=>{let body:Value=serde_json::from_slice(payload).unwrap();json!({"artifactId":body["relativePath"],"method":"put","url":format!("{}/upload",server_base.lock().unwrap())})},
        "/upload"=>{output.lock().unwrap().push(payload.to_vec());json!({})},_=>json!({})};(200,serde_json::to_vec(&response).unwrap())
    }).await;
    *base.lock().unwrap() = url.clone();
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json,sys,time
from pathlib import Path
request=json.loads(Path(sys.argv[2]).read_text())
root=Path(request['outputPath'])
(root/'export.spz').write_bytes(b'checkpoint-version')
state={'completed':{'selection':{'files':[{'path':'frames/inherited.jpg'}]},'export':{'files':[{'path':'export.spz'}]}}}
(root/'platform-state.json').write_text(json.dumps(state))
print(json.dumps({'type':'checkpoint','checkpointId':'snapshot','stage':'export'}),flush=True)
while not (root/'.archive-acks/snapshot.ready').exists():time.sleep(.01)
(root/'export.spz').write_bytes(b'mutated-live-version')
print(json.dumps({'type':'completed'}),flush=True)
"#,
    );
    let mut assignment = assignment(30);
    assignment.resume.push(Artifact {
        artifact_id: "inherited".into(),
        relative_path: "frames/inherited.jpg".into(),
        role: "intermediate".into(),
        size: 4,
        sha256: "a".repeat(64),
        url: "http://unused.invalid".into(),
    });
    let client = api::Client::new(&url, "credential").unwrap();
    let cancel = CancellationToken::new();
    let drain = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let status = Arc::new(tokio::sync::Mutex::new(daemon::Status::default()));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        guerrilla_worker::execution::execute(&paths, &client, assignment, &cancel, drain, status),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(uploaded
        .lock()
        .unwrap()
        .iter()
        .any(|bytes| bytes == b"checkpoint-version"));
    assert!(!uploaded
        .lock()
        .unwrap()
        .iter()
        .any(|bytes| bytes == b"mutated-live-version"));
    let records = requests.lock().unwrap();
    let commit = records
        .iter()
        .position(|r| r.0 == "/api/worker/checkpoint")
        .unwrap();
    let finish = records
        .iter()
        .position(|r| r.0 == "/api/worker/finish")
        .unwrap();
    assert!(commit < finish);
    assert_eq!(
        records[commit].1["state"]["completed"]["export"]["files"][0]["path"],
        "export.spz"
    );
    assert_eq!(
        records[commit].1["inheritedArtifactIds"],
        json!(["inherited"])
    );
    let events: Vec<_> = records
        .iter()
        .filter(|r| r.0 == "/api/worker/events")
        .map(|r| &r.1)
        .collect();
    let sequences: Vec<_> = events
        .iter()
        .map(|e| e["sequence"].as_u64().unwrap())
        .collect();
    assert!(sequences.windows(2).all(|pair| pair[0] < pair[1]));
    for state in ["queued", "uploading", "completing", "saved"] {
        assert!(events
            .iter()
            .any(|e| e["kind"] == "upload" && e["progress"]["status"] == state));
    }
    let saved = events
        .iter()
        .find(|e| e["kind"] == "upload" && e["progress"]["status"] == "saved")
        .unwrap();
    assert_eq!(saved["progress"]["current"], saved["progress"]["total"]);
    assert!(saved["progress"]["total"].as_u64().unwrap() > 0);
    assert_eq!(
        records[commit].1["inheritedArtifactIds"],
        json!(["inherited"])
    );
    assert_eq!(records[finish].1["state"], "completed");
    assert!(!paths.scratch.join("scan").exists());
    drop(records);
    task.abort();
}
#[cfg(unix)]
#[tokio::test]
async fn lease_expiry_stops_engine_tree_and_retains_recovery_files() {
    let (url, requests, task) = server(|_, _| (200, b"{}".to_vec())).await;
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json,sys,time,subprocess
from pathlib import Path
request=json.loads(Path(sys.argv[2]).read_text())
root=Path(request['outputPath'])
child=subprocess.Popen(['sleep','60'])
(root/'engine.pid').write_text(str(child.pid))
print(json.dumps({'type':'progress','stage':'training','message':'Training'}),flush=True)
while True:time.sleep(.05)
"#,
    );
    let client = api::Client::new(&url, "credential").unwrap();
    let mut assignment = assignment(1);
    assignment.lease_expires_at = (chrono::Utc::now() + chrono::Duration::seconds(2)).to_rfc3339();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        guerrilla_worker::execution::execute(
            &paths,
            &client,
            assignment,
            &CancellationToken::new(),
            Arc::new(std::sync::atomic::AtomicBool::new(false)),
            Arc::new(tokio::sync::Mutex::new(daemon::Status::default())),
        ),
    )
    .await
    .unwrap();
    assert!(result.is_err());
    assert!(paths.scratch.join("scan/attempt/run/engine.pid").exists());
    let pid = std::fs::read_to_string(paths.scratch.join("scan/attempt/run/engine.pid")).unwrap();
    let ps = std::process::Command::new("ps")
        .args(["-p", pid.trim(), "-o", "stat="])
        .output()
        .unwrap();
    assert!(
        String::from_utf8_lossy(&ps.stdout).trim().is_empty()
            || String::from_utf8_lossy(&ps.stdout).contains('Z')
    );
    assert!(requests
        .lock()
        .unwrap()
        .iter()
        .any(|r| r.0 == "/api/worker/finish" && r.1["state"] == "interrupted"));
    task.abort();
}

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn background_worker_survives_launcher_exit_and_repeated_start_attaches() {
    let (url, requests, task) = server(|route, _| {
        let value = if route == "/api/worker/poll" {
            json!({"assignment":null,"command":"continue","pollAfterMs":30000})
        } else {
            json!({})
        };
        (200, serde_json::to_vec(&value).unwrap())
    })
    .await;
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json
print(json.dumps({'gpus':[{'id':'fixture-GPU','name':'Fixture GPU','memoryBytes':1024}],'backends':{'lichtfeld':{'available':True,'runtimeVersions':{'fixture':'1'}}}}))
"#,
    );
    std::fs::write(
        &paths.config,
        serde_json::to_vec(&json!({"controlUrl":url,"scratchDirectory":paths.scratch})).unwrap(),
    )
    .unwrap();
    std::fs::write(
        paths.credential_path(),
        serde_json::to_vec(
            &json!({"origin":url,"workerId":"fixture-worker","credential":"fixture-secret"}),
        )
        .unwrap(),
    )
    .unwrap();
    let invoke = |command: &str| {
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_guerrilla-worker"))
            .args(["--config", paths.config.to_str().unwrap(), command])
            .env("WORKER_PIPELINE_BIN", &paths.pipeline)
            .env("WORKER_AGENT_LOCK_DIRECTORY", temp.path().join("gpu-locks"))
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let first = invoke("start");
    assert_eq!(first["running"], true);
    for _ in 0..100 {
        if !requests.lock().unwrap().is_empty() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert!(!requests.lock().unwrap().is_empty());
    let second = invoke("start");
    assert_eq!(second["running"], true);
    assert_eq!(second["workerId"], "fixture-worker");
    assert_eq!(
        requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.0 == "/api/worker/poll")
            .count(),
        1
    );
    let stopped = invoke("stop");
    assert_eq!(stopped["running"], false);
    assert_eq!(stopped["state"], "stopped");
    assert!(paths.credential_path().exists());
    task.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn owner_cancellation_confirms_fence_then_removes_local_outputs() {
    let (url,requests,task)=server(|route,_| {if route=="/api/worker/events"{(409,b"{}".to_vec())}else {let value=if route=="/api/worker/heartbeat"{json!({"command":"stop","reason":"cancelled","leaseExpiresAt":chrono::Utc::now().to_rfc3339()})}else{json!({})};(200,serde_json::to_vec(&value).unwrap())}}).await;
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json,time
print(json.dumps({'type':'progress','stage':'training','message':'Training'}),flush=True)
while True:time.sleep(.05)
"#,
    );
    let result = guerrilla_worker::execution::execute(
        &paths,
        &api::Client::new(&url, "credential").unwrap(),
        assignment(30),
        &CancellationToken::new(),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Arc::new(tokio::sync::Mutex::new(daemon::Status::default())),
    )
    .await;
    assert!(result.is_err());
    assert!(!paths.scratch.join("scan").exists());
    let records = requests.lock().unwrap();
    let heartbeat = records
        .iter()
        .position(|r| r.0 == "/api/worker/heartbeat")
        .unwrap();
    let cleanup = records
        .iter()
        .position(|r| r.0 == "/api/worker/cleanup")
        .unwrap();
    assert!(heartbeat < cleanup);
    assert!(records
        .iter()
        .any(|r| r.0 == "/api/worker/finish" && r.1["error"] == "Cancelled by owner."));
    drop(records);
    task.abort();
}
#[tokio::test]
async fn background_archive_coalesces_pending_snapshots_and_retries_without_early_ack() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let commits = Arc::new(AtomicUsize::new(0));
    let count = commits.clone();
    let (url,requests,task)=server(move |route,payload| {let response=if route=="/api/worker/artifacts" {let value:Value=serde_json::from_slice(payload).unwrap();json!({"artifactId":format!("{}:{}",value["checkpointId"].as_str().unwrap(),value["relativePath"].as_str().unwrap()),"method":"reuse"})}else{json!({})};if route=="/api/worker/checkpoint"&&count.fetch_add(1,Ordering::SeqCst)==0{return (503,b"{}".to_vec());}(200,serde_json::to_vec(&response).unwrap())}).await;
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("run");
    std::fs::create_dir_all(&output).unwrap();
    std::fs::write(output.join("platform-state.json"), b"{\"completed\":{}}").unwrap();
    let queue = archive::BackgroundArchive::new(
        api::Client::new(&url, "credential").unwrap(),
        fence(),
        output.clone(),
        vec![],
        CancellationToken::new(),
    );
    queue.enqueue("first", 1).await.unwrap();
    for _ in 0..200 {
        if commits.load(Ordering::SeqCst) > 0 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    assert_eq!(commits.load(Ordering::SeqCst), 1);
    assert!(!output.join(".archive-acks/first").exists());
    assert!(output.join(".archive-acks/first.ready").exists());
    queue.enqueue("superseded", 2).await.unwrap();
    queue.enqueue("latest", 3).await.unwrap();
    assert!(!output.join(".worker/archives/superseded").exists());
    tokio::time::timeout(std::time::Duration::from_secs(8), queue.drain())
        .await
        .unwrap()
        .unwrap();
    assert!(output.join(".archive-acks/first").exists());
    assert!(output.join(".archive-acks/latest").exists());
    assert!(!output.join(".archive-acks/superseded").exists());
    assert!(!requests
        .lock()
        .unwrap()
        .iter()
        .any(|r| r.0 == "/api/worker/checkpoint" && r.1["checkpointId"] == "superseded"));
    task.abort();
}
#[test]
fn diagnostics_tail_is_bounded_and_redacted() {
    let temp = tempfile::tempdir().unwrap();
    let paths = ContextPaths::load(temp.path().join("profile.json")).unwrap();
    let mut text = "old\n".repeat(20000);
    text.push_str("Bearer secret https://signed.example/x?signature=secret\n");
    std::fs::write(paths.state.join("daemon.log"), text).unwrap();
    let logs = daemon::logs(&paths).unwrap();
    let lines = logs["lines"].as_array().unwrap();
    assert!(lines.len() <= 200);
    let rendered = logs.to_string();
    assert!(!rendered.contains("secret"));
    assert!(rendered.contains("[redacted]"));
}

#[tokio::test]
async fn successive_checkpoints_reuse_completed_data_and_send_frozen_inline_state() {
    let base: Arc<Mutex<String>> = Default::default();
    let server_base = base.clone();
    let uploaded: Arc<Mutex<std::collections::BTreeMap<String, String>>> = Default::default();
    let known = uploaded.clone();
    let (url, requests, task) = server(move |route, payload| {
        let response = if route == "/api/worker/artifacts" {
            let value: Value = serde_json::from_slice(payload).unwrap();
            let key = format!("{}:{}", value["relativePath"], value["sha256"]);
            let mut known = known.lock().unwrap();
            if value["reuseUploaded"] == true && known.contains_key(&key) {
                json!({"artifactId":known[&key],"method":"reuse"})
            } else {
                let id = format!("artifact-{}", known.len());
                known.insert(key, id.clone());
                json!({"artifactId":id,"method":"put","url":format!("{}/upload/{id}",server_base.lock().unwrap())})
            }
        } else if route == "/api/worker/checkpoint" {
            let value: Value = serde_json::from_slice(payload).unwrap();
            if !value["state"]["completed"].is_object() {
                return (400, b"{}".to_vec());
            }
            json!({"ok":true})
        } else { json!({"ok":true}) };
        (200, serde_json::to_vec(&response).unwrap())
    }).await;
    *base.lock().unwrap() = url.clone();
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("run");
    std::fs::create_dir_all(output.join("selection")).unwrap();
    std::fs::write(output.join("selection/frame.jpg"), b"unchanged frame").unwrap();
    let mut state = json!({"completed":{"selection":{"directory":"selection","files":[{"path":"selection/frame.jpg"}]}},"step":1});
    let inherited_state = Artifact {
        artifact_id: "old-state".into(),
        relative_path: "platform-state.json".into(),
        role: "checkpoint".into(),
        size: 0,
        sha256: String::new(),
        url: "http://unused.invalid".into(),
    };
    let queue = archive::BackgroundArchive::new(
        api::Client::new(&url, "credential").unwrap(),
        fence(),
        output.clone(),
        vec![inherited_state],
        CancellationToken::new(),
    );
    for step in 1..=2 {
        state["step"] = json!(step);
        std::fs::write(output.join("platform-state.json"), state.to_string()).unwrap();
        queue
            .enqueue(&format!("checkpoint-{step}"), step)
            .await
            .unwrap();
        // Computation can update live state after the ready acknowledgment.
        std::fs::write(output.join("platform-state.json"), b"{\"mutated\":true}").unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), queue.drain())
            .await
            .unwrap()
            .unwrap();
    }
    let records = requests.lock().unwrap();
    assert_eq!(
        records
            .iter()
            .filter(|(route, _)| route.starts_with("/upload/"))
            .count(),
        3
    );
    let commits: Vec<_> = records
        .iter()
        .filter(|(route, _)| route == "/api/worker/checkpoint")
        .collect();
    assert_eq!(commits.len(), 2);
    assert_eq!(commits[0].1["state"]["step"], 1);
    assert_eq!(commits[1].1["state"]["step"], 2);
    assert!(commits
        .iter()
        .all(|(_, value)| value["inheritedArtifactIds"].is_null()));
    drop(records);
    task.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn shutdown_interrupts_process_that_closed_stdout_but_kept_running() {
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import os,time
os.close(1)
while True:time.sleep(.05)
"#,
    );
    let cancel = CancellationToken::new();
    let signal = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        signal.cancel();
    });
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        guerrilla_worker::execution::run_process(
            &paths,
            &[],
            &temp.path().join("pipeline.log"),
            &cancel,
            |_| async { Ok(()) },
        ),
    )
    .await
    .unwrap();
    assert!(result.is_err());
}
#[cfg(unix)]
#[tokio::test]
async fn early_scratch_error_does_not_leave_a_renewing_lease_task() {
    let (url,requests,task)=server(|_,_|(200,serde_json::to_vec(&json!({"command":"continue","leaseExpiresAt":(chrono::Utc::now()+chrono::Duration::minutes(1)).to_rfc3339()})).unwrap())).await;
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(&temp, "pass\n");
    std::fs::create_dir_all(&paths.scratch).unwrap();
    std::os::unix::fs::symlink(temp.path(), paths.scratch.join("scan")).unwrap();
    let result = guerrilla_worker::execution::execute(
        &paths,
        &api::Client::new(&url, "credential").unwrap(),
        assignment(30),
        &CancellationToken::new(),
        Arc::new(std::sync::atomic::AtomicBool::new(false)),
        Arc::new(tokio::sync::Mutex::new(daemon::Status::default())),
    )
    .await;
    assert!(result.is_err());
    tokio::time::sleep(std::time::Duration::from_millis(5200)).await;
    assert!(requests.lock().unwrap().is_empty());
    task.abort();
}

#[cfg(unix)]
#[test]
fn legacy_preflight_flag_returns_registration_and_subcommand_returns_capabilities() {
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json
print(json.dumps({'gpus':[{'id':'fixture-GPU','name':'Fixture GPU','memoryBytes':1024}],'backends':{'lichtfeld':{'available':True,'runtimeVersions':{'fixture':'1'}}}}))
"#,
    );
    let invoke = |argument: &str| {
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_guerrilla-worker"))
            .args(["--config", paths.config.to_str().unwrap(), argument])
            .env("WORKER_PIPELINE_BIN", &paths.pipeline)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let legacy = invoke("--preflight");
    assert_eq!(legacy["protocolVersion"], 2);
    assert_eq!(legacy["healthy"], true);
    assert_eq!(legacy["engines"]["lichtfeld"].as_str().unwrap().len(), 64);
    assert!(legacy.get("backends").is_none());
    let gui = invoke("preflight");
    assert_eq!(gui["backends"]["lichtfeld"]["available"], true);
    assert!(gui.get("protocolVersion").is_none());
}

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn enrollment_without_engines_or_pipeline_saves_credentials_and_stays_stopped() {
    let (url, requests, task) = server(|_, _| {
        (
            200,
            br#"{"workerId":"enrolled-worker","credential":"fixture-credential"}"#.to_vec(),
        )
    })
    .await;
    let temp = tempfile::tempdir().unwrap();
    let config = temp.path().join("profile.json");
    std::fs::write(&config, serde_json::to_vec(&json!({"controlUrl":url,"spirulaPath":"/missing-engine","scratchDirectory":temp.path().join("scratch")})).unwrap()).unwrap();
    let paths = ContextPaths::load(config).unwrap();
    let missing_pipeline = temp.path().join("absent-pipeline");
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_guerrilla-worker"))
        .args([
            "--config",
            paths.config.to_str().unwrap(),
            "enroll",
            "--token-stdin",
        ])
        .env_remove("WORKER_ENROLLMENT_TOKEN")
        .env("WORKER_PIPELINE_BIN", &missing_pipeline)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"private-enrollment-token\n")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("private-enrollment-token"));
    let status: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(status["state"], "stopped");
    assert_eq!(status["running"], false);
    assert_eq!(status["workerId"], "enrolled-worker");
    assert!(paths.credential_path().exists());
    let registration = requests.lock().unwrap()[0].1["registration"].clone();
    assert_eq!(registration["healthy"], false);
    assert_eq!(registration["gpus"], json!([]));
    assert_eq!(registration["engines"], json!({}));
    assert_eq!(registration["build"], env!("CARGO_PKG_VERSION"));
    // Starting still needs engine/GPU readiness; enrollment cannot claim work.
    let mut paths = paths;
    paths.pipeline = temp.path().join("absent-pipeline");
    assert!(daemon::run(paths.clone()).await.is_err());
    assert_eq!(daemon::status(&paths).unwrap().state, "error");
    assert!(paths.credential_path().exists());
    assert_eq!(requests.lock().unwrap().len(), 1);
    task.abort();
}

#[cfg(unix)]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn enrollment_preserves_authentication_failures_as_safe_codes() {
    let (url, requests, task) =
        server(|_, _| (401, br#"{"error":"private-enrollment-token"}"#.to_vec())).await;
    let temp = tempfile::tempdir().unwrap();
    let mut paths = pipeline_fixture(
        &temp,
        r#"
import json
print(json.dumps({'gpus':[{'id':'fixture-GPU','name':'Fixture GPU','memoryBytes':1024}],'backends':{'spirula':{'available':True,'runtimeVersions':{'fixture':'1'}}}}))
"#,
    );
    paths.profile.control_url = url;
    let error = daemon::enroll(&paths, "private-enrollment-token")
        .await
        .unwrap_err();
    assert_eq!(
        error
            .downcast_ref::<guerrilla_worker::enrollment::EnrollmentError>()
            .unwrap()
            .0,
        "invalid_token"
    );
    assert!(!error.to_string().contains("private-enrollment-token"));
    assert_eq!(requests.lock().unwrap().len(), 1);
    assert!(!paths.credential_path().exists());
    task.abort();
}

#[test]
fn pipeline_commands_explicitly_isolate_python_and_node_import_overrides() {
    let temp = tempfile::tempdir().unwrap();
    let paths = ContextPaths::load(temp.path().join("profile.json")).unwrap();
    let command = paths.command();
    let env: std::collections::BTreeMap<_, _> = command
        .as_std()
        .get_envs()
        .map(|(key, value)| {
            (
                key.to_string_lossy().into_owned(),
                value.map(|value| value.to_string_lossy().into_owned()),
            )
        })
        .collect();
    for key in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONPLATLIBDIR",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "NODE_OPTIONS",
        "NODE_PATH",
    ] {
        assert_eq!(
            env.get(key),
            Some(&None),
            "{key} must be explicitly removed"
        );
    }
    assert_eq!(env.get("PYTHONNOUSERSITE"), Some(&Some("1".into())));
    assert!(!env.contains_key("LD_LIBRARY_PATH"));
    assert!(!env.contains_key("DYLD_LIBRARY_PATH"));
}
#[cfg(unix)]
#[test]
fn vendored_pipeline_ignores_ambient_python_and_node_setup_but_keeps_engine_libraries() {
    let temp = tempfile::tempdir().unwrap();
    let paths = pipeline_fixture(
        &temp,
        r#"
import json,os
keys=['PYTHONHOME','PYTHONPATH','PYTHONUSERBASE','PYTHONSTARTUP','PYTHONINSPECT','PYTHONPLATLIBDIR','PYTHONEXECUTABLE','__PYVENV_LAUNCHER__','NODE_OPTIONS','NODE_PATH','PYTHONNOUSERSITE','LD_LIBRARY_PATH','LICHTFELD_BIN']
print(json.dumps({key:os.environ.get(key) for key in keys}))
"#,
    );
    let mut command = std::process::Command::new(env!("CARGO_BIN_EXE_guerrilla-worker"));
    command
        .args(["--config", paths.config.to_str().unwrap(), "preflight"])
        .env("WORKER_PIPELINE_BIN", &paths.pipeline)
        .env("PYTHONNOUSERSITE", "0")
        .env("LD_LIBRARY_PATH", "/engine-libraries")
        .env("LICHTFELD_BIN", "/user/licensed/LichtFeld");
    for key in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONPLATLIBDIR",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "NODE_OPTIONS",
        "NODE_PATH",
    ] {
        command.env(key, "/invalid/ambient-runtime");
    }
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    for key in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONPLATLIBDIR",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "NODE_OPTIONS",
        "NODE_PATH",
    ] {
        assert_eq!(
            report[key],
            Value::Null,
            "{key} leaked into bundled runtime"
        );
    }
    assert_eq!(report["PYTHONNOUSERSITE"], "1");
    assert_eq!(report["LD_LIBRARY_PATH"], "/engine-libraries");
    assert_eq!(report["LICHTFELD_BIN"], "/user/licensed/LichtFeld");
}

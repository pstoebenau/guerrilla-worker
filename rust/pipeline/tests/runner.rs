#![cfg(unix)]
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Command, Stdio},
};
use tempfile::TempDir;

struct Fixture {
    root: TempDir,
    input: PathBuf,
    output: PathBuf,
    request: PathBuf,
    engine: PathBuf,
    converter: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let input = root.path().join("input.mp4");
        fs::write(&input, b"source fixture").unwrap();
        let output = root.path().join("attempt");
        let request = root.path().join("request.json");
        let engine = root.path().join("spirula");
        let converter = root.path().join("converter");
        for (path, source) in [
            (&engine, include_str!("fixtures/engine.py")),
            (&converter, include_str!("fixtures/converter.py")),
        ] {
            fs::write(path, source).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        let binary = root.path().join("guerrilla-pipeline");
        fs::copy(env!("CARGO_BIN_EXE_guerrilla-pipeline"), &binary).unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
        let fixture = Self {
            root,
            input,
            output,
            request,
            engine,
            converter,
        };
        fixture.write(json!({}));
        fixture
    }
    fn write(&self, overrides: Value) {
        let mut request = json!({"scanId":"scan","attemptId":"attempt","backend":"spirula","maxCap":4,"settings":{"training":{"iterations":2}},"inputPath":self.input,"outputPath":self.output});
        for (key, value) in overrides.as_object().unwrap() {
            request[key] = value.clone();
        }
        fs::write(&self.request, serde_json::to_vec(&request).unwrap()).unwrap();
    }
    fn command(&self) -> Command {
        let mut command = Command::new(self.root.path().join("guerrilla-pipeline"));
        command
            .env("SPIRULA_BIN", &self.engine)
            .env("LICHTFELD_BIN", self.root.path().join("absent-lichtfeld"))
            .env("SPLAT_TRANSFORM_BIN", &self.converter)
            .env("PYTHON", "python3")
            .env("GPU_LOCK_PATH", self.root.path().join("gpu.lock"))
            .env_remove("WORKER_PARENT_PID");
        command
    }
    fn run(&self) -> (bool, Vec<Value>) {
        let output = self
            .command()
            .args(["--request", self.request.to_str().unwrap()])
            .output()
            .unwrap();
        let events = String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect::<Vec<_>>();
        (output.status.success(), events)
    }
    fn state(&self) -> Value {
        serde_json::from_slice(&fs::read(self.output.join("platform-state.json")).unwrap()).unwrap()
    }
}

#[test]
fn real_command_contract_completes_and_resume_rejects_changed_artifacts() {
    let fixture = Fixture::new();
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let result = events.last().unwrap();
    assert_eq!(result["type"], "completed");
    assert_eq!(result["gaussians"], 4);
    let state = fixture.state();
    assert_eq!(state["status"], "completed");
    assert_eq!(state["checkpoints"].as_array().unwrap().len(), 1);
    let checkpoint = fixture
        .output
        .join(state["checkpoints"][0]["path"].as_str().unwrap());
    assert!(checkpoint.join("state.tar").is_file());
    assert!(checkpoint.parent().unwrap().join("config.json").is_file());
    fixture.write(json!({"resume":true}));
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    assert!(events.iter().any(|event| event["resumed"] == true));
    let artifact = fixture.output.join(
        state["completed"]["selection"]["files"][0]["path"]
            .as_str()
            .unwrap(),
    );
    fs::write(artifact, b"modified").unwrap();
    let (success, events) = fixture.run();
    assert!(!success);
    assert!(events.last().unwrap()["error"]
        .as_str()
        .unwrap()
        .contains("Completed artifact changed"));
}

#[test]
fn completed_selective_resume_only_restores_export() {
    let fixture = Fixture::new();
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let state = fixture.state();
    let input_sha = state["identity"]["inputSha256"].clone();
    for name in ["selection", "reconstruction", "training"] {
        fs::remove_dir_all(fixture.output.join(state["stages"][name].as_str().unwrap())).unwrap();
    }
    fs::remove_file(&fixture.input).unwrap();
    fixture.write(json!({"resume":true,"selectiveResume":true,"inputSha256":input_sha}));
    let mut child = fixture
        .command()
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut restores = vec![];
    for line in BufReader::new(child.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "restore" {
            restores.push(event["paths"].clone());
            let ack = fixture
                .output
                .join(".worker/restored")
                .join(event["restoreId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
    }
    assert!(child.wait().unwrap().success());
    assert_eq!(restores.len(), 1);
    assert!(restores[0][0].as_str().unwrap().ends_with("/result.sog"));
}

#[test]
fn archival_failure_retains_native_training_bytes() {
    let fixture = Fixture::new();
    fixture.write(json!({"archiveAck":true,"archiveTimeoutSeconds":1}));
    let mut child = fixture
        .command()
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut events = vec![];
    for line in BufReader::new(child.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "checkpoint" && event["stage"] != "training" {
            let ack = fixture
                .output
                .join(".archive-acks")
                .join(event["checkpointId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
        events.push(event);
    }
    assert!(!child.wait().unwrap().success());
    assert!(events.last().unwrap()["error"]
        .as_str()
        .unwrap()
        .contains("archival was not acknowledged"));
    let state = fixture.state();
    let training = fixture
        .output
        .join(state["stages"]["training"].as_str().unwrap());
    assert!(training.join("run/step-1.ckpt/state.tar").is_file());
    assert!(training.join("run/step-2.ckpt/state.tar").is_file());
    let checkpoint = fixture
        .output
        .join(state["checkpoints"][0]["path"].as_str().unwrap());
    assert!(checkpoint.join("state.tar").is_file());
}

#[test]
fn pruning_requires_verified_ack_and_retains_final_plus_latest_snapshot() {
    let fixture = Fixture::new();
    fixture.write(json!({"archiveAck":true,"archiveTimeoutSeconds":5}));
    let mut child = fixture
        .command()
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    for line in BufReader::new(child.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "checkpoint" {
            let state = fixture.state();
            if event["stage"] == "training" && !state["completed"].get("training").is_some() {
                let training = fixture
                    .output
                    .join(state["stages"]["training"].as_str().unwrap());
                assert!(training.join("run/step-1.ckpt/state.tar").is_file());
            }
            let ack = fixture
                .output
                .join(".archive-acks")
                .join(event["checkpointId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
    }
    assert!(child.wait().unwrap().success());
    let state = fixture.state();
    let training = fixture
        .output
        .join(state["stages"]["training"].as_str().unwrap());
    assert!(!training.join("run/step-1.ckpt").exists());
    assert!(!training.join("run/step-2.ckpt").exists());
    assert!(training.join("final/splat.ply").is_file());
    assert!(fixture
        .output
        .join(state["checkpoints"][0]["path"].as_str().unwrap())
        .join("state.tar")
        .is_file());
}

#[test]
fn converter_count_change_is_rejected() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .env("FIXTURE_COUNT_DELTA", "-1")
        .args(["--request", fixture.request.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("Export Gaussian counts disagree"));
    assert_ne!(fixture.state()["status"], "completed");
}

#[test]
fn requested_spirula_preflight_does_not_import_cuda_engine() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .args(["--preflight", "--engine", "spirula"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["backends"]["spirula"]["available"], true);
    assert!(report["backends"].get("lichtfeld").is_none());
}

#[test]
fn changed_cap_settings_or_runtime_cannot_resume() {
    let fixture = Fixture::new();
    assert!(fixture.run().0);
    for overrides in [
        json!({"resume":true,"maxCap":3}),
        json!({"resume":true,"settings":{"training":{"iterations":3}}}),
        json!({"resume":true,"runtimeVersions":{"pipeline":"wrong"}}),
    ] {
        fixture.write(overrides);
        let (success, events) = fixture.run();
        assert!(!success, "{events:?}");
        assert!(
            events.last().unwrap()["error"]
                .as_str()
                .unwrap()
                .contains("changed")
                || events.last().unwrap()["error"]
                    .as_str()
                    .unwrap()
                    .contains("runtime is unavailable")
        );
    }
}

#[test]
fn abrupt_runner_exit_stops_engine_descendants_and_releases_gpu_lock() {
    use std::time::{Duration, Instant};
    let fixture = Fixture::new();
    let mut child = fixture
        .command()
        .env("FIXTURE_HOLD", "1")
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut engine_pid = None;
    let mut descendant_pid = None;
    for line in BufReader::new(child.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "progress" && event["stage"] == "training" {
            let state = fixture.state();
            let run = fixture
                .output
                .join(state["stages"]["training"].as_str().unwrap())
                .join("run");
            if run.join("descendant.pid").is_file() {
                engine_pid = Some(
                    fs::read_to_string(run.join("engine.pid"))
                        .unwrap()
                        .parse::<i32>()
                        .unwrap(),
                );
                descendant_pid = Some(
                    fs::read_to_string(run.join("descendant.pid"))
                        .unwrap()
                        .parse::<i32>()
                        .unwrap(),
                );
                break;
            }
        }
        assert_ne!(event["type"], "failed", "{event}");
    }
    assert!(engine_pid.is_some() && descendant_pid.is_some());
    unsafe {
        libc::kill(child.id() as i32, libc::SIGKILL);
    }
    child.wait().unwrap();
    let deadline = Instant::now() + Duration::from_secs(6);
    for pid in [engine_pid.unwrap(), descendant_pid.unwrap()] {
        loop {
            #[cfg(target_os = "linux")]
            let state = fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .and_then(|text| {
                    text.rfind(')')
                        .map(|end| text[end + 1..].trim_start().to_string())
                })
                .unwrap_or_default();
            #[cfg(target_os = "macos")]
            let state = {
                let output = Command::new("ps")
                    .args(["-o", "stat=", "-p", &pid.to_string()])
                    .output()
                    .unwrap();
                String::from_utf8_lossy(&output.stdout).to_string()
            };
            if state.trim().is_empty() || state.starts_with('Z') {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "Orphan engine {pid} remains live: {state}"
            );
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    // Dead engine PIDs can precede the guardian's final FD close by a
    // scheduling turn. Assert actual lock release before starting replacement;
    // an inherited lock must remain held throughout descendant containment.
    let lock = fs::File::options()
        .read(true)
        .write(true)
        .open(fixture.root.path().join("gpu.lock"))
        .unwrap();
    let released_by = Instant::now() + Duration::from_secs(6);
    loop {
        match fs2::FileExt::try_lock_exclusive(&lock) {
            Ok(()) => {
                fs2::FileExt::unlock(&lock).unwrap();
                break;
            }
            Err(error) => {
                assert_eq!(
                    error.raw_os_error(),
                    fs2::lock_contended_error().raw_os_error()
                );
                assert!(
                    Instant::now() < released_by,
                    "Dead engine guardian did not release inherited GPU lock"
                );
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    }
    fixture.write(json!({"outputPath":fixture.root.path().join("replacement")}));
    let (success, events) = fixture.run();
    assert!(success, "Replacement could not reclaim GPU: {events:?}");
}

#[test]
fn failed_download_does_not_disclose_signed_url() {
    use std::{
        io::{Read, Write},
        net::TcpListener,
    };
    let fixture = Fixture::new();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        let mut request = [0; 4096];
        let _ = socket.read(&mut request);
        socket.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
    });
    let url = format!("http://{address}/video?X-Amz-Signature=super-secret-token");
    let output = fixture
        .command()
        .args([
            "--download",
            &url,
            "--destination",
            fixture.root.path().join("video.mp4").to_str().unwrap(),
        ])
        .output()
        .unwrap();
    server.join().unwrap();
    assert!(!output.status.success());
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(!text.contains("super-secret-token"));
    assert!(!text.contains(&url));
    assert!(text.contains("redacted URL"));
}

#[test]
fn interrupted_training_selectively_restores_checkpoint_and_relocated_dataset() {
    let fixture = Fixture::new();
    fixture.write(json!({"archiveAck":true,"archiveTimeoutSeconds":1}));
    let mut initial = fixture
        .command()
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    for line in BufReader::new(initial.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "checkpoint" && event["stage"] != "training" {
            let ack = fixture
                .output
                .join(".archive-acks")
                .join(event["checkpointId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
    }
    assert!(!initial.wait().unwrap().success());
    let state = fixture.state();
    assert!(state["stageCheckpoints"].get("training").is_some());
    let relocated = fixture.root.path().join("relocated");
    fs::create_dir_all(&relocated).unwrap();
    fs::copy(
        fixture.output.join("platform-state.json"),
        relocated.join("platform-state.json"),
    )
    .unwrap();
    fs::remove_file(&fixture.input).unwrap();
    fixture.write(json!({"resume":true,"selectiveResume":true,"inputSha256":state["identity"]["inputSha256"],"outputPath":relocated}));
    let mut resumed = fixture
        .command()
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut restored = vec![];
    let mut failed = None;
    for line in BufReader::new(resumed.stdout.take().unwrap()).lines() {
        let event: Value = serde_json::from_str(&line.unwrap()).unwrap();
        if event["type"] == "restore" {
            for name in event["paths"].as_array().unwrap() {
                let name = name.as_str().unwrap();
                restored.push(name.to_string());
                copy_fixture(&fixture.output.join(name), &relocated.join(name));
            }
            let ack = relocated
                .join(".worker/restored")
                .join(event["restoreId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
        if event["type"] == "failed" {
            failed = Some(event);
        }
    }
    assert!(resumed.wait().unwrap().success(), "{failed:?}");
    let final_state: Value =
        serde_json::from_slice(&fs::read(relocated.join("platform-state.json")).unwrap()).unwrap();
    let training = relocated.join(final_state["stages"]["training"].as_str().unwrap());
    assert!(training.join("run/resumed-from").is_file());
    assert!(restored.iter().any(|path| path.ends_with("/images")));
    assert!(restored
        .iter()
        .any(|path| path.contains("retained-checkpoints")));
    assert!(!restored.iter().any(|path| path.starts_with("selection")));
    assert_eq!(final_state["result"]["gaussians"], 4);
}
fn copy_fixture(source: &std::path::Path, target: &std::path::Path) {
    if source.is_dir() {
        fs::create_dir_all(target).unwrap();
        for entry in fs::read_dir(source).unwrap() {
            let entry = entry.unwrap();
            copy_fixture(&entry.path(), &target.join(entry.file_name()));
        }
    } else {
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(source, target).unwrap();
    }
}

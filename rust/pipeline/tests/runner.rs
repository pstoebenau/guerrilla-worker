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

#[allow(dead_code)]
#[path = "../src/process.rs"]
mod process_fixture;
fn emit(_kind: &str, _values: Value) {}

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
        fs::write(root.path().join("roma.pt"), b"fixture model").unwrap();
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
            .env("SPIRULA_ROMA_MODEL", self.root.path().join("roma.pt"))
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
    for name in ["selection", "reconstruction", "densification", "training"] {
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
    assert!(state["completed"].get("training").is_none());
    let training = fixture
        .output
        .join(state["stages"]["training"].as_str().unwrap());
    let checkpoint = fixture
        .output
        .join(state["checkpoints"][0]["path"].as_str().unwrap());
    // Final preservation can precede a failed archival acknowledgement. Seed
    // that incomplete output explicitly rather than depending on engine timing.
    copy_fixture(
        &checkpoint.join("splat.ply"),
        &training.join("final/splat.ply"),
    );
    copy_fixture(
        &checkpoint.parent().unwrap().join("config.json"),
        &training.join("final/config.json"),
    );
    assert!(fs::read_to_string(training.join("final/config.json"))
        .unwrap()
        .contains("\"resumed\": false"));
    let relocated = fixture.root.path().join("relocated");
    fs::create_dir_all(&relocated).unwrap();
    fs::copy(
        fixture.output.join("platform-state.json"),
        relocated.join("platform-state.json"),
    )
    .unwrap();
    fs::remove_file(&fixture.input).unwrap();
    fixture.write(json!({"resume":true,"selectiveResume":true,"inputSha256":state["identity"]["inputSha256"],"outputPath":relocated,"archiveAck":true,"archiveTimeoutSeconds":30}));
    let release = fixture.root.path().join("resume-release");
    let mut resumed = fixture
        .command()
        .env("FIXTURE_RESUME_RELEASE", &release)
        .args(["--request", fixture.request.to_str().unwrap()])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut restored = vec![];
    let mut failed = None;
    let mut checkpoint_observed = false;
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
        if event["type"] == "checkpoint" {
            let current: Value =
                serde_json::from_slice(&fs::read(relocated.join("platform-state.json")).unwrap())
                    .unwrap();
            if event["stage"] == "training" && current["completed"].get("training").is_none() {
                let run_config = relocated
                    .join(current["stages"]["training"].as_str().unwrap())
                    .join("run/config.json");
                assert!(fs::read_to_string(run_config)
                    .unwrap()
                    .contains("\"resumed\": true"));
                checkpoint_observed = true;
                fs::write(&release, b"checkpoint observed").unwrap();
            }
            let ack = relocated
                .join(".archive-acks")
                .join(event["checkpointId"].as_str().unwrap());
            fs::create_dir_all(ack.parent().unwrap()).unwrap();
            fs::write(ack, b"verified").unwrap();
        }
        if event["type"] == "failed" {
            failed = Some(event);
        }
    }
    assert!(resumed.wait().unwrap().success(), "{failed:?}");
    assert!(
        checkpoint_observed,
        "Resume must survive pruning an acknowledged live checkpoint"
    );
    let final_state: Value =
        serde_json::from_slice(&fs::read(relocated.join("platform-state.json")).unwrap()).unwrap();
    let training = relocated.join(final_state["stages"]["training"].as_str().unwrap());
    let config = fs::read_to_string(training.join("final/config.json")).unwrap();
    assert!(config.contains("\"resumed\": true"));
    assert!(config.contains("Infinity"));
    assert!(restored.iter().any(|path| path.ends_with("/images")));
    assert!(restored
        .iter()
        .any(|path| path.contains("retained-checkpoints")));
    assert!(!restored.iter().any(|path| path.starts_with("selection")));
    for stage in ["selection", "reconstruction", "densification"] {
        assert_eq!(final_state["completed"][stage], state["completed"][stage]);
    }
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

#[test]
fn interrupted_preprocessing_restarts_with_a_new_directory() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .env("FIXTURE_FAIL_SELECTION", "1")
        .args(["--request", fixture.request.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let old = fixture.state()["stages"]["selection"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(fixture.output.join(&old).join("partial.jpg").is_file());
    fixture.write(json!({"resume":true}));
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    assert_ne!(fixture.state()["stages"]["selection"], old);
    assert!(!fixture.output.join(&old).exists());
}

#[test]
fn retention_keeps_only_the_latest_preview_and_full_checkpoint() {
    let fixture = Fixture::new();
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let state = fixture.state();
    let previews = fixture
        .output
        .join(state["stages"]["training"].as_str().unwrap())
        .join("previews");
    let latest = fs::read_dir(&previews)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().and_then(|s| s.to_str()) == Some("sog"))
        .unwrap();
    let obsolete = previews.join("step-000000000-obsolete.sog");
    fs::write(&obsolete, b"obsolete preview").unwrap();
    fs::write(obsolete.with_extension("log"), b"obsolete preview log").unwrap();
    let output = fixture
        .command()
        .args(["--retention-plan", fixture.output.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(output.status.success());
    let plan: Value = serde_json::from_slice(&output.stdout).unwrap();
    let exclusions = plan["excludePaths"].as_array().unwrap();
    assert!(exclusions.iter().any(|path| path
        .as_str()
        .unwrap()
        .ends_with("step-000000000-obsolete.sog")));
    assert!(exclusions.iter().any(|path| path
        .as_str()
        .unwrap()
        .ends_with("step-000000000-obsolete.log")));
    assert!(!exclusions
        .iter()
        .any(|path| fixture.output.join(path.as_str().unwrap()) == latest));
    assert_eq!(state["checkpoints"].as_array().unwrap().len(), 1);
}

#[test]
fn failed_latest_preview_preserves_the_previous_successful_preview() {
    let fixture = Fixture::new();
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let state = fixture.state();
    let previews = fixture
        .output
        .join(state["stages"]["training"].as_str().unwrap())
        .join("previews");
    let previous = fs::read_dir(&previews)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().and_then(|s| s.to_str()) == Some("sog"))
        .unwrap();
    let failed = previews.join("step-999999999-failed.sog");
    fs::write(&failed, b"invalid interrupted preview").unwrap();
    let output = fixture
        .command()
        .args(["--retention-plan", fixture.output.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(output.status.success());
    let plan: Value = serde_json::from_slice(&output.stdout).unwrap();
    let exclusions = plan["excludePaths"].as_array().unwrap();
    assert!(exclusions
        .iter()
        .any(|path| fixture.output.join(path.as_str().unwrap()) == failed));
    assert!(!exclusions
        .iter()
        .any(|path| fixture.output.join(path.as_str().unwrap()) == previous));
}

#[test]
fn native_dense_owns_its_dataset_and_discards_caches() {
    let fixture = Fixture::new();
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let state = fixture.state();
    let reconstruction = fixture
        .output
        .join(state["stages"]["reconstruction"].as_str().unwrap());
    let dense = fixture
        .output
        .join(state["stages"]["densification"].as_str().unwrap());
    assert!(!reconstruction.join("dense").exists());
    assert!(reconstruction.join("images/0.jpg").is_file());
    assert!(dense.join("images/0.jpg").is_file());
    assert!(dense.join("sparse/0/cameras.bin").is_file());
    assert!(dense.join("dense/current.json").is_file());
    assert!(dense
        .join("dense/generations/fixture-generation/roma.ply")
        .is_file());
    for name in [
        "selection",
        "reconstruction",
        "densification",
        "training",
        "export",
    ] {
        let stage = &state["completed"][name];
        assert!(stage["files"]
            .as_array()
            .unwrap()
            .iter()
            .any(|file| file["path"].as_str().unwrap().ends_with("/stage.log")));
        let log = fixture
            .output
            .join(stage["directory"].as_str().unwrap())
            .join("stage.log");
        assert!(fs::metadata(log).unwrap().len() <= 8193);
    }
    for name in ["cache", "progress", "perf"] {
        assert!(!dense.join("dense").join(name).exists());
    }
    let result = events.last().unwrap();
    assert!(fixture
        .output
        .join(result["sog"].as_str().unwrap())
        .is_file());
    assert!(fixture
        .output
        .join(result["spz"].as_str().unwrap())
        .is_file());
    assert_eq!(result["gaussians"], 4);
}

#[test]
fn disabled_dense_trains_from_completed_sparse_dataset() {
    let fixture = Fixture::new();
    fixture
        .write(json!({"settings":{"densification":{"enabled":false},"training":{"iterations":2}}}));
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    let state = fixture.state();
    assert!(state["completed"].get("densification").is_none());
    assert!(state["completed"].get("reconstruction").is_some());
    assert!(!events.iter().any(|event| event["stage"] == "densification"));
}

#[test]
fn interrupted_dense_restarts_and_preserves_completed_reconstruction() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .env("FIXTURE_FAIL_DENSE", "1")
        .args(["--request", fixture.request.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let before = fixture.state();
    assert!(before["completed"].get("reconstruction").is_some());
    assert!(before["completed"].get("densification").is_none());
    let partial = fixture
        .output
        .join(before["stages"]["densification"].as_str().unwrap());
    fixture.write(json!({"resume":true}));
    let (success, events) = fixture.run();
    assert!(success, "{events:?}");
    assert_eq!(
        fixture.state()["completed"]["reconstruction"],
        before["completed"]["reconstruction"]
    );
    assert_ne!(
        fixture.state()["stages"]["densification"],
        before["stages"]["densification"]
    );
    assert!(!partial.exists());
}

#[test]
fn incomplete_or_inference_only_training_checkpoints_cannot_complete() {
    for key in ["FIXTURE_INFERENCE_ONLY", "FIXTURE_TRUNCATE_CHECKPOINT"] {
        let fixture = Fixture::new();
        let output = fixture
            .command()
            .env(key, "1")
            .args(["--request", fixture.request.to_str().unwrap()])
            .output()
            .unwrap();
        assert!(!output.status.success(), "{key}");
        let events = String::from_utf8(output.stdout).unwrap();
        assert!(events.contains("no full resume checkpoint"), "{events}");
        assert!(fixture.state()["completed"].get("training").is_none());
        assert!(fixture.state()["completed"].get("reconstruction").is_some());
    }
}

#[test]
fn spz_count_mismatch_prevents_completion() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .env("FIXTURE_SPZ_DELTA", "-1")
        .args(["--request", fixture.request.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("Export Gaussian counts disagree"));
    assert!(fixture.state()["completed"].get("export").is_none());
}

#[test]
fn standalone_image_folder_scan_resumes_and_rejects_changed_source() {
    let fixture = Fixture::new();
    let images = fixture.root.path().join("photos");
    fs::create_dir_all(&images).unwrap();
    for index in 0..3 {
        fs::write(images.join(format!("{index}.jpg")), b"input image").unwrap();
    }
    let args = [
        "scan",
        images.to_str().unwrap(),
        "--output",
        fixture.output.to_str().unwrap(),
        "--max-cap",
        "4",
        "--no-densification",
    ];
    let output = fixture
        .command()
        .env("FIXTURE_FAIL_SELECTION", "1")
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let state = fixture.state();
    assert!(state["completed"].get("densification").is_none());
    assert_eq!(state["result"]["gaussians"], 4);
    let output = fixture
        .command()
        .args(args)
        .arg("--resume")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    fs::write(images.join("0.jpg"), b"changed input").unwrap();
    let output = fixture
        .command()
        .args(args)
        .arg("--resume")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout)
        .contains("Resume input, settings, cap or engine versions changed"));
}

#[test]
fn standalone_video_url_download_is_durable_and_reused_on_resume() {
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
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 14\r\nConnection: close\r\n\r\nsource fixture").unwrap();
    });
    let url = format!("http://{address}/video");
    let args = [
        "scan",
        &url,
        "--output",
        fixture.output.to_str().unwrap(),
        "--max-cap",
        "4",
    ];
    let output = fixture.command().args(args).output().unwrap();
    server.join().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(
        fs::read(fixture.output.join(".worker/input/source.mp4")).unwrap(),
        b"source fixture"
    );
    let output = fixture
        .command()
        .args(args)
        .arg("--resume")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

// Runs in its own test process so SIGKILL cannot be caught by Rust unwinding.
#[test]
#[ignore = "subprocess fixture for the frozen-parent death regression"]
fn frozen_parent_fixture() {
    use std::time::{Duration, Instant};
    let Some(root) = std::env::var_os("GUERRILLA_FROZEN_FIXTURE") else {
        return;
    };
    let root = PathBuf::from(root);
    let mut runtime = process_fixture::Runtime::new().unwrap();
    runtime.lock_gpu().unwrap();
    let script = r#"import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
writer='''import signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
stream=open(sys.argv[1],'ab',buffering=0)
print('ready',flush=True)
while True:
    stream.write(b'x')
    time.sleep(.005)
'''
child=subprocess.Popen([sys.executable,'-c',writer,str(Path(sys.argv[1]).with_name('counter'))],stdout=subprocess.PIPE,text=True)
assert child.stdout.readline().strip()=='ready'
ready=Path(sys.argv[1])
temporary=ready.with_suffix('.tmp')
temporary.write_text(json.dumps([os.getpid(),child.pid]))
temporary.replace(ready)
time.sleep(60)
"#;
    let mut child = process_fixture::ManagedChild::spawn_for_test(
        &[
            "python3".into(),
            "-c".into(),
            script.into(),
            root.join("pids.json").to_string_lossy().into_owned(),
        ],
        &root.join("engine.log"),
        std::path::Path::new(env!("CARGO_BIN_EXE_guerrilla-pipeline")),
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !root.join("pids.json").is_file() {
        assert!(Instant::now() < deadline, "Engine fixture did not launch");
        std::thread::sleep(Duration::from_millis(10));
    }
    let _frozen = child.freeze().unwrap();
    let ready = root.join("frozen.json");
    let temporary = ready.with_extension("tmp");
    fs::write(
        &temporary,
        serde_json::to_vec(&json!({"guardian":child.child.id()})).unwrap(),
    )
    .unwrap();
    fs::rename(temporary, ready).unwrap();
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}

struct FrozenHelper {
    root: tempfile::TempDir,
    child: std::process::Child,
    pids: Vec<u32>,
    guardian: u32,
}
impl FrozenHelper {
    fn launch() -> Self {
        use std::time::{Duration, Instant};
        let root = tempfile::tempdir().unwrap();
        let child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "frozen_parent_fixture",
                "--ignored",
                "--nocapture",
            ])
            .env("GUERRILLA_FROZEN_FIXTURE", root.path())
            .env("GPU_LOCK_PATH", root.path().join("gpu.lock"))
            .env_remove("WORKER_PARENT_PID")
            .env_remove("GPU_LOCK_WAIT")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut helper = Self {
            root,
            child,
            pids: vec![],
            guardian: 0,
        };
        let deadline = Instant::now() + Duration::from_secs(15);
        while !helper.root.path().join("frozen.json").is_file() {
            assert!(
                helper.child.try_wait().unwrap().is_none(),
                "Frozen helper exited early"
            );
            assert!(
                Instant::now() < deadline,
                "Frozen helper did not become ready"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        helper.pids =
            serde_json::from_slice(&fs::read(helper.root.path().join("pids.json")).unwrap())
                .unwrap();
        let frozen: Value =
            serde_json::from_slice(&fs::read(helper.root.path().join("frozen.json")).unwrap())
                .unwrap();
        helper.guardian = frozen["guardian"].as_u64().unwrap() as u32;
        for pid in &helper.pids {
            assert!(
                process_state(*pid).starts_with('T'),
                "Engine writer {pid} is not frozen"
            );
        }
        assert!(
            !process_state(helper.guardian).starts_with('T'),
            "Guardian must remain runnable during snapshots"
        );
        helper
    }
}
impl Drop for FrozenHelper {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if self.guardian > 0 {
            unsafe {
                libc::kill(-(self.guardian as i32), libc::SIGKILL);
            }
        }
        if let Some(engine) = self.pids.first() {
            unsafe {
                libc::kill(-(*engine as i32), libc::SIGKILL);
            }
        }
    }
}
fn process_state(pid: u32) -> String {
    let result = Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output()
        .unwrap();
    String::from_utf8_lossy(&result.stdout).trim().to_string()
}
#[test]
fn killing_parent_during_snapshot_freeze_stops_tree_and_releases_gpu() {
    use std::time::{Duration, Instant};
    let mut helper = FrozenHelper::launch();
    unsafe {
        libc::kill(helper.child.id() as i32, libc::SIGKILL);
    }
    helper.child.wait().unwrap();
    let released_by = Instant::now() + Duration::from_secs(10);
    let lock = fs::File::options()
        .read(true)
        .write(true)
        .open(helper.root.path().join("gpu.lock"))
        .unwrap();
    loop {
        let live = helper
            .pids
            .iter()
            .chain(std::iter::once(&helper.guardian))
            .any(|pid| {
                let state = process_state(*pid);
                !state.is_empty() && !state.starts_with('Z')
            });
        let released = fs2::FileExt::try_lock_exclusive(&lock).is_ok();
        if released {
            for pid in helper.pids.iter().chain(std::iter::once(&helper.guardian)) {
                let state = process_state(*pid);
                assert!(
                    state.is_empty() || state.starts_with('Z'),
                    "GPU lock released while {pid} is live: {state}"
                );
            }
            fs2::FileExt::unlock(&lock).unwrap();
        }
        if !live && released {
            break;
        }
        assert!(
            Instant::now() < released_by,
            "Frozen tree remained live or kept the GPU lock"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}
#[test]
fn engine_leader_exit_does_not_resume_frozen_descendant_writers() {
    use std::time::{Duration, Instant};
    let mut helper = FrozenHelper::launch();
    let counter = helper.root.path().join("counter");
    let frozen_bytes = fs::read(&counter).unwrap();
    unsafe {
        libc::kill(helper.pids[0] as i32, libc::SIGKILL);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let state = process_state(helper.pids[1]);
        assert!(
            state.is_empty() || state.starts_with('T') || state.starts_with('Z'),
            "Guardian resumed a writer while its parent still holds a snapshot freeze: {state}"
        );
        assert_eq!(
            fs::read(&counter).unwrap(),
            frozen_bytes,
            "Frozen checkpoint inputs changed during guardian cleanup"
        );
        if state.is_empty() || state.starts_with('Z') {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "Guardian did not clean up the engine's descendants"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        helper.child.try_wait().unwrap().is_none(),
        "Snapshot parent should still be alive"
    );
}

#[test]
fn source_download_retries_transient_http_failures_without_leaking_signed_urls() {
    use std::{
        io::{Read, Write},
        net::TcpListener,
    };
    let fixture = Fixture::new();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for attempt in 0..3 {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];
            let _ = socket.read(&mut request);
            if attempt < 2 {
                socket.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
            } else {
                socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 14\r\nConnection: close\r\n\r\nsource fixture").unwrap();
            }
        }
    });
    let destination = fixture.root.path().join("download.mp4");
    let url = format!("http://{address}/source?signature=private-token");
    let output = fixture
        .command()
        .args([
            "--download",
            &url,
            "--destination",
            destination.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    server.join().unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "{stdout}");
    assert_eq!(fs::read(destination).unwrap(), b"source fixture");
    assert!(stdout.contains("attempt 3 of 3"));
    assert!(stdout.contains("Retrying source download"));
    assert!(!stdout.contains("private-token"));
    assert!(!stdout.contains(&url));
}

#[test]
fn source_download_does_not_retry_permanent_http_errors() {
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
        socket
            .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap();
    });
    let destination = fixture.root.path().join("download.mp4");
    let output = fixture
        .command()
        .args([
            "--download",
            &format!("http://{address}/missing"),
            "--destination",
            destination.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    server.join().unwrap();
    assert!(!output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("after 1 attempt(s)"));
    assert!(!stdout.contains("retrying"));
    assert!(!destination.exists());
}

# Native enrolled worker

`guerrilla-worker` manages the existing protocol-v2 Guerrilla worker identity and assignments. The Tauri app controls this executable; Docker runs it in the foreground. The worker never creates local scans or installs an engine.

## Commands

```sh
guerrilla-worker --config /absolute/path/profile.json preflight
guerrilla-worker --config /absolute/path/profile.json enroll --token-stdin
guerrilla-worker --config /absolute/path/profile.json start
guerrilla-worker --config /absolute/path/profile.json status
guerrilla-worker --config /absolute/path/profile.json drain
guerrilla-worker --config /absolute/path/profile.json stop
guerrilla-worker --config /absolute/path/profile.json disconnect
guerrilla-worker --config /absolute/path/profile.json logs
```

Commands return JSON. `run` (also the default command) owns the foreground worker loop; `start` launches that same executable with `run`, detached from its launcher. Repeated `start` attaches to the existing profile lock. GUI exit has no effect on the worker. `stop` interrupts the entire engine process tree and retains verified recovery data; `drain` finishes the current assignment and exits without accepting another. Disconnect stops the worker and deletes its local credential; it does not delete the server's identity/history.

The profile uses camelCase fields: `controlUrl`, `scratchDirectory`, `pythonPath`, `ffmpegPath`, `nodePath`, `converterPath`, `lichtfeldPath`, `densificationPluginPath`, `spirulaPath`, and `startAtLogin`. Runtime paths refer to user-provided engines or the vendored worker dependency bundle. Credentials and private command/status files live beside the profile; the default profile lives in the user's configuration directory. Changing the control-plane origin while enrolled is rejected. Unix state has owner-only permissions; Windows state uses a protected owner-only ACL. State and downloaded file replacement are atomic, including on Windows.

`status` returns `state`, `running`, `connected`, `workerId` when enrolled, `message`, `gpus`, `engines`, and optional `scanId`, `attemptId`, `backend`, `stage`, and `progress`. `running` comes from the OS profile lock and remains authoritative when an error is being retried. `preflight` returns the raw pipeline dependency report. The legacy `--preflight` flag returns protocol registration (`protocolVersion`, `engines`, `gpus`, `healthy`) for existing headless setup tooling. `logs` returns at most 200 redacted lines from the last 64 KiB of the daemon log. Credentials never appear in command results. Enrollment reads at most 4098 stdin bytes and requires a token of at most 4096 bytes.

## Runtime resolution

The pipeline executable is `WORKER_PIPELINE_BIN`, or the sibling `guerrilla-pipeline(.exe)`. The dependency bundle is `WORKER_RUNTIME_ROOT`, or sibling `runtime/` with `python/`, `node/`, `ffmpeg/`, `converter/`, and optional `models/`. Explicit profile paths override defaults. Existing `PYTHON`, `NODE_BIN`, `SPLAT_TRANSFORM_BIN`, `LICHTFELD_BIN`, `LICHTFELD_DENSIFICATION_PLUGIN`, and `SPIRULA_BIN` variables remain supported. Models use `TORCH_HOME` when bundled. The packaged app passes `PIPELINE_ROOT`; the pipeline also resolves its packaged helper directory. Python import/prefix overrides and Node import/loader overrides from the user’s shell are removed; `PYTHONNOUSERSITE=1` isolates the bundled dependencies while engine paths and native-library search paths remain available.

Docker may use `WORKER_CONFIG_FILE`, `WORKER_CONTROL_URL`, `WORKER_CREDENTIAL_FILE`, `WORKER_SCRATCH`, and `WORKER_ENROLLMENT_TOKEN`. The token is removed from the process environment before running jobs. Store credentials and scratch on durable volumes; mount user-installed engines separately.

## Execution guarantees

Every event/upload/commit carries the scan, attempt, and lease fence. A separate watchdog stops execution at the last acknowledged lease deadline even when heartbeats fail. User cancellation is confirmed before local scan data is deleted. Pipeline restoration only reads manifest-listed paths and verifies size and SHA-256, including when signed URLs are refreshed. Multipart upload records server ETags and checks local content did not change before completion.

Checkpoint acknowledgements have two meanings: `.archive-acks/ID.ready` means all declared stage products were frozen locally; `.archive-acks/ID` means the server acknowledged the canonical commit. One active upload and one latest pending snapshot bound upload backlog. Local files survive transient failures. Completion waits for all required uploads; scan scratch is removed only after the server acknowledges completion. Inherited verified stage objects participate in the new canonical checkpoint without downloading unrelated stages. Upload events have one serialized sequence with execution events and do not overwrite the desktop's current execution stage.

An OS lock prevents duplicate processes for a profile and a global GPU lock interoperates with the existing Python agent's device lock. Unix process groups and pipeline parent supervision contain engine descendants; Windows pipeline jobs and tree termination provide the corresponding containment. macOS desktop workers follow the console user's session. Linux follows a verified `XDG_SESSION_ID` via `loginctl`, alongside normal logout/session signals. Containers use foreground signal handling independently of a graphical login session.

## Validation

```sh
cargo test -p guerrilla-worker
cargo clippy -p guerrilla-worker --all-targets -- -D warnings
```

Contract tests cover portable retention/symlink safety, origin and runtime identity checks, atomic digest-verified downloads, refreshed identity rejection, multipart fencing/completion, frozen stage snapshots, canonical completion ordering, inherited objects, pending snapshot coalescing, cancellation cleanup, lease expiry/process-tree shutdown, stdout-closed cancellation, early-error lease cleanup, detached startup/repeated attachment, and bounded redacted diagnostics. Test pipeline fixtures exercise lifecycle and protocol boundaries without requiring a licensed engine; real GPU/engine compatibility remains an integration check on the target platform.

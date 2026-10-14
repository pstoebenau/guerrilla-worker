# Cross-platform worker

## Supported platforms

Desktop runtime assets and package jobs cover Windows x64, Linux x64, and macOS
14 or later on Apple Silicon and Intel. CI runs checks on Windows Server 2022,
Ubuntu 24.04, and macOS 15 on both architectures. The desktop package workflow
builds four distributions with the same app and protocol and publishes them in
one GitHub Release after every push to `main` succeeds on all platforms.

GPU job support depends on the engine and hardware. Windows/Linux workers require
compatible installed engines and GPU drivers; LichtFeld requires NVIDIA CUDA.
Windows/Linux GPU libraries and model weights are downloaded and verified from
Setup instead of included in the app. The owner supplies the NVIDIA driver;
`libcuda.so.1` remains external to the managed GPU runtime.
Apple Silicon uses Spirula. Intel Mac distributions support worker management;
the pinned engine distribution does not provide Intel Mac GPU execution.
The Linux NVIDIA Docker image is x64 only.

Windows ARM and Linux ARM do not have bundled runtime targets. Other Linux
distributions need separate runtime validation before claiming support beyond the
Ubuntu build baseline. Android, iOS, and ChromeOS have no native worker packages.

## Architecture

The desktop is a Tauri v2 shell with a Svelte/TypeScript interface. It invokes the
native worker's management commands and reads structured status. The worker runs
independently of Tauri, so closing or quitting the interface does not own a scan's
lifetime. Login startup launches the desktop in background mode; it starts the
separate worker and provides a tray entry to reopen management. Logout ends the
user-session worker; this is not a boot-time machine service.

The macOS desktop distribution requires macOS 14 or later. Bundled native Python
wheels set the minimum: NumPy targets macOS 14 and OpenCV targets macOS 13.
The app's declared minimum follows its dependencies rather than Tauri alone.

`rust/worker` owns outbound protocol v2, credentials, leases, scoped transfers,
background archive commits, selective restoration, startup recovery, GPU exclusion,
and local management. `rust/pipeline` owns execution stages, manifests, adapters,
checkpoint retention, resume, export commands and Gaussian limits. Focused Python
bindings keep native CV and engine libraries out of the Rust process. JSON requests
are validated at the boundary and new helper modules undergo strict static checking.
Legacy standalone scan entry points remain compatible.

## Engine ownership

For the desktop app and headless native installations, the owner installs engine
binaries and the LichtFeld densification plugin through
upstream channels. Setup accepts explicit paths, and discovery provides sensible
installed locations. Availability is checked per engine. LichtFeld is CUDA-only;
Spirula uses the host Vulkan/MoltenVK hardware implementation. Missing or incompatible
engines are reported rather than replaced. There is no engine download/build button.

Desktop supporting runtimes are bundled at build time, outside committed source
files:

- `runtime/python`: pinned relocatable CPython and hash-locked dependencies.
- `runtime/ffmpeg`: pinned FFmpeg/FFprobe with build configuration and notices.
- `runtime/node`: pinned Node runtime, including its upstream license.
- `runtime/converter`: lockfile-pinned PlayCanvas splat-transform and dependencies.
- `runtime/setup`: pinned GPU setup recipe, requirements and installer helper.
- Worker data `runtimes/cuda-<identity>`: verified GPU Python libraries and models, installed on demand on Windows/Linux.
- `pipeline`: Python helper sources and settings schema.

`packaging/runtime-lock.json` records exact asset hashes. Python lock files are
platform-specific where CUDA bindings require different wheels. Build preparation
verifies archives before extracting and verifies executables before final staging.
Python/Node/FFmpeg are automatically selected from the bundle. A compatible, complete GPU installation supplies Python/model paths after setup. Incomplete or mismatched installs are not selected; previous runtime identities remain available and profiles/checkpoints are preserved. Advanced overrides
are possible. Neither shell PATH nor an ambient Python environment is required in
an installed desktop app. The GPU driver remains an operating-system prerequisite.

## Operations

Use Overview to inspect connectivity, execution and engine availability. Setup owns
server URL, enrollment, engine paths, scratch storage, and login startup. Logs are
bounded and redact credentials. Enrollment tokens go through the worker process's
stdin; they are not saved in settings or command arguments.

Closing a window hides it in the tray. Quit interface leaves the worker running.
Stop worker interrupts execution and retains checkpoints. Finish and pause allows
the current scan to complete, then stops claiming assignments. Disconnect requires
a stopped worker, removes its local credential, and preserves scan checkpoints.
Server changes require disconnecting first; revoke the old identity from the old
server's Workers page when appropriate.

Normal status distinguishes an unenrolled, stopped, starting, idle, busy, draining,
or failed worker. An unavailable native runtime in browser development is explicitly
reported. The browser preview does not simulate enrollment or processing.

## Build and validation

Run `bun run build` on each target OS. The script stages both native
executables with Tauri target suffixes and the runtime resources, then builds the
platform package. Rust and frontend checks can run independently of engine installs.
Build tools are required only by developers. Owner-installed engines must be
supplied for desktop/native GPU preflight and complete workflow validation.

The Linux Docker image uses the same Rust executables with a fully bundled
runtime. Unlike desktop packages, it includes built LichtFeld and Spirula engines,
the compatible densification plugin, and model assets. `bun run build:docker`
verifies the checksum-locked engine artifacts and builds `Dockerfile.runtime`,
then `Dockerfile`. There is no host engine installation, engine bind mount, or
first-job engine/model download. The host provides its NVIDIA driver and NVIDIA
Container Toolkit. Credentials, scratch storage, and shared GPU locks stay in
host-managed mounts; container execution does not use login startup. See the
[Docker build and run examples](../README.md#linux-docker-image).

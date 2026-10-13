# Desktop worker

The Tauri app manages an independent `guerrilla-worker` process. Closing its
window hides the interface in the tray; quitting the interface leaves the worker
running. On Linux desktops without tray support, closing exits only the
interface. Use **Stop worker** to stop execution or **Finish job, then pause** to
complete the current assignment before stopping.

The desktop distribution supplies Python, Node, FFmpeg, and the export converter.
LichtFeld and Spirula must be installed by the user. The app detects their
existing installations and supports manual executable selection. Desktop builds
do not build, download, or bundle engines. The separate
[Linux Docker image](../README.md#linux-docker-image) includes both engines, the
densification plugin, and models without host engine installations.
The macOS desktop bundle requires macOS 14 or later because its bundled native
Python wheels target macOS 14 (NumPy) and macOS 13 (OpenCV).

Build the complete native distribution with `bun run build` from the
repository root. For frontend development, run `bun install`, then `bun run dev`
here. Browser previews explicitly indicate that native worker access is
unavailable. `bun run check`, `bun run build`, and `bun test tests` validate the
frontend. Native checks use `cargo test --manifest-path src-tauri/Cargo.toml`.

## Isolated development and installed-app QA

Set `GUERRILLA_WORKER_DATA_DIR` to an absolute, nonroot directory before launching
the app to keep development or QA separate from your regular worker profile:

```sh
GUERRILLA_WORKER_DATA_DIR=/tmp/guerrilla-worker-qa \
  './src-tauri/target/release/bundle/macos/Guerrilla Worker.app/Contents/MacOS/guerrilla-worker-desktop'
```

The override applies to configuration, enrollment, status, command files, logs,
and the default scratch directory. Relative paths, filesystem roots, parent
traversal, and a symlink at the selected data directory are rejected. The same
owner-only data permissions apply. The app loads and migrates profiles only
inside the selected directory; it never copies the normal worker's profile or
credentials into an isolated directory.

Leave **Start worker when I log in** disabled in isolation mode. Saving isolated
settings does not modify the normal app's global login registration, and enabling
login startup is rejected because the test environment variable would not be
present at a later login. Repeated launches with the same override path reopen
that test profile's interface. Isolated profiles use separate single-instance
registration, so launching a test profile cannot reopen a normal enrolled app.

The real-worker transport integration test can be run with:

```sh
GUERRILLA_DESKTOP_TEST_WORKER=/absolute/path/to/guerrilla-worker \
  cargo test --manifest-path src-tauri/Cargo.toml -- --ignored
```

It uses its own temporary profile and verifies real status/log/control responses,
action validation, and enrollment error redaction without using saved credentials.

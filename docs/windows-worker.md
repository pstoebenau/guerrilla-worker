# Windows desktop worker

Install Guerrilla Worker for Windows x64 from the prepared installer package.
Keep the setup executable and **every matching `.bin` payload file** together in
one folder, then run setup. CUDA, PyTorch, and reconstruction libraries make this
a large package; splitting payloads avoids single-executable installer limits.
No terminal, Python, Node, or separate WebView2 installation is required.

The separate [Linux Docker image](../README.md#linux-docker-image) includes its
engines, plugins, and models; this Windows desktop package uses your installations.

The installer runs under your account and includes Microsoft's signed Evergreen
Standalone WebView2 installer. It installs that bundled runtime offline only
when an adequate runtime is missing. Microsoft's shared Evergreen runtime can
receive its normal automatic updates afterward. There is no installer-time
runtime download. Worker registration and execution are not started by setup;
opening the GUI after installation is optional and initially unchecked.

## Setup

1. Install LichtFeld or Spirula yourself. The Windows desktop app and installer
   do not build, download, or bundle either engine. Review detected paths in
   **Setup**, or browse to the existing engine executable. If using LichtFeld densification, select the
   installed plugin folder too.
2. Choose your Guerrilla server and scratch folder. The app supplies its own
   pinned Python, Node, FFmpeg, export converter, and integration dependencies;
   your GPU drivers come from your existing installation. Run **Save and check
   dependencies** and resolve reported hardware or engine compatibility issues.
3. Paste an enrollment token from the server's Workers page and choose **Enroll
   and start**. Tokens travel through stdin, never through command arguments or
   saved settings. Jobs and training settings are assigned by the server.
4. Closing the window keeps the tray and background worker running. Quitting the
   interface also leaves the worker running. **Stop worker** shuts execution down
   and retains verified checkpoints. **Finish job, then pause** finishes the
   current assignment before stopping.

**Start worker when I log in** is optional and managed in Setup. The worker runs
under your Windows account during your logged-in session, without a boot-time
system service. Reopening the app attaches to the existing background worker.

Settings, credentials, logs, and default scratch are stored in
`%LOCALAPPDATA%\Guerrilla\worker`, protected for your account. Existing Windows
tray profiles keep their enrollment, engine paths, and scratch checkpoints;
Python now comes from the worker distribution. Enrollment binds the worker to
its original server. Stop and disconnect before changing servers. Disconnect
keeps checkpoints; revoke the old identity on the original server if needed.

## Updates and removal

Quit the interface before upgrading or uninstalling. Setup also asks any old
Windows tray app to close and uses the installed background worker's normal
shutdown command before replacing or removing files. If shutdown cannot be
confirmed, installation/removal stops so you can inspect logs and retry. Setup
never force-kills a scan or starts a worker afterward.

Upgrades and uninstall preserve settings, enrollment, checkpoints, and your
engine installations. Uninstall removes this app's login registration and app
files. Microsoft's shared WebView2 runtime stays installed for other apps.
Installer packages are unsigned initially; Windows may require you to accept
its standard unsigned-app prompt. No release is automatically published.

## Building

On a Windows x64 machine with the repository's pinned Bun and Rust toolchain,
and licensed Visual Studio/Build Tools with the x64 C++ tools installed.

Use a build disk with at least 70 GiB free. CUDA/PyTorch dependencies can make
GPU distributions about 20 GiB; download caches, staging, installer data, and
installed-app smoke testing need additional copies. The optional package CI job
checks capacity and requires a larger or self-hosted runner when standard hosted
runner storage is insufficient. Runtime dependencies are never removed to make
a package fit.

When manually dispatching package CI, enable `packages` and use
`linux_package_runner`, `windows_package_runner`, and `macos_package_runner` to
select suitable larger or dedicated self-hosted runner labels. Defaults use the
standard hosted labels; change them when their disks fail the capacity check.
The smaller macOS base runtime needs 12 GiB free; Windows and Linux CUDA package
jobs require 70 GiB.
Custom labels must identify runners with the matching OS, native build tools,
and a clean test account. Automatic check jobs keep their normal hosted matrix.

Then run:

```powershell
bun install --frozen-lockfile
bun run build
```

The prepared app is `dist/windows-app`. Installer artifacts are under
`desktop/src-tauri/target/release/bundle/installer`: the setup executable, all payload slices, SHA-256 checksums,
and the embedded WebView2 payload's source/version/signature manifest.

To package an already prepared, self-contained app folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build-windows.ps1 `
  -SourceDirectory C:\absolute\prepared-app -Version 0.3.0 `
  -Destination C:\absolute\installer-output
```

The packaging script validates required runtime files, rejects staged scan-engine
executables, obtains and checks Microsoft's signed x64 standalone WebView2
payload at build time, and uses the pinned, signature-verified Inno Setup 6.4.3
compiler cache. A cached WebView2 installer remains the build's recorded payload;
remove `%LOCALAPPDATA%\Guerrilla\build-tools\MicrosoftEdgeWebView2RuntimeInstallerX64.exe`
to refresh it for a later build. All installer parts must travel together.

The Windows build discovers the licensed x64 MSVC CRT/OpenMP redistributable
through `VCToolsRedistDir` or `vswhere`, verifies Microsoft's signatures, and
includes app-local DLLs beside the native executables and bundled Python.
There is no machine-wide Visual C++ redistributable installation. If discovery
fails, install the C++ build tools or point `VCToolsRedistDir` to their
`VC\Redist\MSVC\14.x` folder. The runtime's `windows-crt/manifest.json` records
the copied files, versions, signatures, and checksums.

[Microsoft's offline deployment instructions](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution#offline-deployment)
and [Inno Setup's disk-spanning requirements](https://jrsoftware.org/ishelp/topic_setup_diskspanning.htm)
explain the two packaging choices.

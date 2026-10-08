# Windows tray worker

Download the unsigned Windows x64 setup executable from the worker repository's
GitHub Releases, install it, and open **Guerrilla Worker** from Start. A tray icon
appears and the settings/enrollment window opens on first launch. No terminal,
PowerShell, Bun, Node or .NET installation is required.

## Setup and enrollment

1. Review detected Python, Spirula, LichtFeld and plugin paths. Browse to your
   existing installations if needed. Python must have the pipeline dependencies;
   FFmpeg and `nvidia-smi` must be on PATH. Exactly one NVIDIA GPU must be visible.
2. The server defaults to `https://guerrilla.dad`; change it before enrolling if
   using your own Guerrilla server. Click **Check dependencies**.
3. Open the server, create an enrollment token on its Workers page, and paste it
   into the masked enrollment field. Click **Enroll and start**. Tokens are sent
   through stdin to the agent, never saved in configuration or command arguments.
4. Close the settings window to keep running in the tray. The tray menu offers
   settings, start/stop, the server, logs, manual release downloads, and quit.

Startup at Windows sign-in is enabled by default. Stop the worker, uncheck
**Start at Windows sign-in**, and save settings to disable it. The app runs as
your Windows user, not a service before login. It does not restart after crashes.
Unexpected tray exit causes its agent to shut down. Only one tray instance runs
per Windows session; launching it again opens the existing settings window.

**Stop worker** and **Quit** cancel active execution using the existing shutdown
path and retain verified checkpoints. Use the server's drain action when the
current job should finish first. A shutdown timeout leaves the app running for
inspection instead of force-killing a scan. Tray status reports process liveness;
the server reports connectivity and job status.

Settings, enrollment, launch logs and scratch are stored in
`%LOCALAPPDATA%\Guerrilla\worker`, protected for your account and SYSTEM.
Existing portable-worker settings in that folder are reused. Credentials bind
an enrolled identity to its original server. To change servers, stop and use
**Disconnect**, then change the URL and enroll again. Disconnect preserves
checkpoints; revoke the old identity from the old server's Workers page.

## Updates and removal

**Download updates…** opens GitHub Releases. Quit the tray app before installing
a new version. Settings and checkpoints survive upgrades and uninstallation;
uninstallation removes startup registration. Installers are unsigned initially
and Windows may show a security warning. There is no automatic updater.

## Building

On Windows with Bun 1.4.2 and .NET SDK 8.0.425 or newer installed:

```powershell
bun install --frozen-lockfile
bun run build:windows
bun run build:installer
```

The native tray executable is `dist/windows-app/guerrilla-worker.exe`. Keep the
whole folder together when using it without installation. Installer output is
under `dist/installer`. The build downloads checksum-verified Node 22.22.0,
pins .NET 8.0.31, includes upstream notices and runs app self-tests. Installer
builds use the signed upstream Inno Setup 6.4.3 compiler, installed under the
user's Guerrilla build-tools cache. No scan engines or model assets are bundled.

Every push to the independent worker repository's `main` runs
`windows-release.yml`: checks, installer build, install/uninstall smoke test,
and a GitHub Release with the installer, matching source and checksums. Each
successful push receives a distinct `windows-vMAJOR.MINOR.RUN_NUMBER` tag.
Failed checks produce no release. Updates to the parent monorepo alone do not
trigger a worker release. Local builds remain uncommitted generated artifacts.

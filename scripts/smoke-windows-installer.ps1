$ErrorActionPreference = 'Stop'
# This production installer test is runner-only. Fail closed if the
# account has an existing worker/profile/login entry; /D alone does
# not isolate the installer's normal profile and registry cleanup.
$normalProfile = Join-Path $env:LOCALAPPDATA 'Guerrilla/worker'
function Assert-FreshWorkerAccount([bool]$AllowStartup = $false) {
  if (Test-Path -LiteralPath $normalProfile) { throw 'Installer smoke requires a fresh dedicated runner without a normal worker profile.' }
  if (@(Get-Process -Name 'guerrilla-worker-desktop', 'guerrilla-worker', 'guerrilla-pipeline' -ErrorAction SilentlyContinue).Count -gt 0) { throw 'Existing worker processes prevent an isolated installer smoke test.' }
  $runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run')
  try {
    if ($null -ne $runKey) {
      foreach ($entry in @('GuerrillaWorker', 'guerrilla-worker-desktop')) {
        if (($runKey.GetValueNames() -contains $entry) -and !($AllowStartup -and $entry -eq 'guerrilla-worker-desktop')) { throw 'Existing Guerrilla login registration prevents installer smoke testing.' }
      }
    }
  } finally { if ($null -ne $runKey) { $runKey.Dispose() } }
}
function Assert-InstalledCrt([string]$InstallDirectory) {
  $crtDirectory = Join-Path $InstallDirectory 'resources/runtime/windows-crt'
  $manifest = Get-Content -LiteralPath (Join-Path $crtDirectory 'manifest.json') -Raw | ConvertFrom-Json
  if (@($manifest.files).Count -eq 0) { throw 'Installed CRT manifest contains no DLLs.' }
  foreach ($entry in $manifest.files) {
    foreach ($directory in @($InstallDirectory, $crtDirectory)) {
      $file = Join-Path $directory $entry.fileName
      if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Installed CRT copy is missing: $file" }
      if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
        throw "Installed CRT copy differs from its verified manifest: $file"
      }
    }
  }
}
Assert-FreshWorkerAccount
foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Guerrilla Worker', 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B837DEDE-FDC1-453A-90C9-B3B010299A61}_is1')) {
  if (Test-Path -LiteralPath $key) { throw 'Installer smoke requires a fresh dedicated runner without a registered Guerrilla installation.' }
}
$installerFolder = Join-Path $env:GITHUB_WORKSPACE 'desktop/src-tauri/target/release/bundle/nsis'
$installers = @(Get-ChildItem -LiteralPath $installerFolder -Filter '*-setup.exe' -File)
if ($installers.Count -ne 1) { throw 'Expected exactly one generated Windows installer.' }
$installer = $installers[0]
if (@(Get-ChildItem -LiteralPath $installerFolder -Filter ($installer.BaseName + '-*.bin') -File).Count -ne 0) { throw 'Expected a single-file installer without data slices.' }
$qaRoot = Join-Path $env:RUNNER_TEMP ('guerrilla-installer-' + [Guid]::NewGuid().ToString('N'))
$installed = Join-Path $qaRoot 'app'
$profileDirectory = Join-Path $qaRoot 'profile'
$null = New-Item -ItemType Directory -Path $profileDirectory -Force
$config = Join-Path $profileDirectory 'config.json'
$scratch = Join-Path $profileDirectory 'scratch'
$null = New-Item -ItemType Directory -Path $scratch -Force
$sentinel = Join-Path $scratch 'retained-checkpoint.txt'
[IO.File]::WriteAllText($sentinel, 'preserved-checkpoint', [Text.UTF8Encoding]::new($false))
$trackedEnvironment = @('WORKER_CREDENTIAL_FILE', 'WORKER_PIPELINE_BIN', 'WORKER_RUNTIME_ROOT', 'WORKER_CRT_ROOT', 'WORKER_SCRATCH')
$previousEnvironment = @{}
foreach ($name in $trackedEnvironment) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$uninstaller = Join-Path $installed 'uninstall.exe'
$removed = $false
try {
  $legacy = Join-Path $qaRoot 'legacy-inno-setup.exe'
  Invoke-WebRequest -Uri 'https://github.com/pstoebenau/guerrilla-worker/releases/download/v0.3.13/guerrilla-worker-0.3.13-windows-x86_64-setup.exe' -OutFile $legacy
  if ((Get-FileHash -LiteralPath $legacy -Algorithm SHA256).Hash.ToLowerInvariant() -ne '0c5819f0e54fe17aecebc395b49206c6f2736897bfce08e616687edef081ac78') { throw 'Pinned legacy Inno installer checksum mismatch.' }
  $legacyDirectory = Join-Path $qaRoot 'old-inno-app'
  $oldSetup = Start-Process -FilePath $legacy -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$legacyDirectory`"") -Wait -PassThru
  if ($oldSetup.ExitCode -ne 0) { throw 'Could not install the pinned legacy Inno release for migration testing.' }
  $legacyKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B837DEDE-FDC1-453A-90C9-B3B010299A61}_is1'
  if (!(Test-Path -LiteralPath $legacyKey)) { throw 'Legacy installer did not register its uninstaller.' }
  $startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
  $null = New-Item -Path $startupKey -Force
  Set-ItemProperty -LiteralPath $startupKey -Name 'guerrilla-worker-desktop' -Value ('"' + (Join-Path $legacyDirectory 'guerrilla-worker-desktop.exe') + '" --background')
  $legacyOwnedFiles = @(Get-ChildItem -LiteralPath $legacyDirectory -Recurse -File | ForEach-Object { $_.FullName })
  $setup = Start-Process -FilePath $installer.FullName -ArgumentList @('/S', "/D=$installed") -Wait -PassThru
  if ($setup.ExitCode -ne 0) { throw "Installer failed ($($setup.ExitCode))" }
  if (Test-Path -LiteralPath $legacyKey) { throw 'Migration retained the legacy Inno uninstaller registration.' }
  $legacyDeadline = [DateTime]::UtcNow.AddSeconds(20)
  while ((Test-Path -LiteralPath (Join-Path $legacyDirectory 'unins000.exe')) -and [DateTime]::UtcNow -lt $legacyDeadline) { Start-Sleep -Milliseconds 250 }
  foreach ($file in $legacyOwnedFiles) {
    if (Test-Path -LiteralPath $file) { throw "Migration left a legacy owned file: $file" }
  }
  $nsisKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Guerrilla Worker'
  if (!(Test-Path -LiteralPath $nsisKey)) { throw 'NSIS uninstaller registration is missing.' }
  $expectedStartup = '"' + (Join-Path $installed 'guerrilla-worker-desktop.exe') + '" --background'
  if ((Get-ItemPropertyValue -LiteralPath $startupKey -Name 'guerrilla-worker-desktop') -ne $expectedStartup) { throw 'Migration lost or misdirected startup registration.' }
  Assert-InstalledCrt $installed
  # Reinstall must remove obsolete app-owned GPU files, rather than
  # mixing the base runtime with dependencies from older releases.
  $obsolete = Join-Path $installed 'resources/runtime/obsolete-runtime.fixture'
  [IO.File]::WriteAllText($obsolete, 'obsolete app-owned dependency')
  $upgrade = Start-Process -FilePath $installer.FullName -ArgumentList @('/S', "/D=$installed") -Wait -PassThru
  if ($upgrade.ExitCode -ne 0 -or (Test-Path -LiteralPath $obsolete)) { throw 'Upgrade failed to replace the app-owned runtime cleanly.' }
  if ((Get-ItemPropertyValue -LiteralPath $startupKey -Name 'guerrilla-worker-desktop') -ne $expectedStartup) { throw 'Upgrade lost startup registration.' }
  Assert-InstalledCrt $installed
  $requiredFiles = @(
    'guerrilla-worker-desktop.exe', 'guerrilla-worker.exe', 'guerrilla-pipeline.exe',
    'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE', 'README.md',
    'resources/runtime/manifest.json', 'resources/runtime/windows-crt/manifest.json',
    'resources/runtime/node/node.exe',
    'resources/runtime/ffmpeg/ffmpeg.exe', 'resources/runtime/ffmpeg/ffprobe.exe',
    'resources/runtime/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs'
  )
  foreach ($relative in $requiredFiles) {
    if (!(Test-Path -LiteralPath (Join-Path $installed $relative) -PathType Leaf)) { throw "Installed app is missing $relative" }
  }
  $runtime = Join-Path $installed 'resources/runtime'
  $profile = [ordered]@{
    controlUrl = 'https://example.invalid'; scratchDirectory = $scratch;
    nodePath = (Join-Path $runtime 'node/node.exe');
    ffmpegPath = (Join-Path $runtime 'ffmpeg/ffmpeg.exe');
    converterPath = (Join-Path $runtime 'converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs');
    startAtLogin = $false
  }
  [IO.File]::WriteAllText($config, ($profile | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
  $configHash = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash
  $env:WORKER_CREDENTIAL_FILE = Join-Path $profileDirectory 'credentials.json'
  $env:WORKER_PIPELINE_BIN = Join-Path $installed 'guerrilla-pipeline.exe'
  $env:WORKER_RUNTIME_ROOT = $runtime
  $env:WORKER_CRT_ROOT = Join-Path $runtime 'windows-crt'
  $env:WORKER_SCRATCH = $scratch
  $worker = Join-Path $installed 'guerrilla-worker.exe'
  $statusOutput = & $worker --config $config status
  if ($LASTEXITCODE -ne 0) { throw 'Installed worker status command failed.' }
  $status = ($statusOutput -join "`n") | ConvertFrom-Json
  if ($status.state -ne 'unenrolled' -or $status.connected -or $status.running) { throw 'Fresh installed worker must be unenrolled, disconnected and stopped.' }
  if (Test-Path -LiteralPath $env:WORKER_CREDENTIAL_FILE) { throw 'Status/preflight unexpectedly created enrollment.' }
  Assert-FreshWorkerAccount $true
  $remove = Start-Process -FilePath $uninstaller -ArgumentList @('/S', "_?=$installed") -Wait -PassThru
  if ($remove.ExitCode -ne 0) { throw "Uninstall failed ($($remove.ExitCode))" }
  $removed = $true
  if (Test-Path -LiteralPath $nsisKey) { throw 'Uninstall retained its NSIS registration.' }
  # Wait for any outstanding file handles before checking removed payloads.
  $deadline = [DateTime]::UtcNow.AddSeconds(15)
  while ((Test-Path -LiteralPath (Join-Path $installed 'guerrilla-worker-desktop.exe')) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
  foreach ($relative in $requiredFiles) {
    if (Test-Path -LiteralPath (Join-Path $installed $relative)) { throw "Uninstall retained owned app file $relative" }
  }
  if (!(Test-Path -LiteralPath $config) -or (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash -ne $configHash) { throw 'Uninstall changed the isolated worker profile.' }
  if (!(Test-Path -LiteralPath $sentinel) -or [IO.File]::ReadAllText($sentinel) -ne 'preserved-checkpoint') { throw 'Uninstall removed the retained checkpoint.' }
  Assert-FreshWorkerAccount
} finally {
  if ($null -ne (Get-Variable legacyDirectory -ErrorAction SilentlyContinue)) {
    $oldUninstaller = Join-Path $legacyDirectory 'unins000.exe'
    if (Test-Path -LiteralPath $oldUninstaller) { Start-Process -FilePath $oldUninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait | Out-Null }
  }
  if (!$removed -and (Test-Path -LiteralPath $uninstaller)) {
    $cleanup = Start-Process -FilePath $uninstaller -ArgumentList @('/S', "_?=$installed") -Wait -PassThru
    if ($cleanup.ExitCode -ne 0) { Write-Warning "Installer smoke cleanup failed ($($cleanup.ExitCode))." }
  }
  foreach ($name in $trackedEnvironment) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process') }
}

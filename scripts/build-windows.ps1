param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Destination = ''
)
# Packaging only. build-desktop.ts supplies the tested app and vendored runtime.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows installer on Windows.' }
if ($Version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'Version must have three or four numeric components.' }
$root = Split-Path $PSScriptRoot -Parent
if (!(Test-Path -LiteralPath $SourceDirectory -PathType Container)) { throw 'SourceDirectory must be an existing prepared app folder.' }
$sourceItem = Get-Item -LiteralPath $SourceDirectory
if ($sourceItem.PSProvider.Name -ne 'FileSystem') { throw 'SourceDirectory must be a filesystem folder.' }
if ($sourceItem.FullName -eq [IO.Path]::GetPathRoot($sourceItem.FullName)) { throw 'SourceDirectory must be a prepared app folder below a filesystem root.' }
$source = $sourceItem.FullName.TrimEnd('\')
if ($Destination -eq '') { $Destination = Join-Path $root 'dist/installer' }
$destinationPath = [IO.Path]::GetFullPath($Destination)
if ($destinationPath -eq [IO.Path]::GetPathRoot($destinationPath)) { throw 'Destination must be an installer folder below a filesystem root.' }
$output = $destinationPath.TrimEnd('\')
if ($output -eq $source -or $output.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer output must be outside the prepared app folder.' }
foreach ($required in @(
    'guerrilla-worker-desktop.exe', 'guerrilla-worker.exe', 'guerrilla-pipeline.exe', 'LICENSE', 'README.md',
    'resources/runtime/manifest.json',
    'resources/runtime/node/node.exe', 'resources/runtime/ffmpeg/ffmpeg.exe',
    'resources/runtime/ffmpeg/ffprobe.exe', 'resources/runtime/converter/node_modules/@playcanvas/splat-transform/bin/cli.mjs'
)) {
    if (!(Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) { throw "Prepared app is missing $required" }
}
$cache = Join-Path $env:LOCALAPPDATA 'Guerrilla/build-tools'
$prerequisites = Join-Path $source 'prerequisites'
foreach ($directory in @($output, $cache, $prerequisites)) { $null = New-Item -ItemType Directory -Path $directory -Force }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
function Assert-Signature([string]$Path, [string]$SubjectPattern) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch $SubjectPattern) {
        throw "Unexpected or invalid code signature: $([IO.Path]::GetFileName($Path))"
    }
    return $signature
}
# Official Microsoft developer page: 'Evergreen Standalone Installer / x64' is
# 2124701. 2124703 is the network bootstrapper and MUST NOT be used here.
# https://developer.microsoft.com/en-us/microsoft-edge/webview2/
# https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
$webViewUrl = 'https://go.microsoft.com/fwlink/?linkid=2124701'
$webViewName = 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
$webViewCache = Join-Path $cache $webViewName
if (!(Test-Path -LiteralPath $webViewCache -PathType Leaf)) {
    $temporary = $webViewCache + '.' + [Guid]::NewGuid().ToString('N') + '.download.exe'
    try {
        Invoke-WebRequest -Uri $webViewUrl -OutFile $temporary -UseBasicParsing
        $null = Assert-Signature $temporary '(^|,\s*)CN=Microsoft Corporation(,|$)'
        if ((Get-Item -LiteralPath $temporary).Length -lt 50000000) { throw 'Expected the full offline WebView2 standalone installer, not a bootstrapper.' }
        Move-Item -LiteralPath $temporary -Destination $webViewCache
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
$webViewSignature = Assert-Signature $webViewCache '(^|,\s*)CN=Microsoft Corporation(,|$)'
if ((Get-Item -LiteralPath $webViewCache).Length -lt 50000000) { throw 'Cached WebView2 payload is not a full offline standalone installer.' }
$webViewPayload = Join-Path $prerequisites $webViewName
Copy-Item -LiteralPath $webViewCache -Destination $webViewPayload -Force
$webViewVersion = (Get-Item -LiteralPath $webViewPayload).VersionInfo.ProductVersion
$webViewHash = (Get-FileHash -LiteralPath $webViewPayload -Algorithm SHA256).Hash.ToLowerInvariant()
$webViewManifest = [ordered]@{
    schemaVersion = 1; architecture = 'x64'; distribution = 'Evergreen Standalone Installer';
    source = $webViewUrl; documentation = 'https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution';
    fileName = $webViewName; sha256 = $webViewHash; productVersion = $webViewVersion;
    signer = $webViewSignature.SignerCertificate.Subject; signatureStatus = 'Valid'
}
$webViewManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $prerequisites 'WebView2Runtime-manifest.json') -Encoding UTF8
# Reuse the pinned Inno Setup cache and verify its upstream signed installer on
# every build, including builds using an already installed cached compiler.
$innoSetup = Join-Path $cache 'innosetup-6.4.3.exe'
if (!(Test-Path -LiteralPath $innoSetup -PathType Leaf)) {
    $temporary = $innoSetup + '.' + [Guid]::NewGuid().ToString('N') + '.download.exe'
    try {
        Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe' -OutFile $temporary -UseBasicParsing
        $null = Assert-Signature $temporary '^CN=Pyrsys B\.V\.,'
        Move-Item -LiteralPath $temporary -Destination $innoSetup
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
$null = Assert-Signature $innoSetup '^CN=Pyrsys B\.V\.,'
$compiler = Join-Path $cache 'inno-6.4.3/ISCC.exe'
if (!(Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $installDirectory = Split-Path $compiler -Parent
    $install = Start-Process -FilePath $innoSetup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', "/DIR=`"$installDirectory`"") -WindowStyle Hidden -Wait -PassThru
    if ($install.ExitCode -ne 0 -or !(Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Could not install the build-only Inno Setup compiler.' }
}
# ISCC's launcher PE metadata does not identify the compiler engine version.
# windows-installer.iss enforces the pin using Inno's actual version constant.
$baseName = "Guerrilla-Worker-$Version-windows-x64-setup"
# Remove only artifacts from this exact version so older extra slices cannot be
# mistaken for part of a smaller rebuilt package. Other releases remain intact.
Get-ChildItem -LiteralPath $output -File | Where-Object {
    $_.Name -eq ($baseName + '.exe') -or $_.Name -like ($baseName + '-*.bin') -or
    $_.Name -eq ($baseName + '-SHA256SUMS.txt') -or $_.Name -eq ($baseName + '-WebView2.json')
} | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
& $compiler "/DAppVersion=$Version" "/DAppSource=$source" "/DInstallerOutput=$output" (Join-Path $root 'packaging/windows-installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$setupFile = Join-Path $output ($baseName + '.exe')
$slices = @(Get-ChildItem -LiteralPath $output -File | Where-Object { $_.Name -like ($baseName + '-*.bin') })
if (!(Test-Path -LiteralPath $setupFile -PathType Leaf) -or $slices.Count -ne 0) { throw 'Expected one self-contained installer executable without data slices.' }
$artifacts = @((Get-Item -LiteralPath $setupFile)) + $slices
$checksumLines = @($artifacts | Sort-Object Name | ForEach-Object { (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $_.Name })
[IO.File]::WriteAllLines((Join-Path $output ($baseName + '-SHA256SUMS.txt')), $checksumLines, [Text.UTF8Encoding]::new($false))
$webViewManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $output ($baseName + '-WebView2.json')) -Encoding UTF8
Write-Output "Windows installer prepared: $setupFile"
Write-Output "Single-file setup.exe is ready. GPU dependencies are downloaded and verified during app setup. No release was published."

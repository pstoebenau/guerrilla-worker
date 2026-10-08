param([string]$Version = '0.1.0', [switch]$Installer)
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'Version must have three or four numeric components.' }
$root = Split-Path $PSScriptRoot -Parent
$destination = Join-Path $root 'dist/windows-app'
$runtime = Join-Path $destination 'runtime'
$pipeline = Join-Path $runtime 'pipeline'
$notices = Join-Path $destination 'notices'
$cache = Join-Path $env:LOCALAPPDATA 'Guerrilla/build-tools'
foreach ($directory in @($destination, $runtime, $pipeline, $notices, $cache)) { $null = New-Item -ItemType Directory -Path $directory -Force }

& bun build (Join-Path $root 'agent/windows.ts') --target=node --outfile (Join-Path $runtime 'worker.mjs')
if ($LASTEXITCODE) { throw 'Agent build failed.' }
& dotnet publish (Join-Path $root 'windows/Guerrilla.Worker.csproj') -c Release -r win-x64 --self-contained true -p:Version=$Version -o $destination
if ($LASTEXITCODE) { throw 'Tray application build failed.' }

# Node ships the complete runtime LICENSE. Pin and verify its archive from the
# upstream checksum inventory; engines and model weights are never bundled.
$nodeVersion = '22.22.0'
$archiveName = "node-v$nodeVersion-win-x64.zip"
$archive = Join-Path $cache $archiveName
$checksums = (Invoke-WebRequest "https://nodejs.org/dist/v$nodeVersion/SHASUMS256.txt" -UseBasicParsing).Content
$match = [regex]::Match($checksums, "(?m)^([a-f0-9]{64})\s+" + [regex]::Escape($archiveName) + '\r?$')
if (!$match.Success) { throw 'Node archive missing from upstream checksums.' }
if (!(Test-Path -LiteralPath $archive)) { Invoke-WebRequest "https://nodejs.org/dist/v$nodeVersion/$archiveName" -OutFile $archive -UseBasicParsing }
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $match.Groups[1].Value) { throw 'Node checksum mismatch.' }
$nodeDirectory = Join-Path $cache "node-v$nodeVersion-win-x64"
Expand-Archive -LiteralPath $archive -DestinationPath $cache -Force
Copy-Item -LiteralPath (Join-Path $nodeDirectory 'node.exe') -Destination $runtime
Copy-Item -LiteralPath (Join-Path $nodeDirectory 'LICENSE') -Destination (Join-Path $notices 'Node-LICENSE.txt')
foreach ($file in Get-ChildItem (Join-Path $root 'pipeline') -File) {
    if ($file.Extension -eq '.py' -or $file.Name -eq 'scan-settings.schema.json') { Copy-Item -LiteralPath $file.FullName -Destination $pipeline }
}
Copy-Item -LiteralPath (Join-Path $root 'LICENSE') -Destination $destination
Copy-Item -LiteralPath (Join-Path $root 'packages/protocol/LICENSE') -Destination (Join-Path $notices 'Protocol-LICENSE.txt')
Copy-Item -LiteralPath (Join-Path $root 'docs/windows-worker.md') -Destination (Join-Path $destination 'README.md')
Copy-Item -LiteralPath (Join-Path $root 'windows/THIRD_PARTY_NOTICES.txt') -Destination $notices
foreach ($document in @{
    'Dotnet-LICENSE.txt' = 'https://raw.githubusercontent.com/dotnet/runtime/v8.0.31/LICENSE.TXT';
    'Dotnet-NOTICES.txt' = 'https://raw.githubusercontent.com/dotnet/runtime/v8.0.31/THIRD-PARTY-NOTICES.TXT';
    'WinForms-LICENSE.txt' = 'https://raw.githubusercontent.com/dotnet/winforms/v8.0.31/LICENSE.TXT';
    'WinForms-NOTICES.txt' = 'https://raw.githubusercontent.com/dotnet/winforms/v8.0.31/THIRD-PARTY-NOTICES.TXT'
}.GetEnumerator()) {
    Invoke-WebRequest $document.Value -OutFile (Join-Path $notices $document.Key) -UseBasicParsing
}
$selfTest = Start-Process -FilePath (Join-Path $destination 'guerrilla-worker.exe') -ArgumentList '--self-test' -WindowStyle Hidden -Wait -PassThru
if ($selfTest.ExitCode) { throw "Tray self-tests failed ($($selfTest.ExitCode))." }
Write-Output "Tray package built and self-tested: $destination"

if ($Installer) {
    $compiler = Join-Path $cache 'inno-6.4.3/ISCC.exe'
    if (!(Test-Path -LiteralPath $compiler)) {
        $setup = Join-Path $cache 'innosetup-6.4.3.exe'
        Invoke-WebRequest 'https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe' -OutFile $setup -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $setup
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '^CN=Pyrsys B\.V\.,') { throw 'Unexpected Inno Setup signature.' }
        $installDirectory = Split-Path $compiler -Parent
        $install = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', "/DIR=`"$installDirectory`"") -WindowStyle Hidden -Wait -PassThru
        if ($install.ExitCode) { throw 'Could not install the build-only installer compiler.' }
    }
    & $compiler "/DAppVersion=$Version" (Join-Path $root 'windows/installer.iss')
    if ($LASTEXITCODE) { throw 'Installer build failed.' }
}

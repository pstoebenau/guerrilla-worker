import { access, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { windowsPowerShellEnvironment } from './windows-powershell';

interface CrtFile {
  fileName: string;
  source: string;
  sourceFileVersion: string;
  productVersion: string;
  signer: string;
  signerThumbprint: string;
  sha256: string;
  keptNewerPythonRuntime: boolean;
}
interface CrtManifest {
  schemaVersion: 1;
  architecture: 'x64';
  discovery: string;
  sourceDirectory: string;
  redistributableVersion: string;
  files: CrtFile[];
  documentation: string;
}

// Windows SDK tools handle Authenticode/catalog signatures and native file
// versions. The script receives its destination through a dedicated environment
// variable; no user path is interpolated into executable PowerShell source.
const vendorScript = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
$stage = $env:GUERRILLA_CRT_STAGE_DIRECTORY
$python = Join-Path $stage 'python'
if (!(Test-Path -LiteralPath (Join-Path $python 'python.exe') -PathType Leaf)) { throw 'Extract the vendored Windows Python before preparing its app-local CRT.' }
$candidateRoots = @()
if ($env:VCToolsRedistDir) { $candidateRoots += [pscustomobject]@{ Path = $env:VCToolsRedistDir; Discovery = 'VCToolsRedistDir' } }
$programFiles86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
if ($programFiles86) {
  $vswhere = Join-Path $programFiles86 'Microsoft Visual Studio/Installer/vswhere.exe'
  if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
    $installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0) { throw 'vswhere could not discover licensed MSVC build tools.' }
    if ($installation) {
      $redist = Join-Path ([string]($installation | Select-Object -First 1)) 'VC/Redist/MSVC'
      if (Test-Path -LiteralPath $redist -PathType Container) {
        $versions = @(Get-ChildItem -LiteralPath $redist -Directory | Where-Object { $_.Name -match '^14\.[0-9]+\.[0-9]+(?:\.[0-9]+)?$' } | Sort-Object { [Version]$_.Name } -Descending)
        foreach ($version in $versions) { $candidateRoots += [pscustomobject]@{ Path = $version.FullName; Discovery = 'vswhere' } }
      }
    }
  }
}
$source = $null
$sourceFiles = @()
$required = @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
$filePattern = '^(?i:msvcp140(?:_[a-z0-9_]+)?|vcruntime140(?:_[a-z0-9_]+)?|concrt140|vcomp140)\.dll$'
foreach ($candidate in $candidateRoots) {
  if (!(Test-Path -LiteralPath $candidate.Path -PathType Container)) { continue }
  $base = Get-Item -LiteralPath $candidate.Path
  $x64 = if ($base.Name -eq 'x64') { $base.FullName } else { Join-Path $base.FullName 'x64' }
  if ($base.Name -match '^Microsoft\.VC14[0-9]+\.CRT$' -and $base.Parent.Name -eq 'x64') { $x64 = $base.Parent.FullName }
  if (!(Test-Path -LiteralPath $x64 -PathType Container)) { continue }
  if (((Get-Item -LiteralPath $x64).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'MSVC redistributable source cannot be a filesystem link.' }
  $folders = @(Get-ChildItem -LiteralPath $x64 -Directory | Where-Object { $_.Name -match '^Microsoft\.VC14[0-9]+\.(CRT|OpenMP)$' })
  $crt = @($folders | Where-Object { $_.Name.EndsWith('.CRT') } | Sort-Object Name -Descending | Select-Object -First 1)
  if ($crt.Count -eq 0) { continue }
  $chosen = @($crt[0]) + @($folders | Where-Object { $_.Name -eq $crt[0].Name.Replace('.CRT', '.OpenMP') })
  $files = @()
  foreach ($folder in $chosen) {
    if (($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'MSVC redistributable source cannot be a filesystem link.' }
    $files += @(Get-ChildItem -LiteralPath $folder.FullName -File | Where-Object { $_.Name -match $filePattern })
  }
  $names = @($files | ForEach-Object { $_.Name })
  if (@($required | Where-Object { $_ -notin $names }).Count -eq 0) { $source = $candidate; $sourceFiles = $files; break }
}
if ($null -eq $source) { throw 'Licensed x64 MSVC CRT redistributable files are missing. Install Visual Studio/Build Tools with C++ x64 tools, or set VCToolsRedistDir to its VC/Redist/MSVC/14.x folder. Guerrilla never downloads DLLs from third-party sites or installs a machine-wide redistributable.' }
function CheckedSignature([string]$file) {
  $entry = Get-Item -LiteralPath $file
  if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'MSVC runtime DLLs cannot be filesystem links.' }
  $signature = Get-AuthenticodeSignature -LiteralPath $file
  if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
    $signer = if ($null -eq $signature.SignerCertificate) { '(none)' } else { $signature.SignerCertificate.Subject }
    throw ('Unexpected or invalid Microsoft runtime DLL signature: {0}; status={1}; message={2}; signer={3}; path={4}' -f $entry.Name, $signature.Status, $signature.StatusMessage, $signer, $entry.FullName)
  }
  return $signature
}
function NumericVersion([string]$value) {
  $match = [regex]::Match($value, '^14\.[0-9]+\.[0-9]+(?:\.[0-9]+)?')
  if (!$match.Success) { throw 'Expected a supported v14 Microsoft C++ runtime file version.' }
  return [Version]$match.Value
}
$destination = Join-Path $stage 'windows-crt'
$null = New-Item -ItemType Directory -Path $destination -Force
$records = @()
foreach ($file in ($sourceFiles | Sort-Object Name)) {
  $signature = CheckedSignature $file.FullName
  $sourceVersion = NumericVersion $file.VersionInfo.FileVersion
  $selected = $file
  $keptNewer = $false
  $pythonFile = Join-Path $python $file.Name
  if (Test-Path -LiteralPath $pythonFile -PathType Leaf) {
    $existing = Get-Item -LiteralPath $pythonFile
    $existingSignature = CheckedSignature $existing.FullName
    $existingVersion = NumericVersion $existing.VersionInfo.FileVersion
    if ($existingVersion -gt $sourceVersion) {
      # Preserve newer bundled CPython runtime DLLs instead of downgrading them.
      $selected = $existing; $signature = $existingSignature; $keptNewer = $true
    }
  }
  $target = Join-Path $destination $file.Name
  Copy-Item -LiteralPath $selected.FullName -Destination $target -Force
  if ($selected.FullName -ne $pythonFile) { Copy-Item -LiteralPath $selected.FullName -Destination $pythonFile -Force }
  $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
  if ((Get-FileHash -LiteralPath $pythonFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'App-local CRT copy verification failed.' }
  $records += [ordered]@{
    fileName = $file.Name; source = $selected.FullName; sourceFileVersion = $selected.VersionInfo.FileVersion;
    productVersion = $selected.VersionInfo.ProductVersion; signer = $signature.SignerCertificate.Subject;
    signerThumbprint = $signature.SignerCertificate.Thumbprint; sha256 = $hash; keptNewerPythonRuntime = $keptNewer
  }
}
[ordered]@{
  schemaVersion = 1; architecture = 'x64'; discovery = $source.Discovery; sourceDirectory = $source.Path;
  redistributableVersion = ($sourceFiles | Where-Object { $_.Name -eq 'msvcp140.dll' } | Select-Object -First 1).VersionInfo.FileVersion; files = @($records);
  documentation = 'https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170'
} | ConvertTo-Json -Depth 6 -Compress
`;

function parseManifest(value: unknown): CrtManifest {
  if (!value || typeof value !== 'object') throw new Error('Invalid Windows CRT preparation response');
  const manifest = value as Record<string, unknown>;
  if (manifest.schemaVersion !== 1 || manifest.architecture !== 'x64' || typeof manifest.sourceDirectory !== 'string'
    || typeof manifest.discovery !== 'string' || typeof manifest.redistributableVersion !== 'string'
    || typeof manifest.documentation !== 'string' || !Array.isArray(manifest.files)) throw new Error('Invalid Windows CRT preparation metadata');
  for (const raw of manifest.files) {
    if (!raw || typeof raw !== 'object') throw new Error('Invalid Windows CRT file metadata');
    const file = raw as Record<string, unknown>;
    if (typeof file.fileName !== 'string' || !/^(?:msvcp140(?:_[a-z0-9_]+)?|vcruntime140(?:_[a-z0-9_]+)?|concrt140|vcomp140)\.dll$/i.test(file.fileName)
      || typeof file.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(file.sha256)
      || typeof file.signer !== 'string' || !/(^|,\s*)CN=Microsoft Corporation(,|$)/.test(file.signer)
      || typeof file.source !== 'string' || typeof file.sourceFileVersion !== 'string' || typeof file.productVersion !== 'string'
      || typeof file.signerThumbprint !== 'string' || typeof file.keptNewerPythonRuntime !== 'boolean') throw new Error('Invalid Windows CRT file identity');
  }
  for (const required of ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']) {
    if (!manifest.files.some((file: CrtFile) => file.fileName.toLowerCase() === required)) throw new Error(`Missing app-local runtime ${required}`);
  }
  return manifest as unknown as CrtManifest;
}

/** Vendor licensed, signed x64 CRT/OpenMP files for a Windows desktop runtime. */
export async function vendorWindowsCrt(stagingRuntime: string): Promise<void> {
  if (process.platform !== 'win32') return;
  if (!path.isAbsolute(stagingRuntime)) throw new Error('Windows CRT staging directory must be absolute');
  await access(path.join(stagingRuntime, 'python', 'python.exe'));
  const encodedScript = Buffer.from(vendorScript, 'utf16le').toString('base64');
  const child = Bun.spawn(['powershell.exe', '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-EncodedCommand', encodedScript], {
    env: { ...windowsPowerShellEnvironment(), GUERRILLA_CRT_STAGE_DIRECTORY: stagingRuntime },
    stdin: 'ignore', stdout: 'pipe', stderr: 'pipe', windowsHide: true,
  });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
  ]);
  if (exitCode !== 0) throw new Error(`Windows app-local CRT preparation failed: ${stderr.trim() || 'Install the licensed MSVC C++ build tools.'}`);
  let decoded: unknown;
  try { decoded = JSON.parse(stdout.replace(/^\uFEFF/, '').trim()); }
  catch { throw new Error('Windows CRT preparation returned invalid JSON'); }
  const manifest = parseManifest(decoded);
  const destination = path.join(stagingRuntime, 'windows-crt');
  await mkdir(destination, { recursive: true });
  await writeFile(path.join(destination, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  await writeFile(path.join(destination, 'NOTICE.txt'), [
    'Microsoft Visual C++ v14 x64 app-local CRT and optional OpenMP runtime',
    'Copyright Microsoft Corporation. All rights reserved.',
    'Copied from licensed Visual Studio/MSVC redistributable files on the build machine.',
    'Redistribution is governed by the Microsoft Software License Terms for the licensed build tools.',
    'https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170',
    'The source/version, valid Microsoft signature, and SHA-256 for each file are recorded in manifest.json.',
    'No machine-wide runtime installation or third-party DLL download is performed.',
    '',
  ].join('\n'));
  console.log(`Vendored ${manifest.files.length} signed app-local Microsoft runtime DLLs.`);
}

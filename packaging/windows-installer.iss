#if VER != EncodeVer(6, 4, 3)
  #error This installer requires the pinned Inno Setup compiler version 6.4.3
#endif

#ifndef AppVersion
  #error AppVersion must be supplied by scripts/build-windows.ps1
#endif
#ifndef AppSource
  #error AppSource must point to the prepared self-contained app folder
#endif
#ifndef InstallerOutput
  #error InstallerOutput must be supplied by scripts/build-windows.ps1
#endif

[Setup]
AppId={{B837DEDE-FDC1-453A-90C9-B3B010299A61}
AppName=Guerrilla Worker
AppVersion={#AppVersion}
AppPublisher=Guerrilla
AppPublisherURL=https://guerrilla.dad
DefaultDirName={localappdata}\Programs\Guerrilla Worker
DefaultGroupName=Guerrilla Worker
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#InstallerOutput}
OutputBaseFilename=Guerrilla-Worker-{#AppVersion}-windows-x64-setup
; GPU libraries and model weights are installed separately during app setup.
DiskSpanning=no
Compression=lzma2/fast
SolidCompression=no
LZMAUseSeparateProcess=yes
WizardStyle=modern
UninstallDisplayIcon={app}\guerrilla-worker-desktop.exe
AppMutex=dad.guerrilla.worker-sim,Local\GuerrillaWorkerTray
CloseApplications=no
RestartApplications=no
LicenseFile={#AppSource}\LICENSE
DisableProgramGroupPage=yes
SetupLogging=yes

[Files]
; First, so prerequisite extraction doesn't scan the application payload.
Source: "{#AppSource}\prerequisites\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy
Source: "{#AppSource}\guerrilla-worker-desktop.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\guerrilla-worker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\guerrilla-pipeline.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\*.dll"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#AppSource}\resources\*"; DestDir: "{app}\resources"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AppSource}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppSource}\prerequisites\WebView2Runtime-manifest.json"; DestDir: "{app}\resources\runtime\notices"; Flags: ignoreversion

[InstallDelete]
; Remove this app's previous bundled runtime, including obsolete dependency files.
; Per-user GPU runtimes, profiles and checkpoints live outside {app} and remain.
Type: filesandordirs; Name: "{app}\resources\runtime"

[Icons]
Name: "{autoprograms}\Guerrilla Worker"; Filename: "{app}\guerrilla-worker-desktop.exe"

[Run]
; Optional GUI launch does not enroll or start the background worker.
Filename: "{app}\guerrilla-worker-desktop.exe"; Description: "Open Guerrilla Worker"; Flags: nowait postinstall skipifsilent unchecked

[Registry]
; Old tray entry points to a filename now used by the headless worker.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "GuerrillaWorker"; Flags: deletevalue uninsdeletevalue
; Tauri autostart uses Cargo's package name. Uninstall removes startup only;
; settings/enrollment/checkpoints and user engine installations are preserved.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "guerrilla-worker-desktop"; Flags: uninsdeletevalue

[Code]
const
  WebViewClient = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebViewInstaller = 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe';

function HasWebViewVersion(RootKey: Integer): Boolean;
var
  Version: String;
  Separator, Major: Integer;
begin
  Result := False;
  if RegQueryStringValue(RootKey, WebViewClient, 'pv', Version) then begin
    Separator := Pos('.', Version);
    if Separator > 1 then begin
      Major := StrToIntDef(Copy(Version, 1, Separator - 1), 0);
      Result := Major >= 105;
    end;
  end;
end;

function WebViewAvailable: Boolean;
begin
  { Microsoft documents HKLM's 32-bit key and HKCU's user key. }
  Result := HasWebViewVersion(HKLM32) or HasWebViewVersion(HKCU32) or HasWebViewVersion(HKCU64);
end;

function StopInstalledWorker: String;
var
  Worker, Profile: String;
  ExitCode: Integer;
begin
  Result := '';
  Worker := ExpandConstant('{app}\guerrilla-worker.exe');
  Profile := ExpandConstant('{localappdata}\Guerrilla\worker\config.json');
  { Do not send new CLI arguments to the old WinForms executable. Its tray
    mutex requires the owner to quit before upgrade, which stops its agent. }
  if FileExists(ExpandConstant('{app}\guerrilla-worker-desktop.exe')) and
     FileExists(Worker) and FileExists(Profile) then begin
    if not Exec(Worker, '--config "' + Profile + '" stop', ExpandConstant('{app}'), SW_HIDE,
                ewWaitUntilTerminated, ExitCode) then begin
      Result := 'Could not stop the installed background worker. Stop it in Guerrilla Worker, quit the interface, then retry.';
    end else if ExitCode <> 0 then begin
      Result := 'The background worker has not confirmed shutdown. Check its logs, stop it, and retry. Saved checkpoints have not been removed.';
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := StopInstalledWorker;
  if Result <> '' then Exit;
  if not WebViewAvailable then begin
    ExtractTemporaryFile(WebViewInstaller);
    if not Exec(ExpandConstant('{tmp}\') + WebViewInstaller, '/silent /install', '', SW_HIDE,
                ewWaitUntilTerminated, ExitCode) then begin
      Result := 'Could not start the bundled offline WebView2 installer. Retry this installer; no runtime download is required.';
    end else if not WebViewAvailable then begin
      NeedsRestart := ExitCode = 3010;
      Result := 'The bundled WebView2 runtime could not be installed (exit code ' + IntToStr(ExitCode) + '). Check the setup log and retry.';
    end;
  end;
end;

function InitializeUninstall: Boolean;
var
  Error: String;
begin
  Error := StopInstalledWorker;
  Result := Error = '';
  if not Result then MsgBox(Error, mbError, MB_OK);
end;

{ No profile/checkpoint deletion, engine install, service registration,
  enrollment, or automatic worker launch occurs during install/uninstall. }

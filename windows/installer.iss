#ifndef AppVersion
  #define AppVersion "0.1.0"
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
OutputDir=..\dist\installer
OutputBaseFilename=Guerrilla-Worker-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\guerrilla-worker.exe
AppMutex=Local\GuerrillaWorkerTray
CloseApplications=no
LicenseFile=..\LICENSE
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\windows-app\guerrilla-worker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\windows-app\runtime\node.exe"; DestDir: "{app}\runtime"; Flags: ignoreversion
Source: "..\dist\windows-app\runtime\worker.mjs"; DestDir: "{app}\runtime"; Flags: ignoreversion
Source: "..\dist\windows-app\runtime\pipeline\*.py"; DestDir: "{app}\runtime\pipeline"; Flags: ignoreversion
Source: "..\dist\windows-app\runtime\pipeline\scan-settings.schema.json"; DestDir: "{app}\runtime\pipeline"; Flags: ignoreversion
Source: "..\dist\windows-app\notices\*.txt"; DestDir: "{app}\notices"; Flags: ignoreversion
Source: "..\dist\windows-app\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\windows-app\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Guerrilla Worker"; Filename: "{app}\guerrilla-worker.exe"

[Run]
Filename: "{app}\guerrilla-worker.exe"; Description: "Launch Guerrilla Worker"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "GuerrillaWorker"; Flags: uninsdeletevalue

; User settings, enrollment and checkpoints live outside {app} and survive
; upgrades/uninstallation. Startup registration is managed by the tray app.

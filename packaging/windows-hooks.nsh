; Extend Tauri's supported NSIS template; never remove the owner's data directory.
!include "LogicLib.nsh"
!define LEGACY_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\{B837DEDE-FDC1-453A-90C9-B3B010299A61}_is1"

Var GuerrillaRestoreAutostart

!macro StopGuerrillaWorker
  ${If} ${FileExists} "$INSTDIR\guerrilla-worker-desktop.exe"
  ${AndIf} ${FileExists} "$INSTDIR\guerrilla-worker.exe"
  ${AndIf} ${FileExists} "$LOCALAPPDATA\Guerrilla\worker\config.json"
    ClearErrors
    ExecWait '"$INSTDIR\guerrilla-worker.exe" --config "$LOCALAPPDATA\Guerrilla\worker\config.json" stop' $0
    ${If} ${Errors}
    ${OrIf} $0 != 0
      MessageBox MB_ICONSTOP "Stop the background worker and quit Guerrilla Worker before retrying. Saved checkpoints are preserved." /SD IDOK
      Abort
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  ; Inno uses a different uninstall identity and default directory. Let its own
  ; uninstaller remove its inventory before NSIS registers the replacement.
  StrCpy $GuerrillaRestoreAutostart ""
  SetRegView 64
  ReadRegStr $0 HKCU "${LEGACY_UNINSTALL_KEY}" "UninstallString"
  ${If} $0 != ""
    ReadRegStr $GuerrillaRestoreAutostart HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "guerrilla-worker-desktop"
    ReadRegStr $2 HKCU "${LEGACY_UNINSTALL_KEY}" "InstallLocation"
    ClearErrors
    ExecWait '$0 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART' $1
    ${If} ${Errors}
    ${OrIf} $1 != 0
      MessageBox MB_ICONSTOP "The previous Guerrilla Worker installation could not be removed. Quit the app, stop its worker, and retry." /SD IDOK
      Abort
    ${EndIf}
    ReadRegStr $0 HKCU "${LEGACY_UNINSTALL_KEY}" "UninstallString"
    ${If} $0 != ""
      Abort "The previous installer registration is still present. Uninstall the previous version, then retry."
    ${EndIf}
    ${If} $2 != ""
      ; Inno schedules its own executable for removal. Wait for that helper
      ; before writing a replacement into a user-selected identical directory.
      StrCpy $3 0
      ${DoWhile} ${FileExists} "$2\unins000.exe"
        Sleep 250
        IntOp $3 $3 + 1
        ${If} $3 >= 80
          Abort "The previous uninstaller has not finished. Wait a moment and retry."
        ${EndIf}
      ${Loop}
      RMDir /r "$2\resources\runtime"
      RMDir "$2\resources"
      RMDir "$2"
    ${EndIf}
  ${EndIf}
  !insertmacro StopGuerrillaWorker
  ; The packaged runtime is entirely app-owned. Removing it prevents obsolete
  ; libraries from surviving upgrades; downloaded engines live outside here.
  RMDir /r "$INSTDIR\resources\runtime"
  ${If} ${FileExists} "$INSTDIR\resources\runtime\*.*"
    Abort "Close Guerrilla Worker and its tools, then retry the update."
  ${EndIf}
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "GuerrillaWorker"
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ${If} $GuerrillaRestoreAutostart != ""
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "guerrilla-worker-desktop" '$\"$INSTDIR\guerrilla-worker-desktop.exe$\" --background'
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro StopGuerrillaWorker
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  RMDir /r "$INSTDIR\resources\runtime"
  ${If} $UpdateMode != 1
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "GuerrillaWorker"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "guerrilla-worker-desktop"
  ${EndIf}
!macroend

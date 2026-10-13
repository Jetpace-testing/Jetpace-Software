; Jetpace installer.
;
;   makensis /DVERSION=1.0.0 installer\jetpace.nsi
;
; Built by scripts/make_installer.py, which checks the payload is present
; and passes the version in - do not run makensis by hand for a release,
; or the version in the installer and the version in version.json can
; disagree and the updater will compare against the wrong one.
;
; WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
;
; It lays down files and then calls the provisioning binary:
;
;     setup\jetpace-setup.exe --all
;
; The provisioning logic is NOT reimplemented here. initdb flags, pg_hba
; contents, the grant list, the migration runner and the password
; generation are all things that have to be exactly right, are covered by
; tests, and have been running on a development machine for a week. NSIS
; script is the worst language available in which to have a second copy of
; them.
;
; So this file is only: elevate, extract, call, register an uninstaller.
; Everything interesting happens in Python that is already proven.
;
; NOTHING IS DOWNLOADED. A plant PC is often firewalled and sometimes has
; no route out at all, and a download during commissioning is a
; commissioning that fails in front of the customer. PostgreSQL,
; TimescaleDB and Mosquitto all ship inside this package.

Unicode true
SetCompressor /SOLID lzma

!ifndef VERSION
  !error "VERSION is not defined. Build with scripts/make_installer.py."
!endif

!define PRODUCT      "Jetpace"
!define PUBLISHER    "Jetpace Technologies"
!define REGKEY       "Software\Microsoft\Windows\CurrentVersion\Uninstall\Jetpace"
!define SVC_DB       "jetpace-postgres"
!define SVC_SUP      "jetpace-supervisor"

Name                 "${PRODUCT} ${VERSION}"
OutFile              "..\dist\Jetpace-Setup-${VERSION}.exe"
InstallDir           "$PROGRAMFILES64\${PRODUCT}"
InstallDirRegKey     HKLM "Software\${PRODUCT}" "InstallDir"
; Services, HKLM and Program Files all need it, and asking once up front
; is better than failing halfway through a database initialisation.
RequestExecutionLevel admin
ShowInstDetails      show
ShowUninstDetails    show
BrandingText         "${PUBLISHER}"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION}.0"
VIAddVersionKey  "ProductName"    "${PRODUCT}"
VIAddVersionKey  "ProductVersion" "${VERSION}"
VIAddVersionKey  "CompanyName"    "${PUBLISHER}"
VIAddVersionKey  "FileDescription" "${PRODUCT} energy and environmental monitoring"
VIAddVersionKey  "FileVersion"    "${VERSION}"

; ---------------------------------------------------------------------------

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Jetpace needs 64-bit Windows."
    Abort
  ${EndIf}
  SetRegView 64
FunctionEnd

; NOTHING WITH A SIDE EFFECT BELONGS IN .onInit.
;
; This used to stop both services there, so that an upgrade would not try
; to overwrite files a running service holds open. The reasoning was right
; and the placement was wrong: .onInit runs before the Welcome page, so
; merely OPENING the installer stopped a running system - and cancelling
; at the first page left it stopped.
;
; It did exactly that on the development machine: both services down, a
; clean reverse-order shutdown in the supervisor log, and no install. On a
; customer's PC it is worse - somebody double-clicks the file to see what
; it is, monitoring stops, and nothing on screen says so.
;
; The stop now happens at the start of the install section, after the user
; has chosen a directory and pressed Install. By then they have agreed to
; something.

; ---------------------------------------------------------------------------

Section "Jetpace" SEC_MAIN
  SectionIn RO

  ; An upgrade must not be written over a running service: the files are
  ; held open and the install half-applies. Here rather than in .onInit -
  ; see the note above.
  DetailPrint "Stopping any running Jetpace services"
  nsExec::ExecToLog 'sc.exe stop "${SVC_SUP}"'
  nsExec::ExecToLog 'sc.exe stop "${SVC_DB}"'
  Sleep 3000

  SetOutPath "$INSTDIR"
  SetOverwrite on

  DetailPrint "Installing the application"
  File /r "..\dist\app"
  File /r "..\dist\supervisor"
  File /r "..\dist\setup"
  File     "..\dist\version.json"

  DetailPrint "Installing configuration and migrations"
  File /r "..\dist\config"
  File /r "..\dist\migrations"

  ; 157 MB, and the reason this installer is large. It is also the reason
  ; it works without a network.
  DetailPrint "Installing PostgreSQL and TimescaleDB"
  File /r "vendor\pgsql"

  DetailPrint "Installing the MQTT broker"
  File /r "vendor\mosquitto"

  ; The data directory is created here and NEVER shipped. It holds the
  ; database, the generated passwords and the logs, and it must sit
  ; outside app\ because an update replaces app\ wholesale.
  CreateDirectory "$INSTDIR\data"
  CreateDirectory "$INSTDIR\data\logs"
  CreateDirectory "$INSTDIR\update"

  WriteRegStr HKLM "Software\${PRODUCT}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\${PRODUCT}" "Version"    "${VERSION}"
SectionEnd

Section "-Provision" SEC_PROVISION
  DetailPrint "Setting up the database - this takes a minute"
  ; --all does: initdb, configure, generate and restrict passwords, start,
  ; create the role and database, enable TimescaleDB, run the migrations,
  ; apply the grants, write the broker config, and register both services.
  nsExec::ExecToLog '"$INSTDIR\setup\jetpace-setup.exe" --all --root "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Provisioning failed with code $0"
    MessageBox MB_ICONSTOP "Jetpace could not set up its database.$\n$\n\
      The log is in $INSTDIR\data\logs.$\n$\n\
      Nothing has been started. The files are installed, so this can be \
      retried from an administrator command prompt with:$\n$\n\
      $INSTDIR\setup\jetpace-setup.exe --all --root $\"$INSTDIR$\""
    ; Deliberately not Abort: the files are good and a retry is cheap.
    ; Rolling back a part-provisioned database would be the riskier move.
  ${EndIf}
SectionEnd

Section "-Record" SEC_RECORD
  ; The installation record. For the environmental product this is IQ
  ; evidence - this version, on this machine, on this date - so it is
  ; written whether or not anybody asks for it.
  DetailPrint "Writing the installation record"
  nsExec::ExecToLog '"$INSTDIR\setup\jetpace-setup.exe" --record --root "$INSTDIR"'

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr   HKLM "${REGKEY}" "DisplayName"     "${PRODUCT} ${VERSION}"
  WriteRegStr   HKLM "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKLM "${REGKEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKLM "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${REGKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${REGKEY}" "NoRepair" 1

  CreateDirectory "$SMPROGRAMS\${PRODUCT}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT}\Jetpace Dashboard.lnk" \
    "http://127.0.0.1:1885/"
  CreateShortcut "$SMPROGRAMS\${PRODUCT}\Uninstall Jetpace.lnk" \
    "$INSTDIR\Uninstall.exe"
SectionEnd

; ---------------------------------------------------------------------------

Section "Uninstall"
  SetRegView 64

  DetailPrint "Stopping and removing the services"
  nsExec::ExecToLog 'sc.exe stop "${SVC_SUP}"'
  nsExec::ExecToLog 'sc.exe stop "${SVC_DB}"'
  Sleep 3000
  nsExec::ExecToLog 'sc.exe delete "${SVC_SUP}"'
  nsExec::ExecToLog 'sc.exe delete "${SVC_DB}"'

  ; THE DATA DIRECTORY IS NOT REMOVED. It holds the historian, which for
  ; the environmental product is a GMP record retained for the life of the
  ; batch plus years, and for the energy product is what bills were raised
  ; from. An uninstaller that deletes it is a data-loss incident waiting
  ; for somebody to uninstall before reinstalling.
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\supervisor"
  RMDir /r "$INSTDIR\setup"
  RMDir /r "$INSTDIR\pgsql"
  RMDir /r "$INSTDIR\mosquitto"
  RMDir /r "$INSTDIR\migrations"
  RMDir /r "$INSTDIR\update"
  Delete "$INSTDIR\version.json"
  Delete "$INSTDIR\Uninstall.exe"
  ; config\ is left too: a site's device list is the commissioning work.

  RMDir "$INSTDIR"          ; only succeeds when data\ and config\ are gone

  Delete "$SMPROGRAMS\${PRODUCT}\Jetpace Dashboard.lnk"
  Delete "$SMPROGRAMS\${PRODUCT}\Uninstall Jetpace.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT}"

  DeleteRegKey HKLM "${REGKEY}"
  DeleteRegKey HKLM "Software\${PRODUCT}"

  ${If} ${FileExists} "$INSTDIR\data\*.*"
    MessageBox MB_ICONINFORMATION "Jetpace has been removed.$\n$\n\
      The database and logs were kept, in$\n$INSTDIR\data$\n$\n\
      Delete that folder by hand if the readings are no longer needed."
  ${EndIf}
SectionEnd

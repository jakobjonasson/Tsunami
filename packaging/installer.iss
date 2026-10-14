; Inno Setup 6 script. Build: ISCC /DAppVersion=<version> packaging\installer.iss
; (build.ps1 passes the version from tsunami.__version__.)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; NEVER change this AppId. It is how a newer installer finds and upgrades an existing install.
AppId={{6F3A9D52-1C8E-4B7A-9E04-5D2B7C1A8F36}
AppName=Tsunami
AppVersion={#AppVersion}
AppVerName=Tsunami {#AppVersion}
AppPublisher=Tsunami
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Tsunami
DefaultGroupName=Tsunami
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Tsunami-{#AppVersion}-setup
SetupIconFile=..\src\tsunami\gui\assets\tsunami.ico
UninstallDisplayIcon={app}\tsunami.exe
VersionInfoVersion={#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\Tsunami\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Tsunami"; Filename: "{app}\tsunami.exe"
Name: "{userdesktop}\Tsunami"; Filename: "{app}\tsunami.exe"; Tasks: desktopicon

[Run]
; Shown on the final page, unchecked by default. Registration is a per-user config edit.
Filename: "{app}\tsunami.exe"; Parameters: "--register-claude-desktop"; Description: "Register with Claude Desktop (quit it from the system tray and reopen afterwards)"; Flags: postinstall nowait unchecked skipifsilent
Filename: "{app}\tsunami.exe"; Description: "Launch Tsunami"; Flags: postinstall nowait skipifsilent

[Code]
// Data folders are kept on uninstall unless the user opts in.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  LocalData, RoamingData: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  LocalData := ExpandConstant('{localappdata}\Tsunami');
  RoamingData := ExpandConstant('{userappdata}\Tsunami');
  if not (DirExists(LocalData) or DirExists(RoamingData)) then
    Exit;
  // A silent uninstall never asks and always keeps the data.
  if UninstallSilent then
    Exit;
  if MsgBox('Also remove your Tsunami data (history, logs, backups, settings and policy)?' + #13#10 +
            LocalData + #13#10 + RoamingData + #13#10#13#10 +
            'Choose No to keep it.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(LocalData, True, True, True);
    DelTree(RoamingData, True, True, True);
  end;
end;

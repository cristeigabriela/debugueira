; The debugueira installer: per user, no admin, onto the user PATH.
;
; Built by packaging/build.py, which passes the version and the PyInstaller output folder:
;   ISCC /DAppVersion=0.1.0 /DDistDir=..\dist\debugueira debugueira.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\debugueira"
#endif

[Setup]
AppId={{6C1D7E52-2F4B-4E43-9C8A-6B9E3D5F0A17}
AppName=debugueira
AppVersion={#AppVersion}
AppPublisher=Gabriela Cristei
AppPublisherURL=https://github.com/cristeigabriela/debugueira
DefaultDirName={localappdata}\Programs\debugueira
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ChangesEnvironment=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=debugueira-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=debugueira
UninstallDisplayIcon={app}\debugueira.exe
LicenseFile=..\LICENSE

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Run]
Filename: "{app}\debugueira.exe"; Parameters: "skill install"; Description: "Install the agent skill for Claude Code (~/.claude/skills/debugueira)"; Flags: postinstall runhidden skipifsilent
Filename: "winget"; Parameters: "install --id Microsoft.WinDbg --exact --accept-package-agreements --accept-source-agreements"; Description: "Install WinDbg (debugueira needs its cdb)"; Flags: postinstall runhidden skipifsilent; Check: not CdbFound

[UninstallRun]
; Live sessions end by mode: launched targets are killed, attached ones detached.
Filename: "{app}\debugueira.exe"; Parameters: "daemon stop"; Flags: runhidden; RunOnceId: "StopDaemon"

[Messages]
FinishedLabel=debugueira is installed and on your PATH.%n%nOpen a new terminal (or restart your agent) to use it: terminals that are already open still have the old PATH.

[Code]
const
  EnvKey = 'Environment';

function CdbFound: Boolean;
begin
  // The WinDbg app alias, or the Windows SDK debuggers.
  Result := FileExists(ExpandConstant('{localappdata}\Microsoft\WindowsApps\cdbX64.exe'))
    or FileExists(ExpandConstant('{commonpf32}\Windows Kits\10\Debuggers\x64\cdb.exe'))
    or FileExists(ExpandConstant('{commonpf64}\Windows Kits\10\Debuggers\x64\cdb.exe'));
end;

function PathHas(const Paths, Dir: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToPath(const Dir: string);
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    Paths := '';
  if PathHas(Paths, Dir) then
    exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(const Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  History: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}'));
    History := ExpandConstant('{localappdata}\debugueira');
    if DirExists(History) and not UninstallSilent then
      if MsgBox('Also delete your debugging history and settings?' + #13#10 + History,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(History, True, True, True);
  end;
end;

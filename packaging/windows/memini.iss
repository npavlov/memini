[Setup]
AppId=NikPavlov.Memini
AppName=Memini CLI
AppPublisher=Nik Pavlov
AppPublisherURL=https://github.com/npavlov/memini
AppVersion={#PackageVersion}
DefaultDirName={localappdata}\Programs\Memini
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayName=Memini CLI
UninstallDisplayIcon={app}\bin\memini.exe
ChangesEnvironment=yes
OutputBaseFilename={#InstallerName}
Compression=lzma2
SolidCompression=yes

[InstallDelete]
Type: filesandordirs; Name: "{app}\runtime"

[Files]
Source: "{#Payload}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Payload}\bin\memini.exe"; DestDir: "{app}\bin"; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime"

[Code]
function BinPath(): String;
begin
  Result := ExpandConstant('{app}\bin');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Path: String;
begin
  if CurStep <> ssPostInstall then Exit;
  RegQueryStringValue(HKCU, 'Environment', 'Path', Path);
  if Pos(';' + Lowercase(BinPath()) + ';', ';' + Lowercase(Path) + ';') = 0 then begin
    if (Path <> '') and (Copy(Path, Length(Path), 1) <> ';') then Path := Path + ';';
    if not RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Path + BinPath()) then
      RaiseException('Could not update the user PATH.');
    RegWriteDWordValue(HKCU, 'Software\NikPavlov\Memini', 'OwnsPath', 1);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, Wrapped: String;
  Index: Integer;
  OwnsPath: Cardinal;
begin
  if CurUninstallStep <> usUninstall then Exit;
  if not RegQueryDWordValue(HKCU, 'Software\NikPavlov\Memini', 'OwnsPath', OwnsPath) then Exit;
  if RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then begin
    Wrapped := ';' + Path + ';';
    Index := Pos(';' + Lowercase(BinPath()) + ';', Lowercase(Wrapped));
    if Index > 0 then begin
      Delete(Wrapped, Index, Length(BinPath()) + 1);
      Path := Copy(Wrapped, 2, Length(Wrapped) - 2);
      if Path = '' then RegDeleteValue(HKCU, 'Environment', 'Path')
      else RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Path);
    end;
  end;
  RegDeleteValue(HKCU, 'Software\NikPavlov\Memini', 'OwnsPath');
  RegDeleteKeyIfEmpty(HKCU, 'Software\NikPavlov\Memini');
end;

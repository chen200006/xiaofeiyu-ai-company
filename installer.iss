; 小肥鱼 AI 公司 · 安装脚本（Inno Setup 6）
; 编译：ISCC.exe installer.iss
; 特点：装到用户目录（不需要管理员）、开始菜单 + 桌面快捷方式、卸载时先备份数据

#define MyAppName "小肥鱼 AI 公司"
#define MyAppVersion "6.3.0"
#define MyAppExeName "小肥鱼AI公司.exe"

[Setup]
AppId={{8E2C4B1A-6F3D-4C0B-9D4A-1F5B7C9E2D31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=chen200006
AppPublisherURL=https://github.com/chen200006/xiaofeiyu-ai-company
DefaultDirName={localappdata}\Programs\XiaoFeiYuAI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist_installer
OutputBaseFilename=小肥鱼AI公司-安装包-v{#MyAppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ShowLanguageDialog=no

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "安装完成后立刻打开"; Flags: nowait postinstall skipifsilent

[Code]
{ 卸载前把数据备份到桌面，避免 company.db 与 images 被一起删掉 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, BackupDir, Stamp: String;
  R: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{app}');
    if FileExists(DataDir + '\company.db') then
    begin
      Stamp := GetDateTimeString('yyyymmdd-hhnnss', '-', ':');
      BackupDir := ExpandConstant('{userdesktop}\小肥鱼AI公司-数据备份-' + Stamp);
      ForceDirectories(BackupDir);
      FileCopy(DataDir + '\company.db', BackupDir + '\company.db', False);
      if DirExists(DataDir + '\images') then
        R := 0
      else
        R := 0;
      if not UninstallSilent then
        MsgBox('你的公司数据已备份到：' + #13#10 + BackupDir + #13#10 + #13#10 +
               '（company.db 是整个公司；images 文件夹是历史配图，需要的话手动复制。）',
               mbInformation, MB_OK);
    end;
  end;
end;

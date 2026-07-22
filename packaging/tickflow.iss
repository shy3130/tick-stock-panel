; ===========================================================================
; TickFlow Stock Panel — Inno Setup 安装包脚本
; ===========================================================================
; 用途: 把 PyInstaller 产出的 dist/TickFlowStockPanel/ 文件夹封装成
;       单个 Setup.exe 安装程序 (双击→安装向导→快捷方式→可卸载)。
;
; 构建 (本地):
;   1. 先跑 PyInstaller: cd backend && uv run pyinstaller ../packaging/tickflow.spec
;   2. 再跑 Inno Setup:   ISCC.exe packaging\tickflow.iss
;   3. 产物: packaging\Output\TickFlowStockPanel-Setup-x.x.x.exe
;
; 设计决策:
;   - 装到用户目录 {localappdata}\Programs\ (不弹 UAC, 不需管理员)
;   - 用户数据存在 {app}\data\ (与程序同处一个总目录, 视觉直观)
;   - 卸载时询问是否删除用户数据 ({app}\data\)
;   - 覆盖安装(升级)不动 data\: Inno Setup 只写程序文件, data 不在安装清单
;   - 桌面 + 开始菜单快捷方式
;   - 卸载入口 (控制面板可见)
; ===========================================================================

#define MyAppName          "TickFlow 股票面板"
#define MyAppNameEN       "TickFlow Stock Panel"
#define MyAppExeName      "TickFlowStockPanel.exe"
#define MyAppPublisher    "TickFlow"

; 版本号: 从根目录 VERSION 文件读取 (前端已移除, 不再从 frontend/package.json 取),
; 与 Release tag 保持一致; 手动指定更可靠 (CI 传入 /DMyAppVersion)
#ifndef MyAppVersion
  #define MyAppVersion     "0.0.0"
#endif

[Setup]
; 基本信息
AppName={#MyAppName}
AppVerName={#MyAppName} {#MyAppVersion}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 默认装到 D 盘 (非系统盘), 用户可在向导中改任意位置
; 若 D 盘不存在, [Code] 段 InitializeWizard 会自动回退到用户目录
DefaultDirName=D:\TickFlowStockPanel
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=TickFlowStockPanel-Setup-{#MyAppVersion}

; 关键: 不需要管理员权限, 永不弹 UAC
; 装到 D 盘普通目录 (非 Program Files) 不需要管理员权限
PrivilegesRequired=lowest
; 允许用户在向导中自由选择安装目录
DisableDirPage=no

; 压缩
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; 界面
WizardStyle=modern
DisableWelcomePage=no
DisableReadyPage=no
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; 卸载相关
Uninstallable=yes
CreateUninstallRegKey=yes

[Languages]
; 简中语言包内置在 packaging/ 下 (从 Inno Setup 官方仓库获取),
; 不依赖安装目录是否含该文件 (CI 友好)。
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 把 PyInstaller 产出的整个文件夹搬进安装目录
; Source 路径相对于 .iss 文件所在目录
Source: "..\backend\dist\TickFlowStockPanel\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

; 桌面 (可选, 由 Task 控制)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后启动应用
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前先关闭正在运行的应用 (否则 exe 被占用删不掉)
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

; [UninstallDelete] 故意不删 {app}:
; 用户数据在 {app}\data\, 若这里写 Type: filesandordirs; Name: "{app}" 会连数据一起删。
; 卸载默认行为已足够 —— Inno Setup 会删除它安装清单内的所有程序文件, 只留下运行时
; 生成的 data\ 目录。是否清理 data\ 由下方 [Code] 的卸载询问逻辑决定。

[Code]
// ── 辅助函数: 判断目录是否为空 ─────────────────────────────────
// Inno Setup 内置无 IsDirEmpty, 用 FindFirst/FindNext 自行实现。
// 用于卸载后清理空的 {app} 壳目录。
function IsDirEmpty(const Dir: String): Boolean;
var
  FindRec: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Dir) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := False;
          Break;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

// ── 启动时: 若 D 盘不存在, 回退默认路径到用户目录 ───────────────
// 避免默认 D:\... 但系统没 D 盘时向导显示无效路径
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard();
var
  DefaultDir: String;
begin
  // D 盘存在 → 用 D 盘; 否则回退用户目录 (无需管理员权限)
  if not DirExists('D:\') then
  begin
    DefaultDir := ExpandConstant('{localappdata}\Programs\TickFlowStockPanel');
    WizardForm.DirEdit.Text := DefaultDir;
  end;
end;

// ── 卸载时询问是否删除用户数据 ─────────────────────────────────
// 用户数据在 {app}\data\ (策略/选股/回测/监控/行情), 与程序同处 {app} 总目录。
// Inno Setup 卸载默认只删它装过的程序文件, data\ 会被保留 (覆盖安装/常规卸载都不丢)。
// 这里仅在用户明确「彻底卸载」时, 才询问是否清理 data\ + {app} 空壳。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, AppDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // {app}\data = 用户数据目录 (与程序同总目录, 子文件夹)
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) then
    begin
      if SuppressibleMsgBox(
          '是否同时删除用户数据?' + #13#10 + #13#10 +
          '位置: ' + DataDir + #13#10 +
          '内容: 行情数据、策略、选股结果、回测记录、监控规则等' + #13#10 + #13#10 +
          '选「是」彻底卸载, 选「否」保留数据(重装后可恢复)。',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
    // 清理可能残留的空 {app} 壳目录 (程序文件已被 Inno Setup 删除)
    AppDir := ExpandConstant('{app}');
    if DirExists(AppDir) and IsDirEmpty(AppDir) then
    begin
      DelTree(AppDir, True, True, True);
    end;
  end;
end;

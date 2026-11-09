#!/usr/bin/env pwsh
# android-ai-test-skills 一键环境安装（Windows）
#
# 用法:
#   pwsh -File scripts/setup.ps1
#   pwsh -File scripts/setup.ps1 -Python "C:\Python310\python.exe"   # 指定解释器
#   pwsh -File scripts/setup.ps1 -SkipDeviceCheck                    # 没插设备也装
#   pwsh -File scripts/setup.ps1 -Recreate                           # 重建已有 venv
#
# 产出：
#   <项目根>\.venv\Scripts\python.exe      （venv 就放在项目里，工具直接用它）
#   <项目根>\storage\                       （证据/会话库等运行产物）
#
# 版本要求：Python 3.10+（**没有上限**）
#   下限 3.10：pillow 12 起要求 >=3.10（3.9 装不上新版 pillow）
#
# ⚠️ 这里历史上写着「上限 3.12」，理由是 rapidocr-onnxruntime 的元数据
#    写死 requires_python="<3.13"。那个包已经改名成 `rapidocr`（3.x），
#    原生支持 3.13 —— 所以上限取消了。
#
#    D:\dsh\langgraph-poc 验证过的组合：
#        Python 3.13.3 + rapidocr 3.10.0 + onnxruntime 1.31.0 + pillow 13
#
#    教训：那种"卡在某个小版本"的限制，通常是某个依赖停更/改名的副作用。
#    换包或升级大版本之后要重新确认，别把旧结论一直抄下去。

[CmdletBinding()]
param(
    [string]$Python = $env:PYTHON,
    [switch]$Recreate,
    [switch]$SkipDeviceCheck,
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root    = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $Root '.venv'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'

$MinPy = [version]'3.10.0'
# 上限放宽到 4.0（等于没有）—— 依赖已换成支持 3.13 的 rapidocr 3.x。
# 保留这个变量是为了"太高/太低"的判断逻辑不用大改。
$MaxPy = [version]'4.0.0'

function Write-Step { param([string]$t) Write-Host "▶ $t" -ForegroundColor Cyan }
function Write-Ok   { param([string]$t) Write-Host "  [OK] $t" -ForegroundColor Green }
function Write-Warn { param([string]$t) Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Write-Fail { param([string]$t) Write-Host "  [X]  $t" -ForegroundColor Red }
function Write-Info { param([string]$t) Write-Host "       $t" -ForegroundColor DarkGray }

# 原生命令常把进度/DEBUG 写进 stderr，在 $ErrorActionPreference='Stop' 下会被
# PowerShell 当 NativeCommandError 直接终止脚本。这里合并 stderr，只按退出码判成败。
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @(), [int[]]$AllowExit = @(0))
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $Exe @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return @{ Output = $output; ExitCode = $code; Ok = ($AllowExit -contains $code) }
}

# ------------------------------------------------------------ Python 探测
# 记录被跳过的解释器及原因，失败时一并打印 —— 否则用户只看到"没找到"，
# 不知道自己机器上那个 python 到底为什么不合格。
$script:Skipped = New-Object System.Collections.ArrayList

function Test-PythonPath {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
    $out = $null
    try { $out = & $Path -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null } catch { return $null }
    if (-not $out) {
        [void]$script:Skipped.Add("$Path  （无法执行，可能是商店占位版）")
        return $null
    }
    $line = ([string]$out).Trim()
    if ($line -notmatch '^\d+\.\d+\.\d+$') {
        [void]$script:Skipped.Add("$Path  （版本号读不出，疑似商店占位版）")
        return $null
    }
    $v = [version]$line
    if ($v -lt $MinPy) {
        [void]$script:Skipped.Add("$Path  （$v 太低，需 >=$($MinPy.Major).$($MinPy.Minor)）")
        return $null
    }
    if ($v -ge $MaxPy) {
        [void]$script:Skipped.Add("$Path  （$v 太旧，最低要求 $($MinPy.Major).$($MinPy.Minor)）")
        return $null
    }
    return @{ Path = $Path; Version = $v }
}

function Resolve-Python {
    param([string]$Explicit)
    $paths = New-Object System.Collections.ArrayList
    if ($Explicit) { [void]$paths.Add($Explicit) }
    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($cmd) { [void]$paths.Add($cmd.Source) }
    }
    # 已知安装目录兜底（绕开 PATH 里的商店占位别名）
    foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'),
                        'C:\Program Files\python',
                        'C:\Python312', 'C:\Python311', 'C:\Python310')) {
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                ForEach-Object { [void]$paths.Add($_.FullName) }
        }
    }
    foreach ($p in $paths) {
        $hit = Test-PythonPath $p
        if ($hit) { return $hit }
    }
    return $null
}

# ------------------------------------------------------------------ 开场
Write-Host ''
Write-Host '============================================' -ForegroundColor DarkCyan
Write-Host '  Android AI Test Skills - 环境安装' -ForegroundColor Cyan
Write-Host "  项目目录: $Root" -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor DarkCyan
Write-Host ''

# ------------------------------------------------------------ 1. adb 检查
Write-Step '1/4 检查 adb...'
$adb = Get-Command adb -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $adb) {
    Write-Fail '未找到 adb，请安装 Android SDK platform-tools 并加入 PATH'
    Write-Info '下载: https://developer.android.com/tools/releases/platform-tools'
    exit 1
}
try { $adbVer = (& $adb.Source version 2>$null | Select-Object -First 1) } catch { $adbVer = $null }
Write-Ok ($(if ($adbVer) { $adbVer } else { $adb.Source }))

# ------------------------------------------------------------ 2. 设备检查
Write-Step '2/4 检查设备...'
if ($SkipDeviceCheck) {
    Write-Warn '已跳过（-SkipDeviceCheck），装完请自行确认 adb devices 有授权设备'
} else {
    $lines = @()
    try { $lines = @(& $adb.Source devices 2>$null | Where-Object { $_.Trim() -match '\bdevice\s*$' }) } catch { }
    if ($lines.Count -eq 0) {
        Write-Fail '未检测到已授权设备，请连接并开启 USB 调试'
        Write-Info '没插设备也要装依赖: pwsh -File scripts/setup.ps1 -SkipDeviceCheck'
        exit 1
    }
    & $adb.Source devices -l | Where-Object { $_.Trim() } | ForEach-Object { Write-Info $_ }
    Write-Ok "设备已连接 ($($lines.Count) 台)"
}

# -------------------------------------------------------- 3. venv 与依赖
Write-Step '3/4 创建虚拟环境并安装依赖...'

$py = Resolve-Python $Python
if (-not $py) {
    Write-Fail "未找到可用的 Python（本工具需要 $MinPy ~ $($MaxPy.Major).$($MaxPy.Minor - 1)）"
    Write-Host ''
    Write-Info '没有上限：OCR 库已换成 rapidocr 3.x（支持 3.13）'
    Write-Info '  "No matching distribution found"（最高只到 1.2.3，装不了 1.3.0+）'
    Write-Info '为什么有下限：pillow 12 起要求 >=3.10'
    Write-Host ''
    Write-Info '装一个 3.10 以上: https://www.python.org/downloads/（勾选 Add to PATH）'
    Write-Info '或显式指定: pwsh -File scripts/setup.ps1 -Python "C:\Python312\python.exe"'
    Write-Info '已知安装位置会 / 也可用 py -0p 列出本机所有解释器'
    if ($script:Skipped.Count) {
        Write-Host ''
        Write-Info '本机已找到但不合格的解释器：'
        # 同一路径可能被"显式指定 + PATH + 已知目录"重复扫到，去重
        $script:Skipped | Select-Object -Unique | ForEach-Object { Write-Info "  - $_" }
    }
    exit 1
}
Write-Info "使用 Python $($py.Version) - $($py.Path)"

if ((Test-Path -LiteralPath $VenvPy) -and $Recreate) {
    Write-Info '检测到已有 venv 且指定 -Recreate，删除重建...'
    try {
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Fail '删除 venv 失败 —— 多半是有进程正占用它'
        Write-Info '常见原因：WebUI 还在跑、或另一个终端正用着这个 venv 的 python'
        Write-Info '处理：先停掉 WebUI / 关掉占用的终端，再重跑本脚本'
        exit 1
    }
}

if (-not (Test-Path -LiteralPath $VenvPy)) {
    Write-Info '创建 venv（首次约需几十秒）...'
    $r = Invoke-Native $py.Path @('-m', 'venv', $VenvDir)
    if (-not $r.Ok) {
        Write-Fail "创建 venv 失败（退出码 $($r.ExitCode)）"
        $r.Output | Select-Object -Last 5 | ForEach-Object { Write-Info $_ }
        exit 1
    }
} else {
    Write-Info '复用已有 venv'
}
Write-Ok "venv 就绪 ($VenvPy)"

if ($SkipDeps) {
    Write-Warn '已跳过依赖安装（-SkipDeps）'
} else {
    Write-Info '升级 pip / setuptools / wheel ...'
    (Invoke-Native $VenvPy @('-m','pip','install','--upgrade','pip','setuptools','wheel')).Output |
        Select-Object -Last 3 | ForEach-Object { Write-Info $_ }

    Write-Info '安装 uiautomator2 / rapidocr / pillow 等（首次较慢，请耐心）...'
    $req = Join-Path $Root 'requirements.txt'
    $r = Invoke-Native $VenvPy @('-m','pip','install','-r',$req)
    $r.Output | Select-Object -Last 5 | ForEach-Object { Write-Info $_ }
    if (-not $r.Ok) {
        Write-Fail "依赖安装失败（退出码 $($r.ExitCode)），请检查网络或代理后重试"
        exit 1
    }
    Write-Ok '依赖安装完成'

    # import 自检：装上了但导不进来的隐性失败在这里暴露
    Write-Info '依赖 import 自检 ...'
    $r = Invoke-Native $VenvPy @('-c','import uiautomator2, rapidocr, PIL, yaml')
    if (-not $r.Ok) {
        Write-Fail '依赖 import 自检失败（包装上了但无法导入）'
        $r.Output | Select-Object -Last 5 | ForEach-Object { Write-Info $_ }
        exit 1
    }
    Write-Ok 'import 自检通过'

    # uiautomator2 设备端初始化（首次会在手机上装 atx-agent）
    if (-not $SkipDeviceCheck) {
        Write-Info '初始化 uiautomator2 设备端（首次会在手机上安装 apk，留意授权提示）...'
        $r = Invoke-Native $VenvPy @('-m','uiautomator2','init')
        if ($r.Ok) { Write-Ok 'u2 初始化完成' }
        else {
            Write-Warn "u2 init 退出码 $($r.ExitCode)（可稍后手动重跑）"
            $r.Output | Select-Object -Last 3 | ForEach-Object { Write-Info $_ }
        }
    }
}

# ------------------------------------------------------ 4. 运行目录自检
Write-Step '4/4 检查运行目录...'
foreach ($d in @('assets', 'assets\cases', 'assets\knowledge',
                 'assets\knowledge\paths',
                 'storage', 'storage\run_history', 'storage\tmp')) {
    $p = Join-Path $Root $d
    if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}
Write-Ok 'assets/ 与 storage/ 就绪'

# ★ 建出来了 ≠ 写得进去。
#   会话文件策略（read-only / workspace-write）拦的是**真写**：
#   目录 Test-Path 得到、权限位 rwx、属主是自己，open(O_WRONLY) 照样 EACCES。
#   2026-11-09 就是这一步"通过"之后，session.py start 只吐了一句
#   PermissionError: [Errno 13]，排查绕了一大圈才定位到沙箱。
foreach ($d in @('storage', 'assets\knowledge', 'assets\cases')) {
    $probe = Join-Path (Join-Path $Root $d) '.write_probe'
    try {
        Set-Content -LiteralPath $probe -Value 'probe' -ErrorAction Stop
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Fail "$d 写不进去：$($_.Exception.Message)"
        Write-Host "  工具层必须装在 harness 会话**可写工作区**之内（或放宽会话文件策略）。" -ForegroundColor Red
        Write-Host "  当前根目录：$Root" -ForegroundColor Red
        Write-Host "  否则装完也跑不了：开会话第一步就会撞 PermissionError: [Errno 13]。" -ForegroundColor Red
        exit 1
    }
}
Write-Ok 'storage / assets 实测可写（不是只看权限位）'

# ------------------------------------------------------------------ 自检
Write-Host ''
Write-Host '--------------------------------------------' -ForegroundColor DarkCyan
Write-Host ' 自检' -ForegroundColor Cyan
$ok = $true
$r = Invoke-Native $VenvPy @('-c','import sys;print(sys.version.split()[0])')
if ($r.Ok) { Write-Ok "venv Python $($r.Output[-1])" } else { Write-Fail 'venv Python 不可用'; $ok = $false }
foreach ($m in @('uiautomator2','rapidocr','yaml','PIL')) {
    $r = Invoke-Native $VenvPy @('-c',"import $m")
    if ($r.Ok) { Write-Ok $m } else { Write-Fail "$m 不可用"; $ok = $false }
}
foreach ($t in @('tools\session.py','tools\act.py','tools\doctor.py','webui.py')) {
    if (Test-Path -LiteralPath (Join-Path $Root $t)) { Write-Ok $t } else { Write-Fail "缺 $t"; $ok = $false }
}

# 统一自检：和 POSIX 那边跑的是同一个脚本（换机器 / 换会话策略之后重跑即可）
Write-Info 'tools/doctor.py 自检（可写性 / 产物同树 / 依赖 / 设备 / 输入法 / 用例库）...'
$r = Invoke-Native $VenvPy @((Join-Path $Root 'tools\doctor.py'))
$r.Output | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
if ($r.ExitCode -eq 2)      { Write-Fail 'doctor.py 有硬失败：别开会话，先照 ✗ 项的 → 修'; $ok = $false }
elseif ($r.ExitCode -eq 1)  { Write-Warn 'doctor.py 有警告项：能跑，但有些产物会拿不全（见 ! 项）' }
else                        { Write-Ok 'doctor.py 全部通过' }

# ------------------------------------------------------------------ 收尾
Write-Host ''
Write-Host '============================================' -ForegroundColor DarkCyan
if ($ok) {
    Write-Host ' 安装完成！' -ForegroundColor Green
    Write-Host ''
    Write-Host ' 常用命令（在项目目录下执行）:' -ForegroundColor DarkGray
    Write-Host '   .\.venv\Scripts\python tools\session.py start --title "..." --input @case.txt' -ForegroundColor Gray
    Write-Host '   .\.venv\Scripts\python tools\observe.py --serial <设备号>' -ForegroundColor Gray
    # 测试台只用标准库，不需要 venv 里的依赖 —— 任意 python 都能跑
    Write-Host '   python webui.py                            # 测试台 http://127.0.0.1:9810' -ForegroundColor Gray
    Write-Host ''
    Write-Host ' 回归测试:' -ForegroundColor DarkGray
    Write-Host '   .\.venv\Scripts\python tests\test_crash_pipeline.py' -ForegroundColor Gray
} else {
    Write-Host ' 安装未全部通过，请按上面的 [X] 项排查' -ForegroundColor Red
}
Write-Host '============================================' -ForegroundColor DarkCyan
exit $(if ($ok) { 0 } else { 1 })

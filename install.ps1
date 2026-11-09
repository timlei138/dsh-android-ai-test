<#
第一次安装（Windows）：在工作区里建项目目录 + 装 Python 依赖，一条命令完事。
和 install.sh 同构，理由也一样：文档第 1、2 步原来写的是「拷 Share 目录」+
「自己开终端跑 pip」，全靠人临场发挥，装出来的东西各不相同；装在沙箱写不进去
的地方，后面 AI 每开一次会话都被 require_writable_storage 拦下（rc=75）。

用法：
  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -To D:\dsh\w\dsh-ai-auto-test
  powershell -ExecutionPolicy Bypass -File install.ps1 -From D:\dsh\android-ai-test-share
  powershell -ExecutionPolicy Bypass -File install.ps1 -Wire
  powershell -ExecutionPolicy Bypass -File install.ps1 -SetupArgs @('--skip-device-check')

★ 落点必须在**会话工作区之内**（DSH 文件策略 workspace-write 只放工作区）。
#>
param(
    [string]$To = "",
    [string]$From = "",
    [switch]$Wire,
    [string[]]$SetupArgs = @()
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Step($m) { Write-Host ""; Write-Host "▶ $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Bad($m)   { Write-Host "  ✗ $m" -ForegroundColor Red; exit 1 }

if (-not $From) { $From = $PSScriptRoot }
if (-not (Test-Path (Join-Path $From 'toolkit\tools\session.py'))) { Bad "源里没有 toolkit\tools\session.py：$From（-From 指到仓库或 Share 包根）" }
if (-not (Test-Path (Join-Path $From 'toolkit\scripts\setup.ps1'))) { Bad "源里没有 toolkit\scripts\setup.ps1：$From" }
$From = (Resolve-Path $From).Path

if (-not $To) { $To = Join-Path (Get-Location).Path 'dsh-ai-auto-test' }
if (Test-Path (Join-Path $To 'toolkit\tools\session.py')) { Bad "落点已经装过了：$(Join-Path $To 'toolkit')（要重装请先自己挪走旧目录，我不覆盖你的产物）" }
if ((Split-Path -LiteralPath $To -Qualifier) -ne (Split-Path -LiteralPath $From -Qualifier)) { Warn "落点和源不在同一个盘：$To" }
New-Item -ItemType Directory -Force -Path $To | Out-Null
$To = (Resolve-Path $To).Path
if ($To -eq $From) { Bad "落点和源是同一个目录。就地安装请直接跑 $From\toolkit\scripts\setup.ps1" }

Write-Host "============================================"
Write-Host "  Android 真机测试环境 · 第一次安装 (Windows)"
Write-Host "  源    $From"
Write-Host "  落点  $To"
Write-Host "============================================"

Step "0/4 落点能不能写（这一条决定你后面会不会撞 [Errno 13]）"
New-Item -ItemType Directory -Force -Path (Join-Path $To 'toolkit') | Out-Null
$probe = Join-Path $To 'toolkit\.w_probe'
try { Set-Content -Path $probe -Value x -ErrorAction Stop; Remove-Item $probe -Force }
catch { Bad "落点写不进去：$To —— 请 -To 指到你会话工作区之内的目录（$_）" }
Good "落点可写：$To"
$ws = if ($env:DSH_WORKSPACE) { $env:DSH_WORKSPACE } else { $null }
if ($ws) {
    if ($To -like (Join-Path $ws '*')) { Good "在工作区之内（DSH_WORKSPACE=$ws）" }
    else { Warn "-To 落在当前会话工作区之外：$To。文件策略是 workspace-write 时 AI 侧写不进产物；要么挪进工作区，要么本会话用 danger-full-access" }
}

Step "1/4 铺代码（不带 .venv / storage / __pycache__ —— 前者换路径即废，后者是你的历史）"
function Copy-Tree($src, $dst) {
    if (-not (Test-Path $src)) { return }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Get-ChildItem -LiteralPath $src -Force | Where-Object {
        $_.Name -notin @('.venv','storage','.git','node_modules','__pycache__') -and $_.Extension -ne '.pyc'
    } | ForEach-Object {
        $t = Join-Path $dst $_.Name
        if ($_.PSIsContainer) { Copy-Tree $_.FullName $t } else { Copy-Item $_.FullName $t -Force }
    }
}
Copy-Tree (Join-Path $From 'toolkit') (Join-Path $To 'toolkit')
foreach ($d in @('plugins','skills','scripts')) { Copy-Tree (Join-Path $From $d) (Join-Path $To $d) }
foreach ($f in @('安装说明.md','README.md')) { $p = Join-Path $From $f; if (Test-Path $p) { Copy-Item $p (Join-Path $To $f) -Force } }
foreach ($s in @('storage','storage\run_history','storage\tmp')) { New-Item -ItemType Directory -Force -Path (Join-Path $To "toolkit\$s") | Out-Null }
if (-not (Test-Path (Join-Path $To 'toolkit\assets\cases'))) { Warn "源里没有 toolkit\assets\cases（用例库）—— 新机器上是空的，跑用例前先把 assets\cases\<包名>\ 放进来" }
$sz = [math]::Round(((Get-ChildItem (Join-Path $To 'toolkit') -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Good "代码就位 ${sz}MB → $(Join-Path $To 'toolkit')"

Step "2/4 + 3/4 Python 环境、依赖、自检（交给 scripts\setup.ps1，它最后一步必跑 doctor.py）"
$setup = Join-Path $To 'toolkit\scripts\setup.ps1'
if ($SetupArgs.Count -gt 0) { & powershell -ExecutionPolicy Bypass -File $setup @SetupArgs } else { & powershell -ExecutionPolicy Bypass -File $setup }
$rc = $LASTEXITCODE
if ($rc -ne 0) { Bad "setup.ps1 失败（rc=$rc，上面是它的原文输出，别绕过去手改）" }

if ($Wire) {
    Step "4/4 挂到 DSH profile（-Wire 只给清单，不自动改你的 profile）"
    $prof = if ($env:DSH_PROFILE) { $env:DSH_PROFILE } else { Join-Path $env:USERPROFILE '.dsh\profiles\web' }
    $pj = Join-Path $prof 'package.json'
    if (Test-Path $pj) {
        Copy-Item $pj "$pj.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        Warn "已备份 $pj → *.bak-*"
        if ((Get-Content $pj -Raw) -match 'android-tools-plugin') { Warn "package.json 里已有 android-tools 链接；现在指哪自己确认：Select-String android $pj" }
        else { Warn "package.json 里还没有；dependencies 加：`"@local/dsh-android-tools`": `"link:$($To -replace '\\','/')\plugins\android-tools-plugin`" 且 bundles 数组加同名项" }
    } else { Warn "找不到 profile 的 package.json：$pj（DSH_PROFILE 指一下）" }
    Warn "插件每次调用都重读 config.target，但改完要重启 harness 才生效。要改的行："
    foreach ($f in @((Join-Path $To 'plugins\android-tools-plugin\cordis.patch.yml'), (Join-Path $To 'plugins\android-auto-test-preset\preset\cordis.patch.yml'))) {
        if (Test-Path $f) { Select-String -Path $f -Pattern 'target:' | ForEach-Object { Warn "    $($_.Path):$($_.LineNumber): $($_.Line.Trim())" } }
    }
    Warn "别忘了：cd $(Join-Path $To 'scripts'); pwsh -File link-dsh-deps.ps1（漏了插件根本起不来）"
} else {
    Step "4/4 未挂接 DSH（没给 -Wire）—— 插件/skills 仍指你原来那份工具层"
}

Write-Host ""
Good "装完了。落点 $To"
Write-Host "   开会话：cd $(Join-Path $To 'toolkit'); .venv\Scripts\python.exe tools\session.py start --package <包名> ..."
Write-Host "   产物：$(Join-Path $To 'toolkit\storage')（在工作区内，沙箱写得住；用例历史与证据都在这里）"
Write-Host "   换掉正在用的那套插件 target：install.ps1 -Wire 只看清单，或照 4/4 手工改 3 行 + 重启"

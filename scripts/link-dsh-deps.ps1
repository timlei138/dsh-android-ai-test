#!/usr/bin/env pwsh
# android-tools-plugin 的 DSH 依赖补齐（Windows）
#
# ## 这个脚本解决什么问题
#
# 插件 lib/index.js 前两行是裸导入：
#
#     import z from '@deepseek-ai/schemastery'
#     import { defineTool } from '@deepseek-ai/dsh-tools'
#
# 这两个包由 DSH 自己安装，不在插件包里。安装说明要求用
# `dsh plugin --profile <名字> add "link:<Share>\plugins\android-tools-plugin"`
# 挂进来（link 是为了改了代码能立即生效）。
#
# 但 link 之后，插件的真实路径仍是 `<Share>\plugins\android-tools-plugin\lib\index.js`。
# Node 解析裸导入时沿 **真实路径** 逐级向上找 node_modules，
# 只会找 `<Share>\plugins\...` 这些目录 —— 那里没有 @deepseek-ai，于是：
#
#     failed to import Error {ERR_MODULE_NOT_FOUND}:
#     Cannot find package '@deepseek-ai/schemastery'
#
# 表现就是 DSH 启动时报 `1 entry did not activate`，8 个 android_* 工具全都不出现。
#
# 本脚本在插件目录里补两个 junction 指向 DSH 自己的那两个包，让裸导入能解析。
#
# ## 用法
#
#     pwsh -File scripts/link-dsh-deps.ps1
#     pwsh -File scripts/link-dsh-deps.ps1 -Profile web        # 指定 profile
#     pwsh -File scripts/link-dsh-deps.ps1 -CheckOnly          # 只检查不修改
#
# 幂等：已经指向正确的会跳过。DSH 升级导致指向失效时重跑一次即可。
#
# ## 为什么不用改代码去掉依赖
#
# 也可以手写 defineTool/Config 把两行 import 干掉（零依赖）。但那样就得跟着
# DSH 的 schema 编译规则走，版本一变就可能对不上。补 junction 让插件继续用
# DSH 官方那套实现，更稳。

[CmdletBinding()]
param(
    [string]$Profile = $(if ($env:DSH_PROFILE) { $env:DSH_PROFILE } else { 'web' }),
    [string]$ShareRoot = '',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Pkgs    = @('schemastery', 'dsh-tools')

# 没显式给 -ShareRoot 时，从脚本所在位置往上一级推（脚本在 <Share>\scripts\ 下）。
# 用 [scriptblock]::Create() 跑时 $PSScriptRoot 是空的，所以这里必须留兜底。
if (-not $ShareRoot) {
    if ($PSScriptRoot) {
        $ShareRoot = Split-Path -Parent $PSScriptRoot
    } else {
        $ShareRoot = (Get-Location).Path
    }
}

$PluginDir = Join-Path $ShareRoot 'plugins\android-tools-plugin'
$DepRoot   = Join-Path $PluginDir 'node_modules\@deepseek-ai'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "    跳过 $msg" -ForegroundColor DarkGray }
function Write-Warn2($msg) { Write-Host "    注意 $msg" -ForegroundColor Yellow }

if (-not (Test-Path $PluginDir)) {
    Write-Host "找不到插件目录：$PluginDir" -ForegroundColor Red
    Write-Host "本脚本应放在 <Share>\scripts\ 下，或用 -ShareRoot 指定 Share 根目录。" -ForegroundColor Red
    exit 1
}

# ── 1. 定位 profile 目录 ─────────────────────────────────────────────
Write-Step "定位 DSH profile 目录"
$ProfileDir = $null
if ($env:DSH_PROFILE_DIR -and (Test-Path $env:DSH_PROFILE_DIR)) {
    $ProfileDir = $env:DSH_PROFILE_DIR
    Write-Ok "取自 `$env:DSH_PROFILE_DIR：$ProfileDir"
} else {
    $guess = Join-Path $env:USERPROFILE ".dsh\profiles\$Profile"
    if (Test-Path $guess) {
        $ProfileDir = $guess
        Write-Ok "按配置名推断：$ProfileDir"
    }
}
if (-not $ProfileDir) {
    Write-Host "找不到 profile 目录（试过 `$env:DSH_PROFILE_DIR 和 %USERPROFILE%\.dsh\profiles\$Profile）" -ForegroundColor Red
    Write-Host "先用 dsh --help 确认 profile 名，再用 -Profile 指定，例如 -Profile web" -ForegroundColor Red
    exit 1
}

# ── 2. 用 node 问出两个包的真实位置（最可靠的探测法）────────────────
# 在 profile 目录里跑 import.meta.resolve：DSH 加载插件时解析的就是这条路径链，
# 所以这里问到的位置一定和 DSH 自己用的是同一个。
Write-Step "向 node 询问 @deepseek-ai/* 的真实位置"
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue)
if (-not $nodeExe) {
    Write-Host "找不到 node。先确认 Node.js 已安装且在 PATH 上（dsh 能跑说明它一定有）。" -ForegroundColor Red
    exit 1
}

$resolved = @{}
foreach ($p in $Pkgs) {
    $js = "try{console.log(import.meta.resolve('@deepseek-ai/$p'))}catch(e){console.log('ERR|'+e.code)}"
    Push-Location $ProfileDir
    try {
        $raw = (& node --input-type=module -e $js 2>&1 | ForEach-Object { "$_" }) -join "`n"
    } finally {
        Pop-Location
    }
    $line = ($raw -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()

    if ($line -match '^ERR\|') {
        Write-Host "    解析 @deepseek-ai/$p 失败：$line" -ForegroundColor Red
        Write-Host "    → 这个 profile 里没有该包，说明 DSH 本身没装好。先确认 dsh 能正常启动。" -ForegroundColor Red
        exit 1
    }

    # file:///C:/.../<pkg>/lib/index.mjs  →  去掉 file:// 与末尾两段
    $path = $line -replace '^file:///', ''
    $path = [System.Uri]::UnescapeDataString($path)
    $path = $path -replace '/', '\'
    $pkgDir = Split-Path -Parent (Split-Path -Parent $path)
    if (-not (Test-Path (Join-Path $pkgDir 'package.json'))) {
        Write-Host "    从 $path 反推包目录失败（$pkgDir 下没有 package.json）" -ForegroundColor Red
        exit 1
    }
    $resolved[$p] = $pkgDir
    Write-Ok "@deepseek-ai/$p → $pkgDir"
}

# ── 3. 在插件目录建 junction ────────────────────────────────────────
Write-Step "在插件目录里补 node_modules 链接"
if (-not (Test-Path $DepRoot)) {
    if ($CheckOnly) {
        Write-Warn2 "缺少 $DepRoot（-CheckOnly 不修改）"
    } else {
        New-Item -ItemType Directory -Force -Path $DepRoot | Out-Null
        Write-Ok "建目录 $DepRoot"
    }
}

foreach ($p in $Pkgs) {
    $link = Join-Path $DepRoot $p
    $want = $resolved[$p]

    if (Test-Path $link) {
        $item = Get-Item $link -Force
        $isLink = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isLink) {
            $have = ($item.Target | Select-Object -First 1)
            if ($have -eq $want) {
                Write-Skip "$p 已指向正确位置"
                continue
            }
            Write-Warn2 "$p 指向旧位置，重建：$have → $want"
            if ($CheckOnly) { continue }
            Remove-Item $link -Recurse -Force
        } else {
            Write-Skip "$p 是真实目录（不是链接），不动它"
            continue
        }
    }

    if ($CheckOnly) {
        Write-Warn2 "$p 缺少链接 → 应指向 $want"
        continue
    }
    New-Item -ItemType Junction -Path $link -Target $want | Out-Null
    Write-Ok "建立 $link → $want"
}

# ── 4. 自检：真的能 import 吗 ───────────────────────────────────────
Write-Step "自检"
if ($CheckOnly) {
    Write-Host "    （-CheckOnly，跳过自检）" -ForegroundColor DarkGray
    exit 0
}

Push-Location $PluginDir
try {
    $js = "import('./lib/index.js').then(m=>console.log('OK|'+Object.keys(m).join(','))).catch(e=>console.log('FAIL|'+(e.code||'')+'|'+e.message.split('\n')[0]))"
    $raw = (& node --input-type=module -e $js 2>&1 | ForEach-Object { "$_" }) -join "`n"
} finally {
    Pop-Location
}
$line = ($raw -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()

if ($line -like 'OK|*') {
    Write-Ok "插件可以正常导入（导出：$($line.Substring(3))）"
    Write-Host ""
    Write-Host "搞定。重启 DSH（或热重载 android-tools）后，8 个 android_* 工具就应该出现了。" -ForegroundColor Green
    Write-Host "验证：dsh --profile $Profile --dump-config 不应再有 'did not activate' 警告。" -ForegroundColor Green
} else {
    Write-Host "    插件仍无法导入：$line" -ForegroundColor Red
    Write-Host "    → 把上面这行发给维护者。常见原因：DSH 升级后包结构变了，或 node 版本过低。" -ForegroundColor Red
    exit 1
}

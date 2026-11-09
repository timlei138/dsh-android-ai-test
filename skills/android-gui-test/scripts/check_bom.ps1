#!/usr/bin/env pwsh
# UTF-8 BOM 校验 / 修复（Windows PowerShell 5.1 兼容性护栏）
#
# 背景（为什么需要这个脚本）：
#   PowerShell 5.1 读 .ps1 时，没有 BOM 就按系统 ANSI 代码页解析（中文 Windows
#   = GBK）。本 skill 的脚本里有大量中文注释/输出和 emoji，按 GBK 解析 UTF-8
#   字节流会得到乱码；一旦乱码破坏了引号或括号配对，整个脚本直接语法崩溃。
#   实测：run_case.ps1 去掉 BOM 后有 7 个语法错误、完全无法执行。
#   BOM 是让 PS 5.1 认出「这是 UTF-8」的唯一信号，不是可省略的装饰。
#
# 为什么会缺 BOM：
#   多数编辑/生成工具重写文件时不会保留原有的 3 字节 BOM（本项目已踩两次：
#   改 webui.ps1、run_case.ps1 后 BOM 丢失）。git/copy 不会弄丢，重写才会。
#
# 用法:
#   pwsh -File scripts/check_bom.ps1              # 只校验，有问题以退出码 1 结束
#   pwsh -File scripts/check_bom.ps1 -Fix         # 校验并自动补回缺失的 BOM
#   pwsh -File scripts/check_bom.ps1 -Fix -Quiet  # 修复且不打印 ok 明细
#
# 约定：修改 scripts/ 下任何 .ps1 之后都应跑一次本脚本（不带 -Fix 先看，
#   确认无误再 -Fix）。退出码 0 = 全部带 BOM；1 = 存在缺失（已 -Fix 则已补）。

[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$SkillDir = Split-Path -Parent $PSScriptRoot
$BOM = @(0xEF, 0xBB, 0xBF)

# 扫描范围：scripts/ 全部 + skill 包根目录下的 .ps1（目前都在 scripts/）
$targets = @()
Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File -ErrorAction SilentlyContinue |
    ForEach-Object { $targets += $_ }
Get-ChildItem -LiteralPath $SkillDir -Filter '*.ps1' -File -ErrorAction SilentlyContinue |
    ForEach-Object { $targets += $_ }
$targets = $targets | Sort-Object FullName -Unique

if (-not $targets) {
    Write-Host "未找到任何 .ps1 文件（$SkillDir）" -ForegroundColor Yellow
    exit 0
}

$missing = @()
$ok = 0
foreach ($f in $targets) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq $BOM[0] -and $bytes[1] -eq $BOM[1] -and $bytes[2] -eq $BOM[2])
    if ($hasBom) {
        $ok++
        if (-not $Quiet) { Write-Host ("  OK   {0}" -f $f.Name) -ForegroundColor DarkGray }
        continue
    }
    $missing += $f
    if ($Fix) {
        # 以 UTF-8 读回原文，再用带 BOM 的 UTF8Encoding 写回（避免二次编码错误）
        $text = [System.IO.File]::ReadAllText($f.FullName, [System.Text.Encoding]::UTF8)
        $enc = New-Object System.Text.UTF8Encoding($true)
        [System.IO.File]::WriteAllText($f.FullName, $text, $enc)
        Write-Host ("  FIX  {0}  已补回 UTF-8 BOM" -f $f.Name) -ForegroundColor Green
    } else {
        Write-Host ("  MISS {0}  缺少 UTF-8 BOM（PS 5.1 会按 GBK 解析，可能直接语法崩溃）" -f $f.Name) -ForegroundColor Red
    }
}

Write-Host ""
if (-not $missing) {
    Write-Host ("校验通过：{0} 个 .ps1 均带 UTF-8 BOM" -f $ok) -ForegroundColor Green
    exit 0
}

if ($Fix) {
    Write-Host ("已修复 {0} 个文件的 BOM（建议重跑一次本脚本确认）" -f $missing.Count) -ForegroundColor Green
    exit 0
}

Write-Host ("发现 {0} 个文件缺 BOM。修复命令：" -f $missing.Count) -ForegroundColor Yellow
Write-Host "  pwsh -File `"$PSCommandPath`" -Fix" -ForegroundColor Yellow
exit 1

<#
.SYNOPSIS
    转发到 install.py —— 环境判定与安装步骤只留一份实现（原因见 install.py 顶部注释）。

.DESCRIPTION
    这个文件原来自己写了 108 行 PowerShell，而它从没在任何机器上被执行过
    （手边这台没有 pwsh）。"工作区在哪、这个目录写得进吗"判两遍必然对不齐，
    所以改成转发。PowerShell 习惯写 -To / -Wire，install.py 是 --to / --wire，
    所以这里做一次参数名翻译，剩下的原样透传（包括 -- 后面的 setup 参数）。

    ⚠️ 本文件在这台 Linux 机器上只能做括号平衡检查，Windows 那边第一次跑请当beta看。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -To D:\dsh\w\dsh-ai-auto-test
    powershell -ExecutionPolicy Bypass -File install.ps1 -Wire -Check
#>
param(
    [string]$To,
    [string]$From,
    [switch]$Wire,
    [switch]$Check,
    [string]$ReuseVenv,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)
$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$Py = $null; $PyArgs = @()
foreach ($c in @('py', 'python', 'python3')) {
    $hit = Get-Command $c -ErrorAction SilentlyContinue
    if ($hit) { $Py = $hit.Source; if ($c -eq 'py') { $PyArgs = @('-3') } ; break }
}
if (-not $Py) {
    Write-Host '  x 找不到 python —— 它本来就是前置条件（建 venv 要用）。装 Python 3.10+ 再跑。' -ForegroundColor Red
    Write-Host "    已经装过层、只是补依赖：powershell -File $Dir\toolkit\scripts\setup.ps1"
    exit 1
}
if (-not (Test-Path (Join-Path $Dir 'install.py'))) {
    Write-Host "  x 缺 $Dir\install.py（包不完整）" -ForegroundColor Red
    exit 1
}

$a = @()
if ($To)        { $a += @('--to', $To) }
if ($From)      { $a += @('--from', $From) }
if ($ReuseVenv) { $a += @('--reuse-venv', $ReuseVenv) }
if ($Wire)      { $a += '--wire' }
if ($Check)     { $a += '--check' }
if ($Rest)      { $a += $Rest }

& $Py @PyArgs (Join-Path $Dir 'install.py') @a
exit $LASTEXITCODE

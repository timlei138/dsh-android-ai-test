#!/usr/bin/env bash
# android-ai-test-skills 一键环境安装（Linux / macOS）
#
# 用法:
#   bash scripts/setup.sh
#   bash scripts/setup.sh --python python3.12      # 指定解释器
#   bash scripts/setup.sh --recreate               # 重建已有 venv
#   bash scripts/setup.sh --skip-device-check      # 没插设备也装
#   bash scripts/setup.sh --mirror <pip index>     # 换镜像源
#
# 产出：
#   <项目根>/.venv/bin/python        （和 setup.ps1 同构，只是 POSIX 路径）
#   <项目根>/storage/                （运行产物：test.db / run_history / tmp）
#   <项目根>/assets/{cases,knowledge/paths}
#
# 为什么另写一份而不是"用 pwsh"：
#   2026-11-09 在 Ubuntu 上第一次部署时，仓库里只有 setup.ps1，
#   全程手敲（见 Share/Linux安装记录.md）。手敲的代价不是慢，是**每个人敲出来的
#   东西不一样**：venv 建在哪、镜像源带不带、目录有没有建全，全靠临场记忆。
#
# ★ 最后一步一定跑 tools/doctor.py：装完不自证，就等着跑用例时撞 [Errno 13]。
set -u

Root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VenvDir="$Root/.venv"
VenvPy="$VenvDir/bin/python"
Mirror="${PIP_MIRROR:-https://mirrors.ctyun.cn/pypi/simple}"
WantPython=""
Recreate=0
SkipDevice=0

while [ $# -gt 0 ]; do
    case "$1" in
        --python) WantPython="${2:-}"; shift 2 ;;
        --python=*) WantPython="${1#*=}"; shift ;;
        --recreate) Recreate=1; shift ;;
        --skip-device-check) SkipDevice=1; shift ;;
        --mirror) Mirror="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  · %s\n' "$*"; }
die()  { printf '\n  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

echo "============================================"
echo "  Android AI Test Skills - 环境安装 (POSIX)"
echo "  项目目录: $Root"
echo "============================================"

# ------------------------------------------------------- 1/5 adb
step "1/5 检查 adb..."
command -v adb >/dev/null 2>&1 || die "PATH 里没有 adb：装 platform-tools（apt install adb 或下载 SDK platform-tools）后重试"
ok "$(adb version 2>/dev/null | head -1)"

# ------------------------------------------------------- 2/5 设备
step "2/5 检查设备..."
if [ "$SkipDevice" = 1 ]; then
    info "已跳过（--skip-device-check）"
else
    State="$(adb get-state 2>/dev/null || true)"
    if [ "$State" = "device" ]; then
        ok "设备在线"
        Ime="$(adb shell settings get secure default_input_method 2>/dev/null | tr -d '\r' || true)"
        case "$Ime" in
            *uiautomator*) ok "输入法 $Ime（可写字）" ;;
            *) printf '  \033[33m!\033[0m 输入法是 %s —— **打字会失败**。\n' "${Ime:-空}"
               printf '      切回：adb shell ime enable com.github.uiautomator/.AdbKeyboard ; adb shell ime set com.github.uiautomator/.AdbKeyboard\n'
               printf '      （不装 AdbKeyboard 也行，但 skill 里的 input 动作会报 ADB_KEYBOARD_CLEAR_TEXT failed）\n' ;;
        esac
    else
        printf '  \033[33m!\033[0m 没有可用设备（adb get-state = %s）：先连真机、开 USB 调试、点"允许"\n' "${State:-未知}"
        printf '      装可以继续装，跑用例之前必须插上。\n'
    fi
fi

# ------------------------------------------------------- 3/5 Python
step "3/5 找合格的 Python（3.10+，无上限）..."
Cands=""
if [ -n "$WantPython" ]; then Cands="$WantPython"; else
    for c in python3 python; do command -v "$c" >/dev/null 2>&1 && Cands="$Cands $c"; done
fi
PyBin=""
for c in $Cands; do
    Ver="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
    case "$Ver" in
        3.1[0-9]|3.[2-9][0-9]) PyBin="$c"; ok "用 $c（Python $Ver）"; break ;;
        *) info "跳过 $c：Python $Ver < 3.10" ;;
    esac
done
[ -n "$PyBin" ] || die "没找到 Python 3.10+。装了的话用 --python 指过来：bash scripts/setup.sh --python /usr/bin/python3.12"

# ------------------------------------------------------- 4/5 venv + 依赖
step "4/5 建 venv 并装依赖..."
if [ -d "$VenvDir" ] && [ ! -x "$VenvPy" ]; then
    die "$VenvDir 存在但没有 bin/python（可能是 Windows 拷过来的 venv —— **venv 不能跨系统/跨路径复用**）。加 --recreate 重建"
fi
if [ -d "$VenvDir" ]; then
    if [ "$Recreate" = 1 ]; then rm -rf "$VenvDir"; info "已删除旧 venv（--recreate）"
    else ok "venv 已存在，跳过创建：$VenvDir"; fi
fi
if [ ! -x "$VenvPy" ]; then
    "$PyBin" -m venv "$VenvDir" || die "venv 创建失败（Debian/Ubuntu 要 apt install python3-venv）"
    ok "venv: $VenvPy"
fi
[ -f "$Root/requirements.txt" ] || die "缺 $Root/requirements.txt"
# 国内直连 pypi.org 经常超时（200MB 的 onnxruntime 尤其），镜像源是默认动作不是可选项
"$VenvPy" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
printf '  · pip install -r requirements.txt -i %s\n' "$Mirror"
"$VenvPy" -m pip install -r "$Root/requirements.txt" -i "$Mirror" || die "依赖安装失败：换源再试 --mirror https://pypi.tuna.tsinghua.edu.cn/simple"
ok "依赖装完"

# ------------------------------------------------------- 5/5 目录 + 自检
step "5/5 建产物目录并自证可写..."
for d in assets/cases assets/knowledge/paths storage storage/run_history storage/tmp; do
    mkdir -p "$Root/$d" || die "建不出 $Root/$d —— 这一层就写不进去：
      ① 你多半在 harness 会话的沙箱里（策略 read-only / workspace-write 且工作区没覆盖这里）
      ② 把工具层装进会话工作区内，或放宽会话文件策略
      ③ 或显式指可写位置：export ANDROID_TEST_STORAGE=\"<可写目录>/storage\""
done
ok "assets/ 与 storage/ 就绪"

# 只 stat 到目录不算数：沙箱拦的是真写，权限位好看也白搭
for d in storage assets/knowledge assets/cases storage/run_history storage/tmp; do
    P="$Root/$d/.write_probe"
    if ! printf probe > "$P" 2>/dev/null; then
        die "$d 写不进去（沙箱在拦，不是权限位问题）：$(dirname "$d") 不在可写范围
      → 把工具层装进会话工作区，或放宽会话文件策略，或
        export ANDROID_TEST_STORAGE=\"<可写目录>/storage\""
    fi
    rm -f "$P"
done
ok "storage / assets 实测可写"

printf '\n'
"$VenvPy" "$Root/tools/doctor.py"
Dc=$?
case "$Dc" in
    0) printf '\n\033[32m✓ 装完了，可以开第一条用例。\033[0m\n' ;;
    1) printf '\n\033[33m! 装完了，但上面有 ! 项：能跑，产物可能不全（见每一条 →）。\033[0m\n' ;;
    *) printf '\n\033[31m✗ 装完了但自检没过：照上面 → 修完重跑 doctor，别急着开会话。\033[0m\n' ;;
esac
printf '\n下一步（三处 target 要指到本目录：%s）\n' "$Root"
printf '  plugins/android-tools-plugin/cordis.patch.yml\n'
printf '  plugins/android-auto-test-preset/preset/cordis.patch.yml   （tester / executor 两段）\n'
printf '  插件若报 Cannot find package @deepseek-ai/schemastery：cd plugins/android-tools-plugin && node link-dsh-deps.mjs\n'
exit "$Dc"

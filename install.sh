#!/usr/bin/env bash
# 第一次安装：在工作区里建项目目录 + 装 Python 依赖，一条命令完事。
#
# 为什么要有这个脚本（2026-11-09 的实测教训）：
#   安装说明第 1、2 步原本写的是「把 Share 目录拷过去」+「自己开终端跑 pip」——
#   全靠人临场发挥。结果每个人装出来的东西不一样：venv 建在哪、镜像源带不带、
#   目录建没建全、装完自不自检，全临场。装到哪个目录本该是安装唯一要做的决定，
#   不该是个坑：装错地方的下场是 AI 每开一次会话都被 require_writable_storage
#   拦下（rc=75），或者跑到第一条用例才撞出一句 PermissionError: [Errno 13]，
#   看不出跟权限有任何关系。
#
# 用法：
#   bash install.sh                      # 装到 $(pwd)/dsh-ai-auto-test
#   bash install.sh --to ~/work/at       # 指定落点（必须在工作区内，见下）
#   bash install.sh --from <源目录>       # 代码来源；缺省 = 本脚本所在目录（仓库）
#   bash install.sh --reuse-venv <venv>  # 从已有 POSIX venv 复制依赖，不连 pip 源
#   bash install.sh --wire               # 打印挂接 DSH profile 的清单（含备份 package.json）
#   bash install.sh -- --skip-device-check   # -- 之后的参数原样转给 scripts/setup.sh
#
# ★ 落点必须在**会话工作区之内**。装在沙箱写不进去的地方（另一个盘、别人的
#   home、/opt…），后面每一步都会 rc=75 —— 那不是这个脚本的 bug，是它在替你
#   挡住一次白跑半天。
set -u

To="$(pwd)/dsh-ai-auto-test"
From=""
ReuseVenv=""
Wire=0
SetupArgs=()

while [ $# -gt 0 ]; do
    case "$1" in
        --to) To="${2:-}"; shift 2 ;;
        --to=*) To="${1#*=}"; shift ;;
        --from) From="${2:-}"; shift 2 ;;
        --from=*) From="${1#*=}"; shift ;;
        --reuse-venv) ReuseVenv="${2:-}"; shift 2 ;;
        --reuse-venv=*) ReuseVenv="${1#*=}"; shift ;;
        --wire) Wire=1; shift ;;
        --) shift; SetupArgs=("$@"); break ;;
        -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: $1（--help 看用法）" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

if [ -z "$From" ]; then From="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
From="$(cd "$From" 2>/dev/null && pwd)" || die "源目录不存在：$From"
[ -f "$From/toolkit/tools/session.py" ] || die "源里没有 toolkit/tools/session.py：$From（--from 指到仓库或 Share 包根）"
[ -f "$From/toolkit/scripts/setup.sh" ] || die "源里没有 toolkit/scripts/setup.sh：$From"

mkdir -p "$To" 2>/dev/null
To="$(cd "$To" 2>/dev/null && pwd)" || die "落点建不出来：$To"
[ -f "$To/toolkit/tools/session.py" ] && die "落点已经装过了：$To/toolkit（要重装请先自己挪走旧目录，我不覆盖你的产物）"
[ "$To" = "$From" ] && die "落点和源是同一个目录。就地安装请直接跑 bash $From/toolkit/scripts/setup.sh，否则换个 --to"
case "$(pwd)/" in
    "$To"/*) die "落点不能包住当前工作区（$(pwd) 在 $To 里面），会自指" ;;
esac

echo "============================================"
echo "  Android 真机测试环境 · 第一次安装"
echo "  源    $From"
echo "  落点  $To"
echo "============================================"

say "0/4 落点能不能写（这一条决定你后面会不会撞 [Errno 13]）"
mkdir -p "$To/toolkit" 2>/dev/null
if ! echo x > "$To/toolkit/.w_probe" 2>/dev/null; then
    die "落点写不进去：$To —— 请 --to 指到你会话工作区之内的目录"
fi
rm -f "$To/toolkit/.w_probe" 2>/dev/null
ok "落点可写：$To"
if [ "${DSH_WORKSPACE:-}" ]; then
    case "$To/" in
        "$DSH_WORKSPACE"/*) ok "在工作区之内（DSH_WORKSPACE=$DSH_WORKSPACE）" ;;
        *) warn "--to 落在当前会话工作区之外：$To。文件策略是 workspace-write 时 AI 侧写不进产物；要么挪进工作区，要么本会话用 danger-full-access" ;;
    esac
fi

say "1/4 铺代码（不带 .venv / storage / __pycache__ —— 前者跨路径即废，后者是你的历史）"
Excl="--exclude=.venv --exclude=storage --exclude=__pycache__ --exclude=*.pyc --exclude=.git --exclude=node_modules"
if command -v rsync >/dev/null 2>&1; then
    rsync -a $Excl "$From/toolkit/" "$To/toolkit/" || die "铺 toolkit 失败"
    for d in plugins skills scripts; do [ -d "$From/$d" ] && rsync -a $Excl "$From/$d/" "$To/$d/"; done
else
    mkdir -p "$To/toolkit"
    for it in tools scripts webui docs assets webui.py requirements.txt; do
        [ -e "$From/toolkit/$it" ] && cp -a "$From/toolkit/$it" "$To/toolkit/$it"
    done
    for d in plugins skills scripts; do [ -d "$From/$d" ] && cp -a "$From/$d" "$To/$d"; done
    find "$To" \( -name __pycache__ -o -name '*.pyc' \) -prune -exec rm -rf {} + 2>/dev/null
fi
for f in 安装说明.md README.md; do [ -f "$From/$f" ] && cp -a "$From/$f" "$To/$f"; done
mkdir -p "$To/toolkit/storage/run_history" "$To/toolkit/storage/tmp" 2>/dev/null
[ -d "$To/toolkit/assets/cases" ] || warn "源里没有 toolkit/assets/cases（用例库）—— 新机器上是空的，跑用例前先把 assets/cases/<包名>/ 放进来"
ok "代码就位 $(du -sh "$To/toolkit" 2>/dev/null | cut -f1) → $To/toolkit"

say "2/4 Python 环境 + 依赖"
SysPy="$(command -v python3 || command -v python)"
if [ -n "$ReuseVenv" ]; then
    [ -x "$ReuseVenv/bin/python" ] || die "--reuse-venv 指过去不是一个能用的 POSIX venv：$ReuseVenv（Windows 建的 .venv 本来就不能跨系统搬，请直连镜像源装）"
    Sp="$(ls -d "$ReuseVenv"/lib/python*/site-packages 2>/dev/null | head -1)"
    [ -n "$Sp" ] || die "在 $ReuseVenv 里找不到 site-packages"
    [ -n "$SysPy" ] || die "系统里找不到 python3，没法建 venv"
    warn "离线模式：从 $ReuseVenv 拷包，不跑 pip install —— 拷过来是什么版本就是什么版本"
    rm -rf "$To/toolkit/.venv"
    # --copies：解释器真身拷进来，不留指向别处的绝对路径软链
    "$SysPy" -m venv --copies "$To/toolkit/.venv" || die "python -m venv 失败"
    Vpy="$To/toolkit/.venv/bin/python"
    Tg="$(ls -d "$To/toolkit/.venv"/lib/python*/site-packages | head -1)"
    n=0
    for p in "$Sp"/*; do
        b="$(basename "$p")"
        case "$b" in pip|setuptools|wheel|pkg_resources|distutils|__pycache__|*.dist-info|*.egg-info) continue ;; esac
        cp -a "$p" "$Tg/$b" && n=$((n+1))
    done
    # .dist-info 必须一起拷，否则 importlib.metadata / pip list 认不出这些包
    for p in "$Sp"/*.dist-info "$Sp"/*.egg-info; do [ -e "$p" ] && cp -a "$p" "$Tg/"; done
    ok "复制了 $n 个包目录（离线）"
else
    [ -n "$SysPy" ] || die "系统里找不到 python3；装了非默认版本用 bash toolkit/scripts/setup.sh --python <路径>"
    bash "$To/toolkit/scripts/setup.sh" ${SetupArgs[@]+"${SetupArgs[@]}"} || die "scripts/setup.sh 失败（上面是它的原文输出，别绕过去手改）"
fi

say "3/4 自检（装完不自证 = 把问题留给第一条用例）"
Vpy="$To/toolkit/.venv/bin/python"
[ -x "$Vpy" ] || Vpy="$SysPy"
"$Vpy" "$To/toolkit/tools/doctor.py" || die "自检没过：按上面每一条的修法改完再跑一次，别硬开用例"

if [ "$Wire" = 1 ]; then
    say "4/4 挂到 DSH profile（--wire 只给清单，不自动改你的 profile）"
    Prof="${DSH_PROFILE:-$HOME/.dsh/profiles/web}"
    Pj="$Prof/package.json"
    if [ -f "$Pj" ]; then
        cp -a "$Pj" "$Pj.bak-$(date +%Y%m%d-%H%M%S)" && warn "已备份 $Pj → *.bak-*"
        grep -q "android-tools-plugin" "$Pj" && warn "package.json 里已有 android-tools 链接；现在指哪自己确认：grep -n android $Pj" \
            || warn "package.json 里还没有；dependencies 加：\"@local/dsh-android-tools\": \"link:$To/plugins/android-tools-plugin\" 且 bundles 数组加同名项"
    else
        warn "找不到 profile 的 package.json：$Pj（DSH_PROFILE 指一下）"
    fi
    warn "插件每次调用都重读 config.target，但改完要重启 harness 才生效。要改的行："
    for f in "$To/plugins/android-tools-plugin/cordis.patch.yml" "$To/plugins/android-auto-test-preset/preset/cordis.patch.yml"; do
        [ -f "$f" ] && grep -n "target:" "$f" | sed "s|^|    $f:|"
    done
    warn "别忘了：cd $To/plugins/android-tools-plugin && node link-dsh-deps.mjs（漏了插件根本起不来）"
else
    say "4/4 未挂接 DSH（没给 --wire）—— 插件/skills 仍指你原来那份工具层"
fi

echo
ok "装完了。落点 $To"
echo "   开会话：cd $To/toolkit && ./.venv/bin/python tools/session.py start --package <包名> ..."
echo "   产物：$To/toolkit/storage（在工作区内，沙箱写得住；用例历史与证据都在这里）"
echo "   换掉正在用的那套插件 target：bash install.sh --wire 只看清单，或照 4/4 手工改 3 行 + 重启"

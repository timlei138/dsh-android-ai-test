"""Measured stages and invocation identity; no device or model policy."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

ROOT = Path(__file__).resolve().parent.parent


def _resolve_storage_root() -> Path:
    """storage/ 放哪？

    ## 解析顺序

      ① `ANDROID_TEST_STORAGE` 环境变量  —— 显式指定，最可靠
      ② **`ROOT / "storage"`**  ← 工具层在 cwd 之下时，产物放工具层旁边
      ③ `cwd / "storage"`        ← 兜底

    ## 为什么 ② 优先

    理想形态是三者并排、自包含：

        <target>/
            tools/  assets/  storage/

    这样"哪个副本在用，产物就在那份里"，换一份工具层不会分不清谁的产物。

    如果 ② 反过来（cwd 优先），而调用方 cwd 是 target 的上一级，
    产物就会落在 `<cwd>/storage/`，和 `tools/`、`assets/` 拆成两处 ——
    看起来像"东西散了两处"。

    ## 什么时候退回 cwd

    工具层在 cwd 之外（直接从安装位置跑，没有独立副本）时，
    `ROOT/storage` 是安装目录 —— 那里不该放产物（用户原话
    「storage 的数据不应该提交」）。这种情况退回 cwd。

    判定方法：看 `ROOT` 是否在 cwd 之下。是 -> `ROOT/storage`；否 -> `cwd/storage`。

    ## 为什么不"自动上溯找能写的地方"

    早期试过写不进去就往上找可写目录。两个问题：
    探测本身不可信（进程权限和实际写入能力可以不一致），
    而"悄悄换个地方落库"会让 AI 以为自己在用 A 库、实际写的是 B 库。
    **所以不猜。** 环境变量显式指定，或按上面的固定规则解析。
    """
    explicit = (os.environ.get("ANDROID_TEST_STORAGE") or "").strip()
    if explicit:
        return Path(explicit).resolve()

    cwd = Path.cwd().resolve()
    root = ROOT.resolve()

    # 工具层在 cwd 之下 -> 产物放工具层旁边（自包含）
    try:
        if root.is_relative_to(cwd):
            return (root / "storage").resolve()
    except (OSError, ValueError):
        pass

    # 工具层在 cwd 之外（安装位置）-> 产物放 cwd，别写进安装目录
    return (cwd / "storage").resolve()


STORAGE_DIR = _resolve_storage_root()


def storage_root_source() -> str:
    """storage/ 这次是按哪条规则定下来的。

    env.py 和 require_writable_storage() 共用这一份判断，
    免得两处各写一套、换机器之后说法不一致。
    """
    if (os.environ.get("ANDROID_TEST_STORAGE") or "").strip():
        return "env:ANDROID_TEST_STORAGE"
    try:
        if ROOT.resolve().is_relative_to(Path.cwd().resolve()):
            return "工具层在 cwd 之下 -> <target>/storage"
    except (OSError, ValueError):
        pass
    return "工具层在 cwd 之外 -> cwd/storage"

# ══════════════════════════════════════════════════════════════════════
# 目录约定
# ══════════════════════════════════════════════════════════════════════
#
#   assets/                      ← **进 git**，是资产不是产物
#     cases/<应用>/              ← 按应用分目录
#       <包名>_<功能>_<编号>.md    ← 原始用例（人写的，或从需求文档粘的）
#       <md文件名>_steps.yaml     ← 跑过之后自动导出的可复用跳转步骤（与 md 同名）
#     knowledge/<包名>.md        ← 知识卡
#     knowledge/paths/<包名>.md  ← 路径图
#
#   storage/                     ← **不进 git**，纯本地运行产物
#     test.db                    ← 单库，所有会话/事件/断言都在这里
#     run_history/<uuid>/        ← 每次运行一个目录，互不干扰
#       evidence/<步号>/         ← 截图 + dump + meta
#       calls/                   ← 调用账本
#       timing/
#       recording_errors/
#     tmp/                       ← Agent 的临时文件（探针、一次性脚本、草稿）
#
# 为什么运行产物按 uuid 分目录：
#   一个全局 evidence/ 的话，跑多了会互相污染：清理一次影响全部历史，
#   查"这次到底点了什么"还要按 session_id 过滤。
#   改成一次运行一个目录后，删一次 = 删一个 uuid 目录，不影响别的。
#
# 为什么 assets 和 storage 分开：
#   用户：「storage 的数据不应该提交。只是本地运行的一些东西。」
#   而用例和知识卡是**要提交的资产** —— 它们决定"下次怎么跑得更快"。

ASSETS_DIR = Path(os.environ.get("ANDROID_TEST_ASSETS", str(ROOT / "assets"))).resolve()
CASES_DIR = ASSETS_DIR / "cases"
# 知识卡放在 assets 下：它是**资产**（进 git、跨设备跨会话复用、决定下次跑得更快），
# 和用例是同一类东西。放根目录会让人以为是代码的一部分。
#   assets/knowledge/<包名>.md + assets/knowledge/paths/<包名>.md
KNOWLEDGE_DIR = Path(os.environ.get(
    "ANDROID_TEST_KNOWLEDGE", str(ASSETS_DIR / "knowledge"))).resolve()
PATHS_DIR = KNOWLEDGE_DIR / "paths"
# 每次运行的落盘目录：一次运行一个目录，目录名是带 `-` 的标准 UUID v4
# （8-4-4-4-12），肉眼可分段读，日志/命令行里复制粘贴也不易粘错。
RECORD_DIR = STORAGE_DIR / "run_history"
TMP_DIR = STORAGE_DIR / "tmp"

# ★ 两个子目录在模块加载时就建好。
#
#   为什么不懒建（用到才建）：
#     AI 会自己 `dir storage` 看有什么。如果 tmp/ 还没建，它会以为这个约定不存在，
#     然后自己找个地方放临时文件（实测踩过：放到了 storage/ 根下）。
#     目录先建出来，约定就是"看得见的"。
_BOOT_MKDIR_ERRORS: list[tuple[str, str]] = []
for _d in (RECORD_DIR, TMP_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError as _exc:
        # 不在这里抛：read_file / kb_check 这类只读工具本来就不需要写。
        # 但也不能装没看见 —— 原来 `except OSError: pass` 的结果是：
        # 目录没建出来、工具照常启动，等到后面某个不相干的调用报一句
        # `PermissionError: [Errno 13]`，人只能拿那句看不懂的话去怀疑设备 / App / 代码。
        # 2026-11-09 实测就是这么绕了一大圈才找到根因。
        _BOOT_MKDIR_ERRORS.append((str(_d), f"{type(_exc).__name__}: {_exc}"))
        print(f"[tools] ⚠️ 产物目录建不出来：{_d} —— {_BOOT_MKDIR_ERRORS[-1][1]}",
              file=sys.stderr)


def probe_writable(dirpath: Path) -> str:
    """实测这个目录写不写得进去：真写一个探针文件再删掉。
    返回空串 = 可写；否则是失败原因。

    ★ 为什么不用 os.access(dir, W_OK)：它查的是 DAC 权限位，
      而沙箱（Linux Landlock / macOS Seatbelt）拦在更上面一层 ——
      权限位显示 rwx、属主是自己，open(O_WRONLY) 照样 EACCES。
      2026-11-09 实测：目录 xuyl:xuyl 775，`touch` 报 [Errno 13]。
      只有真写一次才作数。
    """
    try:
        probe = dirpath / ".write_probe"
        probe.write_text("probe", encoding="utf-8")
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        probe.unlink()
    except OSError:
        pass
    return ""


def writability_report() -> dict[str, str]:
    """storage 和 assets 分开实测，两处都得测。

    test.db 在 storage/，而 steps.yaml 和知识卡写回在 assets/。
    两边不同挂载点 / 不同沙箱范围时会出现
    「库写得进、资产悄悄写不回」的半瘫状态 —— 跑完看着一切正常，
    下次还是得从头摸路。
    """
    out = {}
    for name, d in (("storage", STORAGE_DIR), ("run_history", RECORD_DIR),
                    ("tmp", TMP_DIR), ("knowledge", KNOWLEDGE_DIR),
                    ("cases", CASES_DIR)):
        if not d.is_dir():
            # 没建出来才算失败；只是还没建就先建 —— 否则新装机器上
            # "目录不存在" 会被当成写不进去，把人往错的方向带。
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                out[name] = f"建不出来 {type(exc).__name__}: {exc}"
                continue
        out[name] = probe_writable(d)
    return out


def _suggest_storage() -> str:
    """给一条**实测过**能写出来的 storage 路径；没有就给占位符。

    原来这里无条件印 `export ANDROID_TEST_STORAGE="<cwd>/storage"`。当 cwd
    就在工具层子目录里（人常停在 assets/cases/<包名>/ 下翻用例）、而工具层
    整棵又被沙箱挡住时，它建议的目标 == 刚刚实测失败的那个目录 —— 照着敲
    一遍，报错原样复现，比不提示更坏（看着像已经按提示做过功课了）。
    所以这里先实测父目录能不能建，再拿出来建议。
    """
    for cand in (Path.cwd() / "storage", Path(tempfile.gettempdir()) / "android-test-storage"):
        try:
            if cand.resolve() == STORAGE_DIR.resolve():
                continue
        except OSError:
            continue
        base = cand.parent if cand.parent.exists() else cand.parent.parent
        if probe_writable(base if base.exists() else Path(tempfile.gettempdir())):
            continue
        if probe_writable(cand):          # 自己建不出来就别推荐它
            continue
        return str(cand)
    return "<一个可写目录>/storage"


def storage_fix_hint(suggest=None) -> str:
    """按当前平台给出「把 storage 指到别处」的那一行命令。

    原来 env.py 只印 PowerShell 写法（`$env:X = "..."`），
    在 bash 上照着敲就是 syntax error —— 报错了却给不出可执行的解法。
    """
    d = str(suggest or (Path.cwd() / "storage"))
    if os.name == "nt":
        return f'$env:ANDROID_TEST_STORAGE = "{d}"'
    return f'export ANDROID_TEST_STORAGE="{d}"'


def require_writable_storage(what: str) -> None:
    """写命令进门先实测 storage，写不进去就地停下。

    ★ 绝不"悄悄换个地方落库"（理由见 _resolve_storage_root 的注释：
      那会让 AI 以为在用 A 库、实际写的是 B 库，比直接报错坏得多）。
      这里只把一句看不懂的 Errno 13，换成人能照着做的几条选择。
    """
    report = writability_report()
    hard = [k for k in ("storage", "run_history", "tmp") if report.get(k)]
    # knowledge / cases 是软失败：只影响 steps.yaml 与知识卡写回，不拦命令
    if not hard:
        soft = [k for k in ("knowledge", "cases") if report.get(k)]
        if soft:
            print(f"[tools] ⚠️ storage 可写，但资产目录写不回（{', '.join(soft)}）："
                  "steps.yaml / 知识卡会静默丢失", file=sys.stderr)
        return
    why = report.get("storage") or report.get("run_history") or report.get("tmp") or "?"
    hint = storage_fix_hint(suggest=_suggest_storage())
    stuck = STORAGE_DIR if "storage" in hard else (
        RECORD_DIR if "run_history" in hard else TMP_DIR)
    print("\n".join([
        "",
        "✗ storage 写不进去 —— 拒绝开始（不会换个地方偷偷落库）",
        f"  要做的操作  : {what}",
        f"  卡住的目录  : {stuck}",
        f"  失败原因    : {why}",
        f"  按哪条规则  : {storage_root_source()}",
        f"  工具层 ROOT : {ROOT}",
        f"  当前 cwd    : {Path.cwd()}",
        "",
        "  照下面任一条改完，重跑同一条命令即可：",
        f"    ① 推荐：把工具层装进 harness 会话的**可写工作区**内"
        f"（当前工具层 {ROOT} 整棵都不在可写范围内，只挪 storage 治不了本："
        "steps.yaml / 知识卡照样写不回）",
        f"    ② cwd 别停在工具层的子目录里（用例目录等）：cd {ROOT}",
        f"    ③ 显式指到可写位置：{hint}",
        f"    ④ 若是 DSH 会话沙箱拦的：会话文件策略放宽到 workspace-write 及以上，"
        f"且工作区要覆盖 {stuck.parent}",
        "",
    ]), file=sys.stderr)
    raise SystemExit(75)

CALL_ID = os.environ.get("ANDROID_TEST_CALL_ID") or uuid.uuid4().hex
PROCESS_STARTED = time.perf_counter()
STAGES: dict[str, float] = {}
EVENT_IDS: list[int] = []
EXECUTION_STATE = "unknown"
P = ParamSpec("P")
T = TypeVar("T")


def current_record_dir() -> Path | None:
    """本次运行属于哪个 run_history 目录？

    两个来源，环境变量优先：

      ① `ANDROID_TEST_RECORD` —— 同一个进程内（session.py 内部）传播快
      ② `run_history/.current` 磁盘指针 —— 跨进程用这个

    ⚠️ 为什么必须有两个：observe/act 是独立进程，环境变量传不过去。
       只靠 ① 的话，一次运行的产物会散在 run_history/<uuid>/ 和
       storage/evidence/ 两处。
    """
    raw = (os.environ.get("ANDROID_TEST_RECORD") or "").strip()
    if raw:
        p = Path(raw).resolve()
        if p.is_dir():
            return p
    return _pinned_record()


def new_record_dir() -> Path:
    """建一个新的运行目录（uuid），返回路径。

    目录名用**带 `-` 的标准 UUID v4**（8-4-4-4-12），便于肉眼分段阅读。
    """
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    d = RECORD_DIR / str(uuid.uuid4())
    for sub in ("evidence", "calls", "timing", "recording_errors"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


# ══════════════════════════════════════════════════════════════════════
# 环境探测：告诉 AI 这台机器上该用什么 shell 语法
# ══════════════════════════════════════════════════════════════════════
#
# 用户的要求（原话）：
#   「skills 应该能够根据当前用户的设备情况。自动调整命令的格式和写法。
#     而不是我们强制限定死。」
#
# 所以不是把 SKILL 写成"Windows 用 PowerShell"（那在 Linux 上就错了），
# 而是**在会话开始时探测一次，把结论告诉 AI**。
#
# 实测数据：72 条 bash 命令里有 8 条 Linux 写法（11%）——
# 那些是在 Windows 上跑的，其中 `ls -la` / `2>/dev/null` / `||` 会失败。
# 但同样的写法在 Linux 上是对的，所以不能一刀切。


def detect_shell() -> dict:
    """探测当前环境，返回写命令时该用的语法。

    返回：
        {
          "platform": "win32" | "linux" | "darwin" | ...,
          "shell": "powershell" | "cmd" | "bash" | "zsh" | "unknown",
          "hint": "给 AI 看的一句话说明",
          "path_sep": "\\\\" | "/",
          "tmp_dir": "..."        ← 临时目录的真实路径
        }

    ★ 判定顺序很重要：
      ① 有 SHELL 环境变量且含 bash/zsh  -> 那才是真 shell（Git Bash/WSL）
      ② 否则看 sys.platform
         win32 -> powershell（现代 Windows 的默认）
      ③ 拿不准就返回 unknown，让 AI 自己试
    """
    import os as _os
    import sys as _sys
    import tempfile

    plat = _sys.platform
    shell_env = (_os.environ.get("SHELL") or "").strip()
    comspec = (_os.environ.get("ComSpec") or "").strip()

    shell = "unknown"
    hint = ""

    if "bash" in shell_env:
        shell = "bash"
        hint = ("当前是 bash：`ls -la` / `head -20` / `&&` / `2>/dev/null` / "
                "`/tmp` 都能用。")
    elif "zsh" in shell_env:
        shell = "zsh"
        hint = "当前是 zsh：bash 的写法基本都能用。"
    elif plat == "win32":
        # Windows 上还要看有没有 Git Bash / WSL 的 bash
        shell = "powershell" if comspec or True else "cmd"
        hint = ("当前是 Windows（PowerShell）。`ls -la` / `head` / `&&` / "
                "`2>/dev/null` 会失败 —— 用 `dir` / `Get-Content -TotalCount` / "
                "`;` / `2>$null`。**但 `adb shell \"...\"` 里的内容仍是 "
                "Android 的 Linux，照常用 grep 等**。")
    elif plat in ("linux", "darwin"):
        shell = "bash"
        hint = "当前是 Linux/macOS：bash 写法都能用。"

    return {
        "platform": plat,
        "shell": shell,
        "path_sep": "\\" if plat == "win32" else "/",
        "tmp_dir": tempfile.gettempdir(),
        "hint": hint,
    }


# ══════════════════════════════════════════════════════════════════════
# 本次运行真正会被 spawn 的 shell 解释器
# ══════════════════════════════════════════════════════════════════════
#
# 为什么工具层要关心这件事：
#
#   DSH 在 Windows 上的 `ctx.shell` 由 `@deepseek-ai/dsh-pwsh-sandbox` 提供，
#   它 extends `PwshLocalExecutor` —— 也就是**每一个 android_* 工具调用，
#   都先起一个 PowerShell 进程**，再由它去起 python。
#
#   而 PowerShell 7 和 Windows 自带 5.1 的**启动开销差一倍多**（实测中位
#   937ms vs 2320ms）。落在 5.1 上，每次工具调用白付约 1.4 秒；一条用例
#   三十来次调用就是 40 秒上下，一整批几十条就是几十分钟。
#
#   更麻烦的是它**不报错**：`resolvePwshPath()` 找不到 pwsh.exe 时会静默
#   兜底到最后那个候选 `System32\WindowsPowerShell\v1.0\powershell.exe`。
#   没装 PS7 的机器上，一切照常工作，只是慢 —— 没人会发现。
#
# 所以这里把 DSH 的选择规则**原样复刻一遍**，让 doctor / env 能提前说出来。
# 复刻的依据是 `@deepseek-ai/dsh-pwsh-local/lib/index.js`：
#   candidatePwshPaths()  L23-34   候选顺序
#   candidateExists()     L42-49   lstat 后 isFile() || isSymbolicLink()
#   resolvePwshPath()     L59-65   取第一个存在的
# ⚠️ DSH 升级若改了这三个函数，这里要跟着改（doctor 会把路径报出来，好复核）。

_PWSH7_HINT = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"


def _shell_candidates() -> list[Path]:
    """按 DSH 的顺序列出候选解释器（不含 config.pwshPath 那条显式覆盖）。

    candidatePwshPaths() 的三段：ProgramFiles 下 PS7 的固定位置 → PATH 里每
    个目录下的 pwsh.exe → System32 的 5.1 兜底。PATH 的分隔符和引号剥离也
    照抄（Windows 上 `;`，条目可能带引号）。
    """
    out: list[Path] = []
    program_files = os.environ.get("ProgramFiles") or "C:\\Program Files"
    system_root = os.environ.get("SystemRoot") or "C:\\Windows"

    out.append(Path(program_files) / "PowerShell" / "7" / "pwsh.exe")
    for entry in (os.environ.get("PATH") or "").split(";"):
        trimmed = entry.strip().strip('"')
        if not trimmed:
            continue
        out.append(Path(trimmed) / "pwsh.exe")
    out.append(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    return out


def _candidate_exists(path: Path) -> bool:
    """DSH 用的判据：lstat 之后是文件或符号链接（不跟随重解析点）。

    Store 版 pwsh 在 WindowsApps 下是个 execution alias，Node 报 symlink、
    老版本 Node 报普通文件 —— 两种都算存在。所以这里不能用 `Path.exists()`：
    它对 alias 会跟随到目标，目标 ACL 不允许时抛 OSError 判成"不存在"，
    于是错误地认为没装 PS7。用 lstat 语义（`is_symlink()` 先判）才和 DSH 一致。
    """
    try:
        st = path.lstat()
    except OSError:
        return False
    import stat as _stat
    return _stat.S_ISREG(st.st_mode) or _stat.S_ISLNK(st.st_mode)


def resolve_shell_exe() -> dict:
    """本次运行 `ctx.shell` 会 spawn 哪个解释器 —— 复刻 DSH 的选择结果。

    返回：
        {
          "path": "<解释器绝对路径>" | None,
          "kind": "pwsh7" | "pwsh7-path" | "powershell5" | "pwsh" | "unknown",
          "version": "7" | "5.1" | "",
          "slow": True | False,        ← 落在 5.1 就是 True
          "advice": "..."              ← 只在慢时给话
        }

    kind 的含义：
        pwsh7       → 命中了 ProgramFiles\\PowerShell\\7（MSI 包）
        pwsh7-path  → PATH 里找到的 pwsh.exe（Store / MSIX 包通常走这条）
        powershell5 → 兜底的 Windows PowerShell 5.1（慢，而且是「没装 PS7」的信号）
        pwsh        → 非 Windows，按 PATH 解析
    """
    if os.name != "nt":
        return {"path": None, "kind": "pwsh", "version": "", "slow": False, "advice": ""}

    chosen: Path | None = None
    for cand in _shell_candidates():
        if _candidate_exists(cand):
            chosen = cand
            break

    if chosen is None:
        return {"path": None, "kind": "unknown", "version": "", "slow": False,
                "advice": "找不到任何 PowerShell（连系统自带的 5.1 都没有）—— DSH 的 shell 服务起不来。"}

    name = chosen.name.lower()
    parent_parts = [p.lower() for p in chosen.parts]
    if name == "powershell.exe":
        return {
            "path": str(chosen),
            "kind": "powershell5",
            "version": "5.1",
            "slow": True,
            "advice": (
                "本次跑的是 Windows 自带的 PowerShell 5.1，不是 pwsh 7。"
                "每次 android_* 工具调用都要多花约 1.4 秒起 shell（实测 5.1 中位 2320ms、pwsh 7 是 937ms），"
                "一条用例三十来次调用 ≈ 40 秒，长任务累积很大。"
                "装 PowerShell 7 后**重启 DSH**（pwsh 路径在进程启动时解析并缓存）即可，"
                "命令：winget install --id Microsoft.PowerShell --source winget"
            ),
        }
    if "powershell" in parent_parts and "7" in parent_parts:
        return {"path": str(chosen), "kind": "pwsh7", "version": "7", "slow": False, "advice": ""}
    # PATH 里命中的 pwsh.exe —— Store 版走这条（WindowsApps 下的 alias）
    return {"path": str(chosen), "kind": "pwsh7-path", "version": "7", "slow": False, "advice": ""}


# ══════════════════════════════════════════════════════════════════════
# "当前运行目录" 的磁盘指针
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ 环境变量不够用。observe.py / act.py 是**独立进程**，
#    session.py 在自己进程里设的 ANDROID_TEST_RECORD 传不过去。
#    只靠环境变量的话，session start 建出 run_history/<uuid>/ 之后，
#    紧接着的 observe 拿不到，又落回 storage/evidence/ —— 一次运行的东西散在两处。
#
#    所以除了环境变量，还要写一个磁盘指针 `run_history/.current`，
#    后续进程启动时读它。

_CURRENT_POINTER = RECORD_DIR / ".current"
# 上一次已收尾的运行目录。finish 时由 unpin_record() 写入，
# 供"会话结束后才入库"（case save）这类操作定位到正确的库。
_LAST_POINTER = RECORD_DIR / ".last"


def pin_record(d: Path) -> None:
    """把"当前运行目录"写进磁盘，供后续独立进程读取。"""
    d = Path(d).resolve()
    d.mkdir(parents=True, exist_ok=True)
    _CURRENT_POINTER.parent.mkdir(parents=True, exist_ok=True)
    _CURRENT_POINTER.write_text(str(d), encoding="utf-8")
    os.environ["ANDROID_TEST_RECORD"] = str(d)


def unpin_record() -> None:
    """会话结束后清掉指针（不然下一次会误接到旧目录）。

    ★ **同时留下 `.last` 存档。**

      为什么：`session finish` 之后调用 `case save` 必失败 ——
        finish -> unpin_record() -> `.current` 没了
        case save 是独立进程 -> current_record_dir() 返回 None
        -> 没有上下文，查不到那个会话 -> "会话不存在"

      实测后果：一批用例跑完后，**很多条一条都没写进用例库**，
      因为子 Agent 都在 finish 之后才想到入库。

      `.last` 只是"上一段运行在哪"，不参与任何默认路径计算
      （否则又会污染下一次运行）。只给 `case save --last` 这种显式用法读。
    """
    try:
        # 先存档，再删指针 —— 顺序反了就丢信息
        try:
            txt = _CURRENT_POINTER.read_text(encoding="utf-8").strip()
            if txt:
                _LAST_POINTER.write_text(txt, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        _CURRENT_POINTER.unlink(missing_ok=True)
    except OSError:
        pass
    os.environ.pop("ANDROID_TEST_RECORD", None)


def last_record_dir() -> Path | None:
    """上一次已收尾的运行目录（`.last`）。给 finish 之后的 case save 用。

    ⚠️ 读的时候用 `utf-8-sig`：PowerShell 的 `Set-Content -Encoding utf8`
    会写 BOM，带 BOM 的路径字符串拼不出有效目录，`Path()` 会静默失败。
    （2026-10-28 实测踩过一次 —— 手工修指针时用 Set-Content 覆盖了文件，
     之后 `case save --last` 就找不到上一次的库了。）
    """
    try:
        txt = _LAST_POINTER.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not txt:
        return None
    p = Path(txt).resolve()
    if (p / "db.sqlite3").is_file():
        return p
    return None


def _pinned_record() -> Path | None:
    try:
        # utf-8-sig 同 last_record_dir()：防外部工具写 BOM 后路径解析失败
        txt = _CURRENT_POINTER.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not txt:
        return None
    p = Path(txt).resolve()
    return p if p.is_dir() else None


@contextmanager
def stage(name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        STAGES[name] = STAGES.get(name, 0.0) + (time.perf_counter() - started) * 1000


def timing_snapshot() -> dict:
    return {
        "call_id": CALL_ID,
        "measured_at": datetime.now().isoformat(timespec="milliseconds"),
        "process_observed_ms": round((time.perf_counter() - PROCESS_STARTED) * 1000, 3),
        "stages_ms": {name: round(value, 3) for name, value in STAGES.items()},
        "scope": "since runtime import; startup and earlier imports require launcher timing",
    }


def timed(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with stage(name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


def timed_call(name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    with stage(name):
        return function(*args, **kwargs)


def execution(state: str) -> None:
    global EXECUTION_STATE
    EXECUTION_STATE = state


def call_path(storage: Path, session_id: int | None, call_id: str) -> Path:
    return storage / "calls" / (str(session_id) if session_id is not None else "unbound") / f"{call_id}.json"


def invocation_paths(storage: Path, session_id: int | None = None):
    root = storage / "calls"
    root.mkdir(parents=True, exist_ok=True)
    migrated = root / ".partitioned"
    if not migrated.exists():
        for path in root.glob("*.json"):
            try:
                call = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(call, dict):
                    raise ValueError(f"Invalid invocation record: {path}")
                destination = call_path(storage, call.get("session_id"), path.stem)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f"Duplicate invocation record: {path}")
                path.rename(destination)
            except FileNotFoundError:
                if path.exists():
                    raise
        migrated.touch()
    if session_id is not None:
        return (root / str(session_id)).glob("*.json")
    return root.glob("*/*.json")


def recording_issues(storage: Path, session_id: int) -> list[str]:
    """本会话是否留下了需要人工裁决的"悬空动作"。

    ⚠️ 只锁定"可能已执行"的调用。用法错误（参数不对、工具入口即拒绝）
    确定没碰设备，不构成悬空动作 —— 否则一次手误就要作废整个会话。
    区分见 run.py:is_usage_error 与 audit 记录里的 executed 字段。
    """
    issues = []
    errors = storage / "recording_errors" / str(session_id)
    if errors.exists() and any(errors.iterdir()):
        issues.append("recording failure marker exists")
    for path in invocation_paths(storage, session_id):
        call = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(call, dict):
            raise ValueError(f"Invalid invocation record: {path}")
        audit = call.get("audit", {})
        if not isinstance(audit, dict):
            raise ValueError(f"Invalid invocation audit: {path}")
        if call.get("session_id") != session_id or not call.get("expects_event"):
            continue
        if call.get("call_id") == CALL_ID:
            continue
        # 明确未执行的调用放行（其动作没有发生，无需裁决）
        if audit.get("executed") is False:
            continue
        if call.get("status") == "started" or not audit.get("complete", False):
            issues.append(f"incomplete invocation: {call.get('call_id')}")
    return issues

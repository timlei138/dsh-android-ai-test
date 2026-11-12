"""共享底座：设备连接 + 证据落盘 + 会话事件自动记录。
所有工具无流程逻辑，只提供"看/动/读"原语。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import os
import runtime
from datetime import datetime
from pathlib import Path
from runtime import (
    CALL_ID, CASES_DIR, EVENT_IDS, KNOWLEDGE_DIR, PATHS_DIR, STORAGE_DIR,
    current_record_dir, recording_issues, stage, timed, timing_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_DIR = STORAGE_DIR / "evidence"


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    ⚠️ 必须在 **argparse 解析之前**生效：用法错误（如给 tap 传 --text）
    会由 argparse 直接写 stderr，内容含中文。若沿用控制台默认编码（Windows
    上是 GBK），调用方按 UTF-8 解码时会抛 UnicodeDecodeError，把一个
    "参数写错" 放大成 "输出无法解析"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


ensure_utf8_stdio()


def payload_to_argv(payload: dict | None) -> list[str]:
    """把 stdin JSON 归一成 argparse 能吃的参数列表。

    两种写法都认：

        {"args": ["--card", "com.x", "--query", "y"]}      ← 标准
        {"card": "com.x", "query": "y"}                    ← 扁平

    ⚠️ 为什么要兼容扁平写法（2026-10-29 实测）：
      176 那轮 AI 连续 4 次调 android_read_file 报
      「需要 --path <文件> 或 --card <包名>」—— 它按直觉传了扁平字段，
      而解析只认 args，于是当成空参数。白花约 12 秒 + 4 次思考。

      这份约定只写在 docstring 里、SKILL 没教，AI 猜不到。既然 --stdin-json
      本来就是为了绕开参数传递的坑，那就做到底：怎么写都能用。

    映射规则：key -> --key（下划线换短横线）
      True          -> 只给开关            {"full": true}   -> --full
      False / None  -> 忽略（不传空值）
      list          -> 重复给参数          {"section": ["a","b"]} -> --section a --section b
      其它          -> --key value
    """
    payload = payload or {}
    argv = payload.get("args")
    if argv:
        return [str(x) for x in argv]
    derived: list[str] = []
    for key, val in payload.items():
        if key == "args":
            continue
        flag = "--" + str(key).strip().replace("_", "-")
        if val is False or val is None:
            continue
        if val is True:
            derived.append(flag)
        elif isinstance(val, (list, tuple)):
            for item in val:
                if item is None or item is False:
                    continue
                if item is True:
                    derived.append(flag)
                else:
                    derived.extend([flag, str(item)])
        else:
            derived.extend([flag, str(val)])
    return derived


def apply_stdin_args() -> None:
    """支持 `--stdin-json`：从 stdin 读参数，绕开命令行引号问题。

    ## 为什么需要（2026-10-28 实测）

    `android_read_file(section='坑-删除全部课程表后的两种"空"')` 报
    `usage: read_file.py`。查下来是**参数里带双引号**时：

        node 侧用 POSIX 规则把 " 转义成 \\"
        Windows cmd 不认这个转义，把引号当字符串结束符
        -> Python 收到 `"坑-...的两种\\"` 被拆成两段 -> argparse 报错

    改 `""` 双写也不对（cmd 会把两个引号都当字面量）。

    ## 做法

    参数走 stdin 的 JSON，命令行只留脚本路径：

        python read_file.py --stdin-json
        stdin: {"args": ["--path", "...", "--section", "坑-...的两种\\"空\\""]}

    这样空格、引号、`&`、`|`、`%`、中文、emoji 全部安全 ——
    它们根本没进入命令行解析。

    ## 调用方式

    在每个脚本的 `main()` 里，`parse_args()` 之前调一次：

        from common import apply_stdin_args
        apply_stdin_args()
        args = p.parse_args()

    它只在检测到 `--stdin-json` 时才动 sys.argv，其他情况完全不干预
    （所以手工跑 `python act.py tap --x 1 --y 2` 照旧）。
    """
    if "--stdin-json" not in sys.argv:
        return
    import json as _json
    try:
        # ⚠️ 必须读 **bytes 再显式按 UTF-8 解码**，不能用 `sys.stdin.read()`。
        #
        # `sys.stdin.read()` 用的是 `sys.stdin.encoding`，而它在子进程里可能是
        # **gbk**（Windows 上 PYTHONIOENCODING/PYTHONUTF8 没继承到时就退成系统
        # 编码，本机实测 `sys.stdin.encoding == 'gbk'`）。拿 gbk 去解 UTF-8 字节，
        # 解不出来的部分会变成 surrogate（`'\udc92'` 这种），于是：
        #   · 参数看着"有字"，但中文变成了乱码；
        #   · 往 meta.json / DB 写 JSON 时直接抛
        #     `UnicodeEncodeError: 'utf-8' codec can't encode character
        #      '\udc92' ... surrogates not allowed`
        #     —— 表现是「动作做了但采集失败」，2026-11-12 实测带中文 --why 必挂。
        #
        # 走 buffer 就没有"猜编码"这一步：argv 的 JSON 约定就是 UTF-8。
        raw = sys.stdin.buffer.read().decode("utf-8-sig")  # sig：顺手吃掉 BOM
        payload = _json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return
    argv = payload_to_argv(payload)
    # 保留脚本名，把 JSON 里的参数当成命令行
    sys.argv = [sys.argv[0]] + argv


def _adb(*args: str, timeout: float = 20.0) -> tuple[int, str]:
    """调一次 adb，返回 (returncode, 合并输出)。

    ⚠️ 只在"连接已经失败"的恢复路径上调用 —— 正常路径不该有额外进程开销。
    adb 不在 PATH 时返回 (-1, 原因)，由调用方决定是否提示，不在这里抛。
    """
    import subprocess
    try:
        proc = subprocess.run(["adb", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return -1, "adb 不在 PATH"
    except OSError as error:
        return -1, str(error)
    except Exception as error:  # noqa: BLE001  subprocess.TimeoutExpired 等
        return -1, f"{type(error).__name__}: {error}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _u2_connect(serial: str | None):
    import uiautomator2 as _u2
    return _u2.connect(serial) if serial else _u2.connect()


def _dir_writable(path: str | None) -> bool:
    """真写一个文件再删，别只信 ACL 查询。"""
    import os as _os
    if not path or not _os.path.isdir(path):
        return False
    probe = _os.path.join(path, f".dsh_probe_{_os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        _os.remove(probe)
        return True
    except OSError:
        return False


def ensure_writable_temp() -> str | None:
    """确保 TEMP 可写；不可写就指到工作区内的目录。返回换到的路径（没换返回 None）。

    ⚠️ 为什么需要（2026-10-29 实测，这是 adb 反复报 10061 的真根因）：

      adb daemon 启动时要在 `%TEMP%` 建 `adb.log`。沙箱下 **Python 进程
      对 `%TEMP%` 只读**，于是：

          F adb: main.cpp:52 cannot open ...\\Temp\\adb.log: Permission denied
          * failed to start daemon
          → 5037 不监听 → 之后所有连接 WinError 10061

      而 `pwsh` 能写，所以 `adb devices` 在 bash 里跑就正常 —— 这解释了
      为什么"人工敲一次就好、工具调就不行"。

    ## 为什么是换 TEMP，而不是自己起 server

      adbutils 的 `_safe_connect()` **本来就自带 `adb start-server`**
      （见 .venv/Lib/site-packages/adbutils/_adb.py:69）。那是被大量项目
      验证过的机制，不该由我们重写或拦掉。

      实测（清空 daemon 后）：给 Python 一个可写的 TEMP，
      `u2.connect()` 自己就把 daemon 拉起来了、5037 正常监听、
      adb.log 落在新目录 —— **库的功能完全正常，缺的只是权限。**

      所以这里只补权限，不碰 adb 策略：能写就不动，写不了才换。
      正常机器上（Python 能写 %TEMP%）这个函数什么都不做。
    """
    import os as _os
    cur = _os.environ.get("TEMP") or _os.environ.get("TMP")
    if _dir_writable(cur):
        return None
    target = STORAGE_DIR / "tmp" / "_adb"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # TEMP 和 TMP 都改 —— adb 两个都看，只改一个可能不生效
    _os.environ["TEMP"] = str(target)
    _os.environ["TMP"] = str(target)
    return str(target)


_ADB_SERVER_HINT = (
    "adb server 连不上（WinError 10061）。\n"
    "处置：先重试一次本工具（工具会自己把 server 拉起来）。\n"
    "仍不行就在 bash 里跑一次 `adb devices`，确认能看到 "
    "`<serial>	device`；还是不行就如实上报，不要硬试。"
)


def _adb_server_alive(host: str = "127.0.0.1", port: int = 5037,
                      timeout: float = 0.1) -> bool:
    """探一下 5037 有没有在监听。~0ms，不起进程、不碰 adb。

    ⚠️ 为什么需要（2026-10-29 实测）：
      裸 `u2.connect()` 在 server 没运行时**要 14.6 秒**才抛 10061
      （uiautomator2 内部重试）。而真正的原因一句"server 没在跑"就说清了。
      先探一下，把 14.6 秒降到 0.1 秒，且提示准确。
    """
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def connect(serial: str | None):
    """延迟导入 uiautomator2：无依赖环境也能 --help / 查看用法。

    ## 这里不做自己的 ADB 恢复逻辑

    adbutils 的 `_safe_connect()` **自带 `adb start-server`**
    （.venv/Lib/site-packages/adbutils/_adb.py:69），那是被大量项目验证过的
    机制。我们**不重写、也不拦它**。

    工具层只做两件库管不到的事：

      ① **探测 5037（100ms）**：server 没跑时裸 connect 要 14.6 秒才抛 10061，
         先探一下就能立刻说清原因。
      ② **保证 TEMP 可写**（见 `ensure_writable_temp`）：这是本机 adb 反复
         报 10061 的真根因 —— daemon 要写 `%TEMP%\\adb.log`，而沙箱下
         Python 对 `%TEMP%` 只读。补上权限后，库自己的 start-server 就能工作。

    也就是说：**真正把 server 拉起来的是 adbutils，我们只负责给它能干活的环境。**
    """
    try:
        with stage("device_import"):
            import uiautomator2 as _u2  # noqa: F401
    except ImportError:
        fail("缺依赖：先建环境 pip install -r requirements.txt")

    with stage("adb_probe"):
        if not _adb_server_alive():
            # server 没跑。先确保 TEMP 可写（否则库起 daemon 也会失败），
            # 再交给 adbutils 的 start-server —— 它会自己拉起来。
            ensure_writable_temp()

    with stage("device_connect"):
        try:
            return _u2_connect(serial)
        except Exception as error:  # noqa: BLE001
            text = str(error)
            if "10061" in text or "积极拒绝" in text or "refused" in text:
                fail(_ADB_SERVER_HINT)
            fail(f"设备连接失败：{error}\n"
                 "处置：在 bash 里跑 `adb devices` 确认设备是 `device` 态；"
                 "仍不行就如实上报，不要硬试。")


def try_connect(serial: str | None):
    """静默版：连不上返回 None（用于可有可无的设备操作，如 start 时清 crash 缓冲区）。"""
    try:
        import uiautomator2 as _u2
        return _u2.connect(serial) if serial else _u2.connect()
    except Exception:  # noqa: BLE001
        return None


def rel(path: Path) -> str:
    """证据路径转 skill 根相对（posix），Web UI 按此路径伺服。"""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _card_keywords(card: Path) -> list[str]:
    """从卡片里抽「跨模块指路」：正文提到的其它包名。

    用途见 knowledge_hint 的 cross_cards 分支。

    ⚠️ 必须显式剥掉 `.md` 后缀：`\\b(com\\.[a-z0-9_.]+)\\b` 会把后缀一起吞进
    包名（得到 ``com.zui.calendar.md``），拼出来的路径必然不存在，
    于是**跨卡指路静默失效**。
    """
    try:
        text = card.read_text(encoding="utf-8")
    except OSError:
        return []
    found = re.findall(r"\b((?:com|android)\.[a-z0-9_.]+?)(?:\.md)?\b", text)
    return [f for f in found]


def _sections_matching(sections: list[str], screen: str | None) -> list[str]:
    """按当前屏幕/前台包，把相关小节挑到前面。

    ⚠️ 真实教训（用例 121「加倒数日小部件 + 开负一屏」）：
    `knowledge_hint` 只平铺小节标题，AI 扫一眼觉得"跟我这步无关"就跳过，
    结果**把已经写好的坐标重新探了一遍**，还探错了（旧记录坐标与本机不符）。

    真正的病根有两个，都在这里一起治：
      1. **索引漏了 `###` 级小节**（见 _scan_sections）：关键小节是 `###`，
         压根没进索引，AI 不可能"看到但不读"——它是根本没出现。
      2. 标题平铺、没有相关性排序。

    这里做的是**低成本的粗筛**：用前台 activity 的词与常见界面词匹配小节标题，
    命中的置顶。不做语义理解，判错也只是排序差异，不会让 AI 看不到任何小节。
    """
    if not screen:
        return []
    tokens: set[str] = set()
    screen_l = screen.lower()
    for part in re.split(r"[._/]", screen_l):
        # 整词（如 zuilaunchersettings）几乎不可能出现在中文标题里，
        # 真正有用的是把它拆成可辨识的词根，以及下面 _SYNONYM 的界面词。
        if len(part) > 2:
            tokens.add(part)
    # camelCase 拆词：ZuiLauncherSettings -> zui/launcher/settings
    for part in re.split(r"[._/]", screen):
        for word in re.findall(r"[A-Z][a-z]+", part):
            tokens.add(word.lower())
    # 常见界面词 → 标题里可能出现的中文（保持克制，只放高置信的）
    _SYNONYM = {
        "launcher": ["桌面", "负一屏", "小部件", "widget"],
        "settings": ["设置", "开关"],
        "systemui": ["通知", "状态栏"],
        "permission": ["权限"],
        "widget": ["小部件", "widget"],
        "countdown": ["倒数日"],
        "birthday": ["生日"],
        "curriculum": ["课程"],
    }
    # 太泛的词会误命中（实测 'settings' 把「课程时间设置页」也拉进来了），
    # 只在没有更具体命中时才允许它们参与。
    _GENERIC = {"settings", "activity", "main", "info", "event"}
    hits: list[str] = []
    weak: list[str] = []
    for section in sections:
        sec_l = section.lower()
        strong = False
        for tok in tokens:
            if tok in _GENERIC:
                continue
            if tok in sec_l:
                strong = True
                break
            for alias in _SYNONYM.get(tok, []):
                if alias.lower() in sec_l:
                    strong = True
                    break
            if strong:
                break
        if strong:
            hits.append(section)
            continue
        for tok in tokens:
            if tok in _GENERIC and tok in sec_l:
                weak.append(section)
                break
    # 弱命中只在强命中为空时兜底，避免把泛词噪音排在真相关项前面
    return hits if hits else weak


def _scan_sections(card: Path) -> tuple[list[str], list[str]]:
    """扫卡片的 `##` 与 `###` 小节标题。

    ⚠️ **必须同时收 `###`**：卡里有些关键小节是挂在 `##` 之下的 `###`，
    只收 `## ` 的话**这一节根本不会出现在 knowledge_hint 的索引里** ——
    AI 不是"看到标题却不读"，而是看不到，只好从头重新探索，往往还探错坐标。

    返回 (全部小节, 顶层小节)。`###` 会带上前缀的父级，便于 AI 判断上下文。
    """
    top: list[str] = []
    allsec: list[str] = []
    parent = ""
    try:
        lines = card.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], []
    for line in lines:
        if line.startswith("### "):
            name = line[4:].strip()
            if name:
                allsec.append(f"{parent} > {name}" if parent else name)
        elif line.startswith("## "):
            name = line[3:].strip()
            if name:
                top.append(name)
                allsec.append(name)
                parent = name
    return allsec, top


_SYSTEM_CARD = "assets/knowledge/_system.md"


def _system_card_hint() -> dict:
    """兜底卡（跨 App 的系统行为）的索引。

    ⚠️ 必须给 `sections` 目录，不能只给一个路径字符串：只给
      `"assets/knowledge/_system.md"` 一句话，AI 不知道里面有什么，于是从不主动读。
      后果很实在 —— 卡里写了「系统设置里应用通知有两个层级，别选错」，
      跑 87 那轮 AI 完全没看，自己摸索着把「通知类别」改成静音
      （应该关应用级总开关），还判错了结论。

      本包知识卡有 `sections` 目录，AI 看到标题就知道该读哪节；兜底卡同样需要。
      19 个小节标题约 400 字符，一次性给，比 AI 摸黑试探便宜得多。

    返回 {"path", "how_to_read", "sections", "tip"}；
    卡不存在时退化成只给路径。
    """
    card = KNOWLEDGE_DIR / "_system.md"
    if not card.is_file():
        return {"path": _SYSTEM_CARD}
    try:
        sections, _top = _scan_sections(card)
    except OSError:
        return {"path": _SYSTEM_CARD}
    return {
        "path": _SYSTEM_CARD,
        "how_to_read": 'android_read_file(card="_system", query="<你的场景>")'
                       '  —— 每个 match 的 body 就是该节全文',
        "sections": sections,
        "tip": ("**跨 App 的系统行为都在这里**（权限弹窗 / 通知开关 / BACK 键 / "
                "预装应用卸载 / Toast 抓取）。命中你的场景就去读对应节，"
                "别自己摸索 —— 系统界面的层级和文案所有 App 都一样。"),
    }


def knowledge_hint(package: str | None, shown: bool = False,
                   screen: str | None = None) -> dict | None:
    """知识卡浮现：告诉 AI"这个包有卡、卡里有哪些小节"。

    ⚠️ 为什么需要（真实教训）：`assets/knowledge/` 下有真机验证过的卡，
    但**全链路没有任何地方提示卡存在**——跑 168/169 那几轮，AI 从头到尾
    没读过那张卡，把已经沉淀好的事实重新探了一遍。**已有资产完全没被用上。**

    设计取舍：
    - **只给标题、不给行号**：卡片会持续编辑，行号是易腐数据；
      AI 拿到标题再 `grep -n` 定位即可（两步都很便宜）。
    - **只提示一次**（shown=True 时直接返回 None）：索引本身也占 token，
      每步都带会把前面压 nodes 省下的又还回去。

    `screen` 传当前前台 activity 时，会把与当前屏幕相关的小节**置顶到
    `sections_relevant`**（见 _sections_matching），并在 `cross_cards` 里
    指出"别家卡里也写了本界面相关内容"——针对 2026-10-14 那次
    "知识在日历卡、界面却属于 launcher，按包找卡找错"的真实教训。

    返回 None 表示：无包名 / 无卡 / 已提示过。
    """
    if not package or shown:
        return None
    card = KNOWLEDGE_DIR / f"{package}.md"
    path_map = PATHS_DIR / f"{package}.md"
    # 路径图节点（落脚点），有就一并给出 —— 它才是"先定位，再找出边"的入口
    path_nodes: list[str] = []
    # 这些小节不是落脚点，是路径图的辅助段，别混进节点索引
    _NOT_NODES = ("节点索引", "自动采集的边", "说明", "索引", "同 rid 多态")
    if path_map.is_file():
        try:
            for line in path_map.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    name = line[3:].strip()
                    if name and not any(k in name for k in _NOT_NODES):
                        path_nodes.append(name)
        except OSError:
            pass
    if not card.is_file() and not path_nodes:
        return {
            "card": None,
            "system_card": _system_card_hint(),
            "tip": ("本包暂无专属知识卡。系统界面（权限弹窗/系统对话框/预装应用"
                    "卸载恢复/通知开关）看 system_card"),
        }
    sections: list[str] = []
    if card.is_file():
        sections, _top = _scan_sections(card)

    # 跨卡指路：本包卡的正文里提到别的包，说明"那些包的内容也写在这里"
    cross: dict[str, str] = {}
    if card.is_file():
        for other in set(_card_keywords(card)):
            if other == package:
                continue
            other_card = KNOWLEDGE_DIR / f"{other}.md"
            if other_card.is_file():
                cross[other] = f"assets/knowledge/{other}.md"

    out: dict = {
        # 直接给「怎么调」，不给文件路径：给路径的话 AI 会用
        # `android_read_file(path=...)` 整份读卡（一轮读十几次，两卡各读好几遍）；
        # 而 `card=<包名>` 能一次搜两卡、还能按节取，快得多。
        # 所以字段名是 `package` + 两个现成的调用式，别让 AI 自己把路径反推成参数。
        "package": package if card.is_file() else None,
        "how_to_read": (
            f'android_read_file(card="{package}", query="<这条用例要干什么>")'
            '  —— 一次同时搜知识卡+路径卡，每个 match 的 body 就是该节全文，'
            '看完直接用，别把同一节再读一遍'
            if card.is_file() else None),
        "how_to_read_section": (
            f'android_read_file(card="{package}", section=["A", "B"])'
            '  —— 按节读，section 是数组，一次读完所有要看的节'
            if card.is_file() else None),
        "sections": sections,
        "tip": ("**执行前先定位**：路径图是节点图（节点=落脚点，边=认什么锚点→去哪）。"
                "先认自己在哪个节点（看 activity + 屏幕上有什么），再检索该节点的出边。"
                "知识卡是客观事实（什么情况下会发生什么、怎么运作、怎么算符合预期），"
                "按交互类型词检索命中标题，只读对应小节。"
                "**用 card= 参数读，不要用 path= 整份读。**"),
        "system_card": _system_card_hint(),
    }
    relevant = _sections_matching(sections, screen)
    if relevant:
        out["sections_relevant"] = relevant
        out["sections_relevant_tip"] = (
            "以上小节标题与当前屏幕关键词匹配，**优先读这几节**（其它小节仍可能有坑）。")
    if cross:
        out["cross_cards"] = cross
        out["cross_cards_tip"] = (
            "本包卡片正文引用了这些包的内容 —— 若当前界面属于它们，"
            "对应知识可能就写在本包里，别只按被测包名找卡。")
    if path_nodes:
        out["path_map"] = f"assets/knowledge/paths/{package}.md"
        # 字段名直说用途：这是"节点索引"，供先定位、再 grep 对应节
        out["nodes"] = path_nodes
    return out


def case_spec_hint(case_id: int | None) -> dict | None:
    """告诉 AI"这条用例有一份写好的执行规格"，并给出摘要。

    ⚠️ 为什么需要（2026-10-15 真实缺口）：用例规格（`assets/cases/<应用>/*.yaml`）
    沉淀了首轮踩到的**全部坑**与每步的**入口判据**，但 `session start` 的返回里
    **一个字都没提到它** —— 实测 `assets/cases` 出现次数为 0。于是复跑时
    AI 只能从零重探，第一遍踩过的坑**原封不动再踩一遍**，规格等于白写。
    这与当年"知识卡存在却全链路无提示"是同一类失效：**有资产，但没有交付面**。

    ⚠️ 为什么按 `meta.case_id` 反查、而不是拼文件名：
    DB 里的用例标题与 YAML 文件名**并不一致**，实测两例：
      标题「联想日历_182_新建编辑课程」 vs 文件「联想日历_182_手动创建课程表-新建编辑课程.yaml」
      标题「日历其他用例_26」         vs 文件「日历_26_每个时间段最大10条.yaml」
    拼路径会静默找不到，反查才可靠。

    ⚠️ 为什么只给摘要、不给全文：YAML 可能上千行，全塞进 start 的返回会
    挤掉本该用于观察的上下文。给**路径 + 步骤清单 + 复跑判据**，
    AI 需要哪步的细节再自己去读那一节。

    返回 None 表示：无 case_id / 该用例没有 YAML / 文件损坏 —— 都属正常
    （实测 62 条用例里只有 8 条有规格），**调用方不该因此报错**。
    """
    if not case_id:
        return None
    # 用例按应用分目录放在 assets/cases/<包名>/ 下，必须**递归** glob：
    # 平铺 glob("*.yaml") 一条都找不到。
    from runtime import CASES_DIR as _CASES
    cases_dir = _CASES
    if not cases_dir.is_dir():
        return None
    try:
        import yaml
    except ImportError:
        return None
    for p in sorted(cases_dir.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") or {}
        if meta.get("case_id") != case_id:
            continue
        steps = doc.get("steps") or []
        index = []
        for s in steps:
            pf = s.get("pitfalls") or []
            exp = s.get("expect") or {}
            # 位置判据：去掉纯说明性的 note，剩下的才是"拿屏幕能比对"的
            crit = {k: v for k, v in exp.items() if k != "note"}
            index.append({
                "step": s.get("id"),
                "desc": (s.get("desc") or "").strip(),
                "pitfalls": len(pf),
                "landing": crit,
            })
        md = p.with_suffix(".md")
        df = meta.get("derived_from") or {}
        # 前置条件按**能不能动手改**分成三类（2026-10-15 定）。
        # 为什么必须分：原先笼统一个 preconditions 列表，AI 分不清该"动手做"
        # 还是该"直接 BLOCKED"——同一份 YAML 里两条前置会得出相反判断
        # （「存在课程表」动手建了，「周视图有空白格」却不敢动而卡住）。
        pre_raw = doc.get("preconditions") or []
        pre = {"clean": [], "goto": [], "prepare": [], "assert": []}
        for x in pre_raw:
            if not isinstance(x, dict):
                continue
            t = (x.get("type") or "").strip()
            if t == "goto":
                pre["goto"].append({"desc": x.get("desc"),
                                    "target": x.get("target"),
                                    "path": x.get("path"),
                                    "skip_if": x.get("skip_if")})
            elif t == "clean":
                pre["clean"].append({"desc": x.get("desc"),
                                     "do": x.get("do"),
                                     "skip_if": x.get("skip_if"),
                                     "on_fail": x.get("on_fail")})
            elif t in ("prepare", "assert"):
                pre[t].append({"desc": x.get("desc"),
                               "do": x.get("do"),
                               "check": x.get("check"),
                               "skip_if": x.get("skip_if"),
                               "on_fail": x.get("on_fail")})
            else:
                # 没标 type 的旧条目：当作 assert（保守，不擅自动手）
                pre["assert"].append({"desc": x.get("desc"),
                                      "check": x.get("check"),
                                      "on_fail": x.get("on_fail")})
        # 路径要相对 ROOT 报出来，AI 拿着它就能直接读
        try:
            rel_yaml = p.relative_to(ROOT).as_posix()
            rel_md = md.relative_to(ROOT).as_posix() if md.is_file() else None
        except ValueError:
            rel_yaml = p.as_posix()
            rel_md = md.as_posix() if md.is_file() else None
        return {
            "yaml": rel_yaml,
            "md": rel_md,
            "title": meta.get("title"),
            "steps": len(steps),
            "step_index": index,
            "preconditions": pre,
            "last": {"session": df.get("session"), "status": df.get("status")},
            "tip": (
                "**复跑先读这份规格**：它由上一轮实测沉淀而来，"
                "每步都写了『动手前屏上该有什么』与**那一步专属的坑**。"
                "别从零重探 —— 首轮踩过的坑都在这里。"
                "动手前先 `Read` 这个 YAML（或渲染好的 MD，更易读），"
                "走到哪步就只看那步的 `pitfalls`。"),
            "start_tip": (
                "**起点固定，按前置顺序走到起点再顺序执行**：会话已冷启动复位，"
                "所以起点一定是 App 首页。处理顺序："
                "`preconditions.clean`（**用例要求清数据时**用 `pm clear` 回首次使用态，"
                "清完首次打开会有授权流程——顺着走完即可，不用特殊处理）→ "
                "`preconditions.goto`（怎么从首页走到起点）→ "
                "`preconditions.prepare`（要动手准备的状态，**已有则跳过**）→ "
                "`preconditions.assert`（只能检查、不满足才 BLOCKED）。"
                "⚠️ **数据前提先自己恢复**（要干净就 `pm clear`，要某个东西就造出来），"
                "不要因为『用例没要求』就停下；造不出来（试过 2 次）才算 BLOCKED。"
                "准备完再按 `step_index` 顺序执行 s1→s2→…。"),
        }
    return None


_CASE_NUM_RE = re.compile(r"[_\-](\d{2,4})(?!\d)")


def case_stems(package: str | None) -> list[str]:
    """`assets/cases/<包名>/` 下所有用例 md 的文件名（不含 `.md`）。"""
    if not package:
        return []
    d = CASES_DIR / package
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def _stem_num(stem: str) -> str | None:
    m = _CASE_NUM_RE.search(stem)
    return m.group(1) if m else None


def _stem_module(stem: str) -> str:
    """`com.zui.calendar_重要日倒数_03` → `重要日倒数`"""
    m = _CASE_NUM_RE.search(stem)
    return stem[: m.start()].rstrip("_-") if m else ""


def _loose(s: str) -> str:
    """去掉数字、标点、空白，只留可比的汉字/字母 —— 用于「模块名像不像」判断。"""
    return re.sub(r"[\s\-_/（）()【】\[\]0-9A-Za-z.]+", "", s)


def resolve_case_stem(package: str | None, title: str | None,
                      user_input: str | None = None) -> str | None:
    """把会话的「标题 + 用例原文」解析成**用例 md 的文件名**（不含 `.md`）。

    steps 文件名一律是 `<md文件名>_steps.yaml`，所以必须先知道 md 叫什么。
    编号不唯一（实测「启动应用_03」和「重要日倒数_03」并存），只有带上模块名
    的文件名才天然不撞 —— 写入（`session.py:export_steps`）和读取
    （`steps_hint`）都走这里，各写一套正则必然打架。

    解析顺序（在 372 条真实会话上验证，命中 370 条）：
      ① 用例原文首行 `# xxx` 且 `xxx.md` 存在 —— 最可靠，直接就是文件名
      ② 标题**以某个 md 文件名开头** —— 派发时原样抄的文件名会是这样
      ③ 标题里的编号在目录里唯一 —— 多数情况
      ④ 编号有多个候选时，用模块名匹配（标题里含 md 的模块名）
    都不中返回 None（调用方按"没这条用例"处理，别报错）。
    """
    stems = case_stems(package)
    if not stems:
        return None
    pool = set(stems)
    # ① 原文首行
    if user_input:
        first = (user_input.lstrip("\ufeff").splitlines() or [""])[0].strip()
        if first.startswith("#"):
            cand = first.lstrip("#").strip()
            if cand in pool:
                return cand
    if not title:
        return None
    # ② 标题前缀
    hits = [s for s in stems if title.startswith(s)]
    if hits:
        return max(hits, key=len)
    # ③ 编号唯一
    m = _CASE_NUM_RE.search(title) or re.search(r"(\d{2,4})", title)
    if not m:
        return None
    num = m.group(1)
    cands = [s for s in stems if _stem_num(s) == num]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    # ④ 模块名消歧
    tn = _loose(title)
    hit = [s for s in cands
           if (mod := _stem_module(s)) and _loose(mod) in tn]
    return hit[0] if len(hit) == 1 else None


def steps_hint(package: str | None, title: str | None,
               user_input: str | None = None) -> dict | None:
    """告诉 AI「这条用例上次走过的跳转步骤」，给它参考。

    ## 为什么需要

    每次跑同一条用例，AI 都要重新摸「更多按钮在哪、菜单里哪个是课程表」。
    这些是纯机械的跳转，`session finish` 已经自动导出到
    `assets/cases/<包名>/<md文件名>_steps.yaml`，但**开场没人告诉 AI 它存在**。
    （文件名与用例 md 同名，见 `resolve_case_stem` 的说明。）

    ## 只作参考，不自动执行

    返回的是「上一次的实测路径」，不是唯一正确路径：
      · 前置不同 → 起点可能不一样
      · 版本变化 → 某步可能失效

    所以只给清单 + 一句提醒，让 AI 自己决定照不照着走。
    **它要是照着点失败了，会自己接管** —— 这比代码硬走安全。

    ## 返回 None 的情况

    没有包名 / 没标题 / 还没跑过这条用例 / 文件坏了 —— 都正常，调用方别报错。
    """
    if not package or not title:
        return None
    stem = resolve_case_stem(package, title, user_input)
    if not stem:
        return None
    f = CASES_DIR / package / f"{stem}_steps.yaml"
    if not f.is_file():
        return None
    try:
        raw = f.read_text(encoding="utf-8")
    except OSError:
        return None

    # 轻量解析：不引 yaml（这个文件格式是我们自己写的，很规整）
    steps: list[dict] = []
    meta: dict[str, str] = {}
    cur: dict | None = None
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- n:"):
            if cur:
                steps.append(cur)
            cur = {"n": s.split(":", 1)[1].strip()}
            continue
        if cur is not None and ":" in s:
            k, v = s.split(":", 1)
            cur[k.strip()] = v.strip()
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            meta[k.strip()] = v.strip()
    if cur:
        steps.append(cur)
    if not steps:
        return None

    # 只给前 8 步 —— 够看清路线，又不挤占上下文
    brief = []
    for st in steps[:8]:
        item = {"via": st.get("via"), "expect": st.get("expect")}
        if st.get("in_page", "").lower() == "true":
            item["in_page"] = True
        brief.append(item)

    return {
        "file": f"assets/cases/{package}/{f.name}",
        "last_run": meta.get("last_run"),
        "verdict": meta.get("verdict"),
        "step_count": meta.get("step_count") or len(steps),
        # 起点条件：第 1 步之前得在哪个页面。不给这个字段，AI 拿到 8 步
        # 却不知道"这 8 步要求你先在日历首页"，起点不对时可能点错地方还不自知。
        "start_from": meta.get("start_from"),
        "steps": brief,
        "truncated": len(steps) > 8,
        "tip": ("这是**上一次**的实测跳转路径，可照着走能省找控件的时间。"
                "但它不是唯一正确路径：**先看 start_from，当前不在那个页面就先自己走到那儿**；"
                "版本变化某步也可能失效。照着点失败就自己接管，不要硬走。"
                "完整内容用 android_read_file 读 file。"),
    }


def auto_name() -> str:
    """证据名自动派生：<会话id>/step<N>——探索时不该逼 AI 先想命名。

    计数器自增用 O_EXCL 锁文件保护（读改写非原子，并行调用会拿到同一个
    step 号互相覆盖证据）；抢不到锁短暂重试，超时降级为直读（宁重号不断功能）。

    计数器**按会话重置**（`storage/.evidence_seq_<sid>`）：每个会话从 1 开始，
    命名形如 `step01 / step02 / step03 …`，和 AI 手动命名的风格一致。
    「一次运行只有一个会话」是常态，所以这不影响并发安全。
    """
    import os
    try:
        from db import current_session
        sid = current_session()
    except Exception:  # noqa: BLE001
        sid = None
    tag = str(sid) if sid else "loose"
    seq_file = STORAGE_DIR / f".evidence_seq_{tag}"
    lock = str(seq_file) + ".lock"
    seq_file.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    try:
        n = int(seq_file.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    fd = None
    for _ in range(20):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.01)
        except OSError:
            break
    try:
        if fd is not None:
            os.close(fd)
            try:
                n = int(seq_file.read_text().strip()) + 1
            except (OSError, ValueError):
                pass
        # ⚠️ 降级路径也必须写入：抢不到锁时若跳过写入，下次调用会读到同一个 n，
        # 证据互相覆盖——而加锁本来就是为了防这个（真实缺陷）。
        # 宁可有极小概率重号，也不能必然重号。
        seq_file.write_text(str(n), encoding="utf-8")
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                os.unlink(lock)
            except OSError:
                pass
    # 2 位补零：step01 / step02 …… step99；超过 99 自然变成 step100。
    # 必须和 AI 自己传的 --name（`case168/step01`）同一风格：
    # 两种位数混在一个目录里按字符排序会错乱（"step0001" < "step00"）。
    return f"{sid}/step{n:02d}"


def evidence_paths(base_dir: str | None, name: str | None) -> tuple[Path, Path, Path]:
    """证据落盘路径 (shot.png, dump.xml, meta.json)。

    ## 默认落点：本次运行的目录，不是全局大锅饭

    有当前运行目录时落 `run_history/<uuid>/evidence/<name>/` —— 删一次运行
    就等于删一个 uuid 目录。落在全局 `storage/evidence/<name>/` 的话，
    证据没有归属（多轮运行叠加、想删某次无从下手、只增不减）。

    没有运行目录（工具被单独调用、没开会话）时退到
    `storage/evidence/<name>/`，保证工具可独立使用。

    ## name 由调用方给

      传了就按传的；没传走 auto_name()，生成 `<session_id>/stepNNNN`。
    """
    if not name:
        name = auto_name()
    if base_dir:
        d = Path(base_dir)
    else:
        run = current_record_dir()
        d = (run / "evidence") if run else DEFAULT_EVIDENCE_DIR
    d = d / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "shot.png", d / "dump.xml", d / "meta.json"


def log_auto(kind: str, tool: str, detail: str, data: dict | None = None,
             evidence: str | None = None, duration_ms: int | None = None,
             started_at: str | None = None) -> int | None:
    """Persist an event or explicitly stop; an executed action must not be replayed."""
    sid = None
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            with stage("event_write"):
                eid = get_db().log_event(
                    sid, kind, tool, detail,
                    {**(data or {}), "timing": timing_snapshot()},
                    evidence, duration_ms, started_at)
            if eid is None:
                raise RuntimeError("Event writer returned no event ID")
            EVENT_IDS.append(eid)
            return eid
        return None
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        payload = {
            "recording_failed": True, "call_id": CALL_ID, "session_id": sid,
            "stage": "event_write", "error": str(error),
            "action_may_have_executed": runtime.EXECUTION_STATE != "not_executed",
            "evidence": evidence,
            "hint": "Do not replay actions. Preserve evidence and resolve incomplete recording.",
        }
        if sid is not None:
            try:
                directory = STORAGE_DIR / "recording_errors" / str(sid)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{CALL_ID}.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except OSError as marker_error:
                payload["marker_error"] = str(marker_error)
        print(f"Recording failed: {error}", file=sys.stderr)
        emit(False, payload)
        raise SystemExit(1)


def backfill_package(package: str | None) -> None:
    """首个 observe 回填当前会话的被测包名。"""
    if not package:
        return
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            get_db().set_package(sid, package)
    except Exception:
        pass


def _rotation(d: "u2.Device", shot=None) -> int:
    """屏幕旋转角（0/1/2/3），跨 uiautomator2 版本稳健取法。

    ⚠️ uiautomator2 3.x 移除了 Device.rotation（AttributeError 会让每次
    observe/act 直接失败）；Device.info 在 Android 17 上又会抛
    ApplicationSharedMemory not initialized。故此处三级降级，
    最后用截图长宽比推断（横屏=1），拿不到就返回 -1 而不是抛异常——
    取证不该因为一个展示字段而整条链路失败。
    """
    for attr in ("rotation", "getRotation"):
        try:
            v = getattr(d, attr)
            v = v() if callable(v) else v
            if isinstance(v, int) and 0 <= v <= 3:
                return v
        except Exception:  # noqa: BLE001
            pass
    try:
        w, h = d.window_size()
        if w and h:
            return 1 if w > h else 0
    except Exception:  # noqa: BLE001
        pass
    if shot is not None:
        try:
            return 1 if shot.size[0] > shot.size[1] else 0
        except Exception:  # noqa: BLE001
            pass
    return -1


def _dump_with_timeout(d, timeout: float) -> str:
    """在**独立线程**里跑 dump_hierarchy，超时就抛。

    ⚠️ 为什么必须从外面再包一层（langgraph-poc 已实测，2026-10-31 移植）：

      uiautomator2 内部是**无限重试**：
        _do_dump_hierarchy (uiautomator2/__init__.py:136)
        __retry_internal (retry/api.py:35)          ← 无上限
        dump_hierarchy (uiautomator2/__init__.py:120)

      jsonrpc 自己有 HTTP 超时，但 retry 装饰器把超时当"可重试"继续打，
      所以**在调用内部加超时没用**，只能在最外面加一道硬墙。

      实测卡死：用例跑到 s3 的第一次观察就停住 —— 进程活着、设备停在原页不动、
      日志不再增长，看起来像"一直在点完成"，实际是**一次都没点**，全卡在观察里。

    用 daemon 线程 + join(timeout)：超时后线程被丢弃（daemon 不阻塞退出），
    主线程抛 TimeoutError 交给上层处理，进程不会挂死。
    """
    import threading
    result: dict = {}

    def work() -> None:
        try:
            result["xml"] = d.dump_hierarchy()
        except BaseException as exc:  # noqa: BLE001
            result["err"] = exc

    th = threading.Thread(target=work, daemon=True, name="dump_hierarchy")
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(
            f"dump_hierarchy 超过 {timeout:.0f}s 未返回（uiautomator2 在无限重试）")
    if "err" in result:
        raise result["err"]
    return str(result.get("xml") or "")


def _dump_hierarchy(d, xml: Path, timeout: float = 20.0) -> tuple[str, int]:
    """dump 一次 UI 树；撞上 uiautomator 会话冲突时清理残留并重试一次。

    ⚠️ 真实教训（2026-10-10 下午，连续 6 条会话作废）：
    `uiautomator dump` 抛出
        IllegalStateException: UiAutomationService ... already registered!
        at com.android.uiautomator.core.UiAutomationShellWrapper.connect
        at com.android.commands.uiautomator.DumpCommand.run
    —— 上一位 dump 进程没释放 automation 会话，新的又去注册，直接撞死。
    崩溃主体是 **uiautomator 自己**（shell 起的进程，所以 crash buffer 里
    没有 `Process:` 行），**不是被测应用崩溃**。但工具层把它当成了环境故障，
    整条会话按 ERROR 收尾，一天内因此废掉 6 条。

    处置：这是**只读观测**，失败时设备状态没被改变，重试不会有副作用
    （不同于 tap/input —— 那些永不重放）。故：残留进程清掉 → 重 dump 一次。

    清残留只动 uiautomator 进程，不 kill-server、不碰应用进程。

    ⚠️ 另加**超时硬墙**（见 _dump_with_timeout）：u2 内部无限重试会卡死会话。
    超时不重试 —— 卡 20 秒还没结果说明 u2 已经在死循环里，再试只会更慢，
    直接抛给上层按"观测失败"处理。
    """
    attempts = 2
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _dump_with_timeout(d, timeout), attempt
        except TimeoutError:
            raise                      # 超时不重试，直接抛
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt == attempts:
                break
            # 这里不区分异常类型，一律清理+重试一次：dump 是只读观测，
            # 多试一次无害；断连/超时类即使救不回来，也只是多花约 1 秒。
            with stage("dump_retry"):
                # 清掉可能残留的 uiautomator / automation 观测进程
                _adb("shell", "pkill", "-f", "com.android.commands.uiautomator")
                _adb("shell", "pkill", "-f", "com.github.uiautomator")
                time.sleep(1.0)
    raise last_error if last_error else RuntimeError("dump_hierarchy failed")


_TOP_ACTIVITY_RE = r"ActivityRecord\{[^}]*\s+u\d+\s+([\w.]+)/([\w.$]+)[^}]*\}"


def foreground_of(d) -> dict:
    """当前前台应用 {package, activity}。**不用 u2 的 app_current。**

    ## 两个必须绕开它的理由（langgraph-poc 已实测，2026-10-31 移植）

    ### ① 页面过渡期它会返回**旧页面**

      176 的 s5：点周视图空格子，界面 1.1 秒后才跳到 EditCourseActivity，
      而点击后立刻读 app_current 拿到的还是 TimetableActivity ——
      记账 changed=False、模型以为没反应、换格子再点，
      **连点 15 格全判无效**，最后 BLOCKED。同一坐标手动点能进。

      26/46 那条 235 个事件的运行也是这个形态：
        「第2次点新建（第1次未跳转）」「重建日程事件（上次未保存）」
      —— 全是「读前台读早了 → 以为没跳转 → 重做」。

    ### ② 本机型它慢 43 倍

      `dumpsys window windows` 在本 ROM **没有 mCurrentFocus 输出**，
      于是 adbutils 退到 `dumpsys activity top`（很啰嗦，一次要好几秒）。
      实测：u2.app_current 5388ms，下面这条 125ms。

    ## 三级回退（和 AiAgentTest 一致）

      ① `topResumedActivity=ActivityRecord{... u0 pkg/.Act ...}`  最精准
      ② `mResumedActivity: ActivityRecord{...}`                  老 ROM
      ③ `ResumedActivity: ActivityRecord{...}`                   定制 ROM

    activity 统一补全成全名（dumpsys 里常写 `.foo.BarActivity` 省略包名）。

    拿不到返回 {}，让上层按"不知道当前在哪"处理，而不是停摆。
    """
    import re as _re
    try:
        out = d.shell("dumpsys activity activities")
    except Exception:  # noqa: BLE001
        return {}
    # ⚠️ u2 的 shell() 返回 ShellResponse 对象，不是 str。
    #    只判 isinstance(out, str) 会永远 False → 静默返回 {} → 表现为"读不到前台"，
    #    比报错更难查。两种形态都收。
    text = getattr(out, "output", None)
    if text is None and isinstance(out, str):
        text = out
    if not isinstance(text, str):
        return {}
    for pat in (r"topResumedActivity=" + _TOP_ACTIVITY_RE,
                r"mResumedActivity:\s*" + _TOP_ACTIVITY_RE,
                r"ResumedActivity:\s*" + _TOP_ACTIVITY_RE):
        m = _re.search(pat, text)
        if not m:
            continue
        pkg, act = m.group(1), m.group(2)
        if act.startswith("."):
            act = pkg + act
        elif "." not in act:
            act = f"{pkg}.{act}"
        return {"package": pkg, "activity": act}
    return {}


def capture(d: "u2.Device", png: Path, xml: Path, meta: Path,
            extra: dict | None = None,
            event: dict | None = None) -> dict:
    """截图 + dump + 前台状态，落盘并返回摘要 dict。

    event={"kind","tool","detail","duration_ms"} 时自动写入当前会话事件流。
    """
    with stage("screenshot"):
        shot = d.screenshot()
    with stage("png_save"):
        shot.save(png)
    with stage("ui_dump"):
        xml_text, dump_attempts = _dump_hierarchy(d, xml)
    with stage("rotation"):
        rotation = _rotation(d, shot)
    with stage("xml_save"):
        xml.write_text(xml_text, encoding="utf-8")
    with stage("foreground"):
        # 走 dumpsys，不走 u2.app_current —— 见 foreground_of 的说明
        cur = foreground_of(d)
    info = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "package": cur.get("package"),
        "activity": cur.get("activity"),
        "rotation": rotation,
        "size": list(shot.size),
    }
    if dump_attempts > 1:
        info["dump_attempts"] = dump_attempts  # 留痕：这次是清理残留后救回来的
    if extra:
        info.update(extra)
    info["call_id"] = CALL_ID
    info["timing"] = timing_snapshot()
    with stage("meta_save"):
        meta.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if event is not None:
        log_auto(event["kind"], event["tool"], event["detail"], info,
                 rel(png), event.get("duration_ms"), event.get("started_at"))
    return info


# ---------- dump 摘要 ----------

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@timed("screen_signature")
def npm_identity(xml_text: str, fg_package: str | None = None,
                 limit: int = 80) -> list[str]:
    """给一屏算一个**身份签名**：页面上出现过的 `kind=value` 短串集合。

    用途：事后自动整理路径边时，判断"这一步落在路径图的哪个节点"
    （路径图节点带「识别键」，形如 `rid=btnCreateManually` / `text=全部课程表`）。

    为什么存签名而不是存 nodes：签名是几十个短串（可随事件落库），
    nodes 是上百个对象（落库太占地方）。
    """
    import xml.etree.ElementTree as ET
    out: list[str] = []
    seen: set[str] = set()
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return out
    for el in root.iter("node"):
        a = el.attrib
        if fg_package and a.get("package") and a.get("package") != fg_package:
            continue
        rid = (a.get("resource-id") or "").split("/")[-1]
        if rid and f"rid={rid}" not in seen:
            seen.add(f"rid={rid}")
            out.append(f"rid={rid}")
        t = (a.get("text") or "").strip()
        if t and f"text={t}" not in seen:
            seen.add(f"text={t}")
            out.append(f"text={t}")
        d = (a.get("content-desc") or "").strip()
        if d and f"desc={d}" not in seen:
            seen.add(f"desc={d}")
            out.append(f"desc={d}")
        if len(out) >= limit:
            break
    return out


@timed("node_labels")
def labeled_nodes(xml_text: str, fg_package: str | None = None) -> list[tuple]:
    """取页面上**有文字的节点**位置：[(center, text), ...]。

    用途：给 `next_targets` 做"子节点文字冒泡" —— 可点容器自己没有文字时，
    用这些文字的位置反查它属于哪个可点节点。
    """
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return out
    for el in root.iter("node"):
        a = el.attrib
        if fg_package and a.get("package") and a.get("package") != fg_package:
            continue
        t = (a.get("text") or "").strip()
        if not t:
            continue
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        out.append(((x1 + x2) // 2, (y1 + y2) // 2, t))
    return out


@timed("next_targets")
def next_targets(nodes: list[dict], limit: int = 12,
                 labeled: list[tuple] | None = None) -> list[dict]:
    """从 nodes 里挑出"下一步最可能点的目标"，给一份**精简可点清单**。

    ⚠️ 为什么需要（真实教训，被同一个人纠正多次）：
    AI 的执行循环常变成「act → observe 拿坐标 → act → observe …」——
    但它其实**不需要**额外 observe：act 返回的 nodes 就是动作后的画面。
    多一次 observe = 工具 2s + 决策等待 10~15s，纯粹浪费。

    根因是"下一个目标是什么/在哪"要在一大坨 nodes 里翻，太费劲 →
    干脆另起一次 observe。本函数把这件事变便宜：**直接给精简可点清单**。

    兼容两种 nodes 格式：
      - nav 模式（短键 i/t/r/c/s）：**本身就是可点/可滑动集合**，全部算候选
      - full 模式（完整键 + clickable）：只取 clickable 的

    `labeled`：`[(center, text), ...]` —— 页面上的**文字节点**位置。
    可点容器常常自己没有文字（文字在子节点里，如菜单项、列表行），
    此时用**空间包含**把子节点文字认领给该可点节点（冒泡），
    否则清单里会出现一堆"没有标签只有坐标"的项，AI 仍得回去翻 nodes。
    """
    def _bubble(c) -> str:
        """找出位于该可点节点范围内的文字（取最长的一条）。"""
        if not labeled:
            return ""
        cx, cy = c[0], c[1]
        best = ""
        for lx, ly, lt in labeled:
            # 文字中心落在可点节点附近（容器通常比文字大，用宽容的邻近判定）
            if abs(lx - cx) <= 400 and abs(ly - cy) <= 80 and len(lt) > len(best):
                best = lt
        return best

    cands = []
    seen = set()
    for n in nodes:
        # nav 模式没有 clickable 字段（短键格式本身就是筛选过的），full 模式才判
        if "clickable" in n and not n.get("clickable"):
            continue
        c = n.get("center") or n.get("c")
        if not c:
            continue
        text = n.get("text") or n.get("t") or ""
        desc = n.get("desc") or ""
        rid = n.get("rid") or n.get("r") or ""
        if not text and not desc:
            text = _bubble(c)          # 容器无文字 → 从子节点冒泡
        key = (tuple(c), text, rid)
        if key in seen:
            continue
        seen.add(key)
        # 有语义的优先；三无的（纯容器/无描述图标）排后
        score = (3 if text else 0) + (2 if desc else 0) + (1 if rid else 0)
        item = {"c": list(c)}
        if text:
            item["t"] = text
        if desc:
            item["d"] = desc
        if rid:
            item["r"] = rid.split("/")[-1]
        if n.get("scrollable") or n.get("s"):
            item["s"] = True
        cands.append((score, item))
    cands.sort(key=lambda x: -x[0])
    return [c[1] for c in cands[:limit]]


# 装饰性系统界面：状态栏 / 导航栏 / 桌面 taskbar。
# 这些节点与"被测 App 的界面"无关，纯噪音（实测在某 App 页占 full 节点的 72%）。
_DECOR_PKGS = ("com.android.systemui", "com.zui.launcher", "com.android.launcher",
               "com.android.launcher3")


@timed("node_summary")
def summarize_xml(xml_text: str, max_nodes: int = 300,
                  mode: str = "nav", fg_package: str | None = None) -> list[dict]:
    """把 UI 树压成给 LLM 读的节点列表。

    两种模式（实测同一屏 3840×2560 的压缩效果）：
      mode="nav"  —— **导航模式（默认）**：只给**可点 + 可滑动**的节点，字段名用短键。
                     可滑动容器必须留（不可点但能滑，正是"该往哪滑"的线索）；
                     实测 62 节点/6826 字符 → 9 节点/509 字符（省 93%）。
                     nav 字段：i=下标 t=文字 r=rid 末段 c=中心坐标
                             s=可滑动 off=不可用 checked/selected=选中态
      mode="full" —— **检查模式**：给全部有语义的节点（clickable/scrollable/
                     有 rid/text/desc），字段名完整。断言"页面上出现了什么文字"
                     时必须用这个，否则会漏判（nav 不含不可点的文本节点）。

    过滤（fg_package 非空时启用）：**只保留前台包自己的节点**，
    丢掉其它包的（状态栏/导航栏/桌面 taskbar 等装饰）。

    ⚠️ 为什么按"前台包"过滤而不是"被测包"：被测 App 崩溃后前台会回落到桌面、
    权限弹窗宿主是 permissioncontroller、流程跳到相机/图库——这些时刻前台包
    **本来就不是被测包**，若按被测包过滤会把整个屏幕滤空，崩溃和弹窗就看不见了。
    按前台包过滤则天然兼容：屏幕上是什么，就保留什么。

    为什么要分模式 + 过滤：nodes 每步都进 LLM 上下文，导航时给全量是纯浪费。
    """
    import xml.etree.ElementTree as ET
    full = (mode == "full")
    nodes: list[dict] = []
    for el in ET.fromstring(xml_text).iter("node"):
        a = el.attrib
        rid, text, desc = a.get("resource-id", ""), a.get("text", ""), a.get("content-desc", "")
        clickable = a.get("clickable") == "true"
        scrollable = a.get("scrollable") == "true"
        if fg_package:
            pkg = a.get("package", "")
            # 只留前台包的节点；节点没标 package 时按装饰包名兜底排除
            if pkg:
                if pkg != fg_package:
                    continue
            elif any(d in rid for d in _DECOR_PKGS):
                continue
        if not full and not (clickable or scrollable):
            # 导航模式：留可点的 + 可滑动的。
            # ⚠️ 必须带 scrollable——不可点但可滑动的容器（列表/滚轮）正是
            # "该往哪滑"的线索；只留 clickable 会让 AI 在长列表里找不到滑动目标。
            continue
        if full and not (clickable or scrollable or rid or text or desc):
            continue                      # 检查模式：跳过纯容器
        m = _BOUNDS_RE.search(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

        if full:
            node = {"bounds": [x1, y1, x2, y2], "center": [cx, cy]}
            if rid:
                node["rid"] = rid
            if text:
                node["text"] = text
            if desc:
                node["desc"] = desc
            if clickable:
                node["clickable"] = True
            if scrollable:
                node["scrollable"] = True
            if a.get("enabled") == "false":
                node["enabled"] = False
            for st in ("checked", "selected"):
                if a.get(st) == "true":
                    node[st] = True
        else:
            # 短键名：i=过滤后列表下标 t=文字 r=rid 末段 c=中心坐标（i 与 dump.xml 序号无关）
            node = {"i": len(nodes), "c": [cx, cy]}
            if text:
                node["t"] = text
            if desc:
                node["t"] = (node.get("t") + " " + desc).strip() if node.get("t") else desc
            if rid:
                node["r"] = rid.split("/")[-1]
            if scrollable:
                node["s"] = True
            if a.get("enabled") == "false":
                node["off"] = True
            for st in ("checked", "selected"):
                if a.get(st) == "true":
                    node[st] = True
        nodes.append(node)
        if len(nodes) >= max_nodes:
            break
    return nodes


def add_common_args(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--serial", default=None, help="多设备时指定 adb serial")
    p.add_argument("--dir", default=None, help="证据根目录（默认 storage/evidence）")
    p.add_argument("--name", default=None,
                   help="证据名，如 case172/step01；缺省自动派生 <会话id>/step<N>")
    p.add_argument("--full", action="store_true",
                   help="nodes 给全量语义节点（检查模式；默认导航模式，只给可点/可滑动节点）")
    p.add_argument("--no-filter", action="store_true", dest="no_filter",
                   help="不过滤系统界面节点（默认只保留当前前台包的节点）")
    p.add_argument("--settle", type=int, default=500,
                   help="动作后稳定窗口毫秒数，默认 500（仅 act 用）")
    p.add_argument("--perm-action", default=None, choices=["grant", "deny"],
                   dest="perm_action",
                   help="声明权限意图（**仅 act 用**）：测「拒绝」路径时才需要，"
                        "不声明默认点同意")
    p.add_argument("--perm", default=None,
                   help="配合 --perm-action：权限类型标记（如 media_images/camera）")
    p.add_argument("--expect", default=None, dest="expect",
                   help="检查点（**仅 act 用**）：动作后校验落点。"
                        "形如 activity=EditTimetableActivity 或 rid=et_schedule_name，"
                        "逗号=AND、竖线=任一。由工具判", )
    return p


def add_common_args_all_subcommands(p: argparse.ArgumentParser) -> None:
    """把全部公共参数也挂到每个子命令上（须在 add_subparsers 之后调）。

    ⚠️ 背景：argparse 的父级参数**只能写在子命令之前**，于是
    `act.py tap --serial X` 报 unrecognized arguments，而
    `observe.py --serial X` 却正常——同一份 SKILL.md 说"多设备加 --serial"，
    两个工具的正确写法却不同，AI 必然踩（真实事故）。
    这里给每个子命令再挂一份同名参数，两种位置都能解析。

    ⚠️⚠️ 关键坑（踩过，务必保留 SUPPRESS）：
    argparse 解析时**父子同名 dest 会互相覆盖**——子解析器先以默认值写入
    namespace，父级再解析时（若参数写在前面）又被子命令的默认值盖回去，
    结果是"命令不报错、但值丢了"。实测 `--perm-action grant` 写在子命令前时
    解析结果为 None，意图静默失效。
    解法：子命令上的同名参数一律 `default=argparse.SUPPRESS`——
    这样**只有显式传了该 flag 才会写入 namespace**，没传就不碰父级的值。
    """
    specs = [
        ("--serial", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--dir", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--name", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--full", {"action": "store_true", "default": argparse.SUPPRESS,
                    "help": argparse.SUPPRESS}),
        ("--no-filter", {"action": "store_true", "dest": "no_filter",
                         "default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--settle", {"type": int, "default": argparse.SUPPRESS,
                      "help": argparse.SUPPRESS}),
        ("--perm-action", {"default": argparse.SUPPRESS,
                           "choices": ["grant", "deny"],
                           "dest": "perm_action", "help": argparse.SUPPRESS}),
        ("--perm", {"default": argparse.SUPPRESS, "help": argparse.SUPPRESS}),
        ("--expect", {"default": argparse.SUPPRESS, "dest": "expect",
                      "help": argparse.SUPPRESS}),
    ]
    subparsers = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
    for sp in subparsers:
        for choice in sp.choices.values():
            existing = getattr(choice, "_option_string_actions", {})
            for flag, kw in specs:
                if flag not in existing:
                    choice.add_argument(flag, **kw)


def emit(ok: bool, payload: dict) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
    except AttributeError:
        pass
    if ok:
        runtime.execution("executed")
    result = {"ok": ok, **payload, "call_id": CALL_ID,
              "event_ids": list(EVENT_IDS), "execution_state": runtime.EXECUTION_STATE}
    with stage("serialization"):
        # **紧凑输出，不要 indent=2**：缩进会占掉近一半体积（实测一条 act 返回
        # 2,739 字符里 1,335 是空格）。AI 读紧凑 JSON 没问题，人要看时
        # 有 meta.json（仍 indent=2）和 webui 页面两条路。
        text = json.dumps(result, ensure_ascii=False,
                          separators=(",", ":"))
    timing = {**timing_snapshot(), "response_bytes": len((text + "\n").encode("utf-8"))}
    timing_file = os.environ.get("ANDROID_TEST_TIMING_FILE")
    if timing_file:
        try:
            path = Path(timing_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(timing), encoding="utf-8")
        except OSError as error:
            print(text)
            print(json.dumps({"audit_failed": True, "call_id": CALL_ID,
                              "stage": "timing_write", "error": str(error)}), file=sys.stderr)
            raise SystemExit(1)
    if os.environ.get("ANDROID_TEST_VERBOSE_TIMING") == "1":
        result["timing"] = timing
        text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)


def fail(msg: str, *, rejected: bool = False) -> None:
    emit(False, {"error": msg, **({"rejected": True} if rejected else {})})
    sys.exit(1)


def guard_terminated(tool: str) -> None:
    """硬性拦截：会话已因崩溃终止时，拒绝执行任何观测/动作。

    ⚠️ 用户明确要求："发现 Crash/ANR 就直接停止这个 case 的执行，不要做多余的事"。
    只发 `terminate` 信号靠 AI 自觉是不够的——AI 可能仍去截图/点击，
    产生不可信数据和无谓动作。故在工具入口硬拦。

    放行方式：session.py resume --id <N> --note "<为什么还能继续>"
    （人工确认是偶发、或已恢复前置后才用）。未开会话时不拦。
    """
    try:
        runtime.execution("not_executed")
        from db import current_session, get_db
        sid = current_session()
        if not sid:
            return
        if recording_issues(STORAGE_DIR, sid):
            fail(f"Session {sid} has incomplete recording. Resolve it and finish ERROR/BLOCKED; do not replay actions.", rejected=True)
        reason = get_db().get_termination(sid)
    except (OSError, ValueError, sqlite3.Error) as error:
        fail(f"Cannot verify session guard: {error}")
    if not reason:
        return
    emit(False, {
        "terminated": True,
        "rejected": True,
        "session_id": sid,
        "error": f"会话 #{sid} 已终止，拒绝执行 {tool}",
        "reason": reason,
        "hint": ("被测/关联包已崩溃/ANR —— 本 case 到此结束，不要做多余的事。"
                 "请记 BLOCKED finding 并 finish --status BLOCKED 收尾，"
                 "然后继续下一条用例。"),
    })
    sys.exit(3)  # 独立退出码，便于调用方区分"被拦截"与"执行失败"


def settle(sec: float = 0.6) -> None:
    """动作后给画面一个极短的稳定窗口。这是工具层唯一的"等待"，固定且极短。"""
    time.sleep(sec)

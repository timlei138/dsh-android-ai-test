"""用例执行的 JSON 入口 —— 给 DSH 工具插件（android_case）调用。

为什么单独开一个薄 CLI：
  · Node 插件不该懂 Python，也不该解析自由文本 —— 读 stdin 的 JSON、写 stdout 的 JSON
  · subagent.run_case() 是库接口，进度日志走 stderr，**报告走 stdout**，
    两者分开，插件才能稳定地 JSON.parse(stdout)
  · 用例数据由插件带过来（主 Agent 拆解的），不再需要 task_168.json 这种硬编码文件

用法（插件侧）：
    echo '<case json>' | python run_case_json.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
# 包在 src/ 下，不在仓库根 —— 加 src 而不是仓库根
_SRC = _HERE / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 进度日志写 stderr，stdout 只留最终 JSON —— 否则插件解析不了
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# ★ 先确保环境齐备，**再**导入业务模块。
#
# 顺序不能反：导入 android_agent 会连带 import langgraph/adbutils，
# 缺任何一个都会在 import 阶段直接炸 —— 那时还没走到这里，
# 报出来的是一句含糊的 ModuleNotFoundError，看不出"该装什么"。
#
# 所以第一跑（新机器 / 换了 Python 版本）会自动装依赖：
# 缺什么装什么，全都齐了就是一次 0.2 秒的 import 试探，几乎无开销。
#
# 装不上时不在这里退出 —— 交给后面的正常流程报错，保持单一失败路径。
try:
    import setup_env as _setup_env
    _setup_env.ensure()
except Exception as _e:  # noqa: BLE001
    print(f"⚠️ 环境检查没能完成：{_e}", file=sys.stderr)

from android_agent.agent.runner import CaseSpec, run_case  # noqa: E402

# 直接跑本脚本（不经插件）时的知识卡根目录
from android_agent.cases.paths import skill_root  # noqa: E402
DEFAULT_SKILL = skill_root()

# ★ 每次启动顺手清一次 tmp/ 里超过 24 小时的临时文件。
#
# 为什么放在这里而不是后台定时任务：本项目没有常驻进程，
# 每次跑用例就是一次"自然的心跳" —— 跑完顺手清，最省事也最可预期。
#
# 清理失败**绝不能影响跑用例**：吞掉所有异常。清不掉旧文件只是磁盘脏，
# 但要是因此让用例跑不起来，就是拿小问题换大问题。
try:
    from android_agent.report.scratch import clean_tmp as _clean_tmp
    _clean_tmp(max_age_hours=24)
except Exception:  # noqa: BLE001
    pass


def _pick_device() -> str | None:
    """环境变量优先，否则用唯一在线且**u2 真能连上**的设备。

    ★ 不再 `adb devices`。

    规矩：设备操作一律走 uiautomator2，只有 adb logcat 例外。
    何况 `adb devices` 列得出设备**不代表 u2 连得上** —— 授权了但
    uiautomator 服务没起来很常见。这里选出来的设备要交给 DirectDevice 用，
    那就该用同一条通道验它真能用。

    ## 怎么枚举

    `uiautomator2` 没有公开的 list_devices，枚举这件事交给 adbutils
    的 `adbd.list()`。⚠️ 这一步**只读设备列表，不执行任何设备操作**，
    拿到 serial 之后一律 `u2.connect(serial)` 用 u2 去验。
    """
    env = os.environ.get("ANDROID_SERIAL")
    if env:
        return env

    serials: list[str] = []
    try:
        import adbutils

        for info in adbutils.adb.list():
            s = getattr(info, "serial", None) or str(info)
            if not s or s == "None":
                continue
            # 这些是状态字，不是设备
            if s in ("adb", "devices", "List", "of", "attached"):
                continue
            serials.append(s)
    except Exception:  # noqa: BLE001
        return None

    usable: list[str] = []
    for s in serials:
        try:
            import uiautomator2 as u2
            d = u2.connect(s)
            d.window_size()          # 真探一下，连不上就抛
            usable.append(s)
        except Exception:  # noqa: BLE001  连不上/未授权 -> 跳过
            continue
    return usable[0] if len(usable) == 1 else None


def _run_locked(*, spec, answer, serial, skill_root, spec_in, engine):
    """真正执行用例。**只在持有设备锁时调用。**

    单独抽出来，是为了让 `with` 的范围一眼看清：从起点检查到断言，
    全程都在锁内。锁外只留"读用例 JSON"和"落盘报告"。
    """
    return run_case(
        spec,
        answer=answer,
        serial=serial,
        skill_root=skill_root,
        diag=bool(spec_in.get("diag", True)),
        # ★ 引擎走参数进来（不是事后给 report 赋值）——
        #   报告文件是 finish 节点写的、run_case 再读回来，事后赋值改不到文件里。
        engine=engine,
    )


def main() -> int:
    # ★ stdout 只留最终 JSON —— 进度日志必须全部走 stderr。
    # subagent._make_logger() 直接 print 到 stdout，会把报告冲散，
    # 所以这里把 fd 1 复制一份留作"真 stdout"，再把 fd 1 重定向到 fd 2。
    import os

    real_fd = os.dup(1)          # 复制句柄，dup2 之后它仍指向原 stdout
    sys.stdout.flush()
    os.dup2(2, 1)                # 之后所有 print 都进 stderr

    def emit(payload: dict) -> int:
        """把报告 JSON 写到真正的 stdout（用保存的 fd，绕开被改的 fd 1）。"""
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        os.write(real_fd, data)
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        emit({"status": "BLOCKED", "summary": "没有收到用例数据",
              "issues": ["stdin 为空"]})
        return 2

    try:
        spec_in = json.loads(raw)
    except json.JSONDecodeError as e:
        emit({"status": "BLOCKED", "summary": "用例 JSON 解析失败",
              "issues": [str(e)]})
        return 2

    # skill_root 决定知识卡去哪找；插件会带过来，直接跑本脚本时用默认值
    skill_root = spec_in.get("skill_root") or DEFAULT_SKILL
    serial = spec_in.get("serial") or _pick_device()

    # 让工具层的知识卡路径能被 search_knowledge 找到
    import android_agent.agent.runner as _sa
    _sa.DEFAULT_SKILL = skill_root

    try:
        # ★ 两种入口：
        #   case_name -> 从用例库按名字加载 JSON 用例
        #   否则      -> 吃主 Agent 现场拆的 steps/asserts JSON
        #
        # ⚠️ YAML 入口已整体删除（用户：「删除所有的YAML文件」）。
        _case_ref = spec_in.get("case_name") or spec_in.get("case_json")
        _from_file = False
        warns: list[str] = []
        if _case_ref:
            from android_agent.cases.loader import load_case, load_case_by_name
            try:
                if spec_in.get("case_json"):
                    spec, warns = load_case(str(spec_in["case_json"]))
                else:
                    spec, warns, _ = load_case_by_name(str(_case_ref))
                _from_file = True
            except FileNotFoundError:
                # ★ 库里还没有这份用例但有 steps -> 用现场拆的照跑。
                #
                #   以前这里直接抛出去变成 BLOCKED「没有用例无法执行」，
                #   于是形成死循环：没文件不能跑 -> 跑完才生成 -> 永远不生成。
                if not spec_in.get("steps"):
                    raise
                print(f"[case] 还没沉淀用例（{_case_ref}），"
                      f"用现场拆的 steps 跑", flush=True)
            if _from_file:
                for w in warns:
                    print(f"[case] ⚠️ 未采纳：{w}", flush=True)
                if spec_in.get("perm_action"):
                    spec.perm_action = spec_in["perm_action"]
                print(f"[case] 用例 {spec.case_id}（{len(spec.steps)} 步 / "
                      f"{len(spec.asserts)} 断言 / {len(warns)} 条未采纳）",
                      flush=True)
        # 没走文件 -> 用现场拆的 steps 构造
        if not _from_file:
            spec = CaseSpec(
                case_id=str(spec_in.get("case_id") or "case"),
                title=str(spec_in.get("title") or "未命名用例"),
                package=str(spec_in.get("package") or ""),
                steps=list(spec_in.get("steps") or []),
                asserts=list(spec_in.get("asserts") or []),
                knowledge_files=list(spec_in.get("knowledge_files") or []),
                # ★ 前提必须传下去 —— 漏了它，pm clear 不会执行，
                # App 就不是首次启动状态，用例从起点就走错（实测踩过）。
                preconditions=list(spec_in.get("preconditions") or []),
                perm_action=spec_in.get("perm_action") or None,
            )
    except Exception as e:
        emit({"status": "BLOCKED", "summary": "用例结构不合法",
              "issues": [f"{type(e).__name__}: {e}"]})
        return 2

    # ★ 谁来做判断：真模型 还是 规则应答器。
    #
    # 这个开关以前没有，于是**所有跑出来的数字都是规则应答器（autopilot）的**，
    # 但看起来像模型的成绩 —— 实测被这个坑过一次：
    # 查 "纯回放到底省了多少模型调用"，跑出来 decide=0，
    # 还以为模型真的没被叫，其实是根本没接模型。
    #
    # ⚠️ 默认是 "model"（不是 autopilot）。
    # 理由：忘了传 → 真模型（正确结果）；而不是忘了传 → 假成绩（危险）。
    # autopilot 改成"要显式声明才用"——它只验 YAML 结构，不产生用例结论。
    _engine = str(spec_in.get("answer") or "model").lower()
    if _engine not in ("model", "autopilot"):
        # 拼错时**报错**，不要静默退回某个引擎 ——
        # 静默退回正是"拿到假成绩却不知道"的成因。
        emit({"status": "BLOCKED",
              "summary": f"answer 只能是 model / autopilot，收到 {_engine!r}",
              "issues": ["拼写错误不会被静默容忍（否则会拿到不是你要的引擎的成绩）"]})
        return 2
    _answer = None
    if _engine == "model":
        from android_agent.agent.runner import make_model_answer
        _answer = make_model_answer(skill_root=skill_root, verbose=True)
        print("[engine] 真模型应答（decide/assert 交给 LLM）", flush=True)
    else:
        print("[engine] 规则应答器（不调用 LLM，仅验 YAML 结构；"
              "**其结果不能作用例结论**）", flush=True)

    # ★★ 设备互斥：**同一台设备同一时刻只能有一条用例在跑**。
    #
    # 为什么要在这里锁（2026-10 实测踩到）：
    #   用户派了两个子 Agent 同时跑用例，两个进程抢 `<设备序列号>`，结果是
    #     · 起点检查失败（期望 TimetableActivity，实际 AllInOneActivity
    #       —— 另一条用例把页面翻走了）
    #     · 连点 btnCreateManually 两次没反应（另一个人在点别的）
    #     · activity 在两条用例之间来回跳，最后判"卡死"
    #   这些**全不是被测 App 的问题**，却产出看起来正常的失败报告。
    #
    # 锁放在 `run_case` 外层、包住整个执行期 —— 起点检查、每步动作、
    # 断言都要在锁内，任何一段露在外面就等于没锁。
    #
    # 拿不到锁就**排队等**（设备是独占资源，等是正常语义）；
    # 等太久则明确失败，绝不默默往下跑 —— 那正是出问题的状态。
    from android_agent.device.lock import DeviceLock, DeviceBusy

    _lock = DeviceLock(serial)
    try:
        _lock.acquire()
    except DeviceBusy as e:
        emit({"status": "BLOCKED",
              "summary": "设备正被另一条用例占用，本次未执行",
              "issues": [str(e)]})
        return 3

    try:
        report = _run_locked(
            spec=spec, answer=_answer, serial=serial, skill_root=skill_root,
            spec_in=spec_in, engine=_engine)
    finally:
        # 无论成功、异常、崩溃都要放锁 —— 否则下次直接死锁
        _lock.release()

    # 双保险：万一 finish 那条路没带上，这里补一次（内存里的报告也要对）
    if not report.engine:
        report.engine = _engine

    # 报告落盘，路径回给上层（主 Agent 可读全文）
    #
    # ★ 统一写 tmp/ —— 临时产物不散在仓库根。tmp/ 里的东西按 24 小时
    #   自动清理（见 android_agent.report.scratch），所以报告只当"当次
    #   运行的证据"，不当长期存档。要长期留就自己拷出去。
    from android_agent.report.scratch import tmp_dir
    out_dir = tmp_dir()
    report_path = out_dir / f"report_{spec.case_id}_subagent.json"
    try:
        # ⚠️ report_path 要**先写进 dict 再落盘** —— 否则文件里没有这个字段，
        #   主 Agent 从文件读时拿不到"报告在哪"（虽然它自己就在报告里）。
        report.report_path = str(report_path)
        report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # ⚠️ 这里以前是 `except Exception: pass`，把两个真 bug 静默吞掉了：
        #   1) `report.to_dict()` —— SubagentReport 根本没这个方法
        #   2) `report.report_path = ...` —— pydantic v2 拒绝未声明字段
        #   两个都被吞掉，表现为"报告文件不存在"，且没有任何报错线索。
        #   写不出报告是"可诊断性"层面的故障，必须说出来。
        print(f"[report] ⚠️ 报告文件没写成（{type(e).__name__}: {e}）；"
              f"结论仍会通过 stdout 返回", flush=True)

    # ★ 落库：每次运行存一行历史，WebUI 从这里读。
    #
    # 为什么必须做：在此之前 report_<case>.json 和 clicks_<case>.jsonl
    # **每次都覆盖**，于是"同一个用例上次过没过、这次为什么变了"
    # 根本没有数据可查。落库之后历史才留得住。
    #
    # ⚠️ 落库失败**绝不能影响用例结论** —— 它只是展示层。
    #    所以整段吞异常，只在 stderr 提示一句。
    run_id = None
    try:
        from android_agent.reporting import store as _store
        from pathlib import Path as _P
        # ★ 产物都在本次运行的目录里（runs/<uuid>_<时间戳>_<case>/）。
        #   报告自己带着这个路径，直接从它拼，不再去 tmp/ 根下按 case_id 猜。
        _run_dir = _P(report.run_dir) if getattr(report, "run_dir", "") else _P()
        _rows: list = []
        _ledger_file = _run_dir / "clicks.jsonl"
        if _ledger_file.exists():
            for _line in _ledger_file.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line:
                    try:
                        _rows.append(json.loads(_line))
                    except json.JSONDecodeError:
                        pass
        _rep = report.to_dict()
        # 报告里的 steps_done 是"做了几步"，总步数在 summary 的
        # 「步骤 7/7」里 —— store 自己会去抠。
        run_id = _store.save_run(
            case_name=str(spec.case_id),
            report=_rep,
            ledger_rows=_rows,
            paths={
                "ledger": str(_ledger_file),
                "log": str(_run_dir / "run.log"),
                "llm": str(_run_dir / "llm.md"),
                "report": str(report_path),
                "run_dir": str(_run_dir),
            },
        )
        print(f"[report] 已落库 run_id={run_id}", file=sys.stderr, flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[report] ⚠️ 落库失败（{type(_e).__name__}: {_e}）；"
              f"不影响本次结论", file=sys.stderr, flush=True)

    payload = {
        "status": report.status,
        # ★ 引擎必须回给主 Agent —— 报告长得一样，只有这个字段能分辨
        # "这是模型的结论" 还是 "这是规则应答器的结论"。
        "engine": getattr(report, "engine", "") or _engine,
        "summary": report.summary,
        "steps_done": report.steps_done,
        "coverage_ok": report.coverage_ok,
        "issues": list(report.issues or []),
        "findings": [f for f in (report.findings or [])],
        "runtime_permissions": list(report.runtime_permissions or []),
        "runtime_permission_prompts": list(report.runtime_permission_prompts or []),
        "dialogs_dismissed": list(report.dialogs_dismissed or []),
        "preconditions_done": list(report.preconditions_done or []),
        "restored": list(report.restored or []),
        # ★ 模型对话原文的路径必须回给主 Agent。
        #   payload 是**手拼**的，漏一个字段就永远不会出现 ——
        #   之前漏了 llm_path，主 Agent 拿到的就是 None，
        #   等于"模型对话记录了但没人知道在哪"。
        "llm_path": getattr(report, "llm_path", "") or "",
        "run_dir": getattr(report, "run_dir", "") or "",
        "report_path": getattr(report, "report_path", None),
    }
    return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())

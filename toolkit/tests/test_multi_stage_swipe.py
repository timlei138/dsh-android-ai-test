# -*- coding: utf-8 -*-
"""`tools/multi_stage_swipe.py` —— 分阶段滑动的单测。

## 为什么这块必须有测试

它是**唯一会自己驱动手指**的代码，错了的代价和别的模块不一样：

  · 参数校验漏一条 → 一个写错的 `--stages` 会被真正执行，把界面拖到别处，
    而用例还在按原来的预期判（结论错，且证据是"看起来动了"）；
  · 抬手没放进 `finally` → 中途异常时手指**按在屏幕上不松**。实测过一次：
    之后所有 tap 都变成屏内误触，只能拔 USB 恢复；
  · 插值写错 → 位移量不够，滚动没到底，判成"没有更多数据"。

这三条都不需要真机就能验：`parse_swipe` / `check_bounds` 是纯函数，
`run_swipe` 只依赖一个 `device.touch` 对象 —— 用假设备记录调用序列即可。
"""
import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import multi_stage_swipe as mss  # noqa: E402

CASES = []

def _tmp_parent() -> str:
    """临时目录的父目录：优先仓库内的 storage/tmp，不可用就退回系统临时目录。

    测试不该因为「从没跑过用例、storage/tmp 还没被建出来」就报错。
    """
    import tempfile
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()



def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def xy(value):
    """与 act.py 里同契约的坐标解析器：'x,y' -> (int, int)。"""
    a, _, b = str(value).partition(",")
    return int(a), int(b)


def expect_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("应当报 ValueError，但没抛")


class FakeTouch:
    """记录 down/move/up 的调用序列与坐标。"""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def down(self, x, y):
        if "down" in self.fail_on:
            raise RuntimeError("device gone")
        self.calls.append(("down", x, y))

    def move(self, x, y):
        if "move" in self.fail_on:
            raise RuntimeError("device gone")
        self.calls.append(("move", x, y))

    def up(self, x, y):
        if "up" in self.fail_on:
            raise RuntimeError("up failed")
        self.calls.append(("up", x, y))


class FakeDevice:
    def __init__(self, touch):
        self.touch = touch


class FakeClock:
    """可推进的假时钟：sleep 一次就前进相应秒数。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(stages, start=(100, 500), touch=None):
    clock = FakeClock()
    t = touch or FakeTouch()
    mss.run_swipe(FakeDevice(t), start, stages, clock=clock, sleep=clock.sleep)
    return t


# ── parse_swipe ────────────────────────────────────────────────────

@case("parse：单段 move 正常解析")
def _():
    origin, stages = mss.parse_swipe("100,500", [{"type": "move", "to": "100,100", "dur": 300}], xy)
    assert origin == (100, 500)
    assert stages == [{"type": "move", "dur": 300, "to": (100, 100)}]


@case("parse：hold 不带 to")
def _():
    _, stages = mss.parse_swipe("10,20", [{"type": "hold", "dur": 500}], xy)
    assert stages == [{"type": "hold", "dur": 500}]


@case("★ parse：hold 带 to 必须拒（会让人以为它移动了）")
def _():
    msg = expect_error(mss.parse_swipe, "0,0",
                       [{"type": "hold", "dur": 100, "to": "5,5"}], xy)
    assert "to" in msg


@case("★ parse：move 缺 to 必须拒")
def _():
    msg = expect_error(mss.parse_swipe, "0,0", [{"type": "move", "dur": 300}], xy)
    assert ".to" in msg or "to " in msg


@case("parse：未知 type 拒绝，报错点明是第几段（从 1 起算）")
def _():
    msg = expect_error(mss.parse_swipe, "0,0",
                       [{"type": "move", "to": "1,1", "dur": 10},
                        {"type": "tap", "dur": 10}], xy)
    assert "stages[2]" in msg
    assert "move 或 hold" in msg


@case("★ parse：未知字段拒绝（写错的键不能被静默忽略）")
def _():
    msg = expect_error(mss.parse_swipe, "0,0",
                       [{"type": "move", "to": "1,1", "dur": 10, "ease": "linear"}], xy)
    assert "ease" in msg


@case("parse：dur 必须是正整数（0 / 负数 / 浮点 / 字符串 / 缺省 / bool 全拒）")
def _():
    for bad in (0, -1, 1.5, "300", None, True, False):
        expect_error(mss.parse_swipe, "0,0",
                     [{"type": "hold", "dur": bad}], xy)


@case("parse：stages 必须是非空数组")
def _():
    for bad in ([], None, {}, "move"):
        expect_error(mss.parse_swipe, "0,0", bad, xy)


@case("parse：stage 必须是对象")
def _():
    msg = expect_error(mss.parse_swipe, "0,0", ["move,1,1"], xy)
    assert "对象" in msg


@case("★ parse：坐标必须是字符串、非负、能被 parse_xy 解析")
def _():
    expect_error(mss.parse_swipe, 100, [{"type": "hold", "dur": 10}], xy)
    expect_error(mss.parse_swipe, "0,0",
                 [{"type": "move", "to": (1, 1), "dur": 10}], xy)
    msg = expect_error(mss.parse_swipe, "-5,0", [{"type": "hold", "dur": 10}], xy)
    assert "负数" in msg
    msg = expect_error(mss.parse_swipe, "0,0",
                       [{"type": "move", "to": "abc", "dur": 10}], xy)
    assert "坐标格式" in msg


@case("★ parse：按下前的参数错误一条都不用碰设备")
def _():
    # 这条行为是「rejected=True，device untouched」的实现基础：
    # parse_swipe 抛错时 run_swipe 根本没被调用。
    t = FakeTouch()
    try:
        mss.parse_swipe("0,0", [{"type": "move", "dur": 10}], xy)
    except ValueError:
        pass
    assert t.calls == []


# ── check_bounds ───────────────────────────────────────────────────

@case("bounds：起点与所有 to 都在屏内 → 通过")
def _():
    mss.check_bounds((10, 10),
                     [{"type": "move", "to": (100, 100)},
                      {"type": "hold"},
                      {"type": "move", "to": (0, 0)}],
                     (200, 200))


@case("★ bounds：边界值 —— 右下角 (w-1, h-1) 合法，w / h 本身非法")
def _():
    mss.check_bounds((199, 199), [], (200, 200))
    expect_error(mss.check_bounds, (200, 199), [], (200, 200))
    expect_error(mss.check_bounds, (199, 200), [], (200, 200))


@case("bounds：报错要指明是起点还是第几段")
def _():
    msg = expect_error(mss.check_bounds, (0, 0),
                       [{"type": "move", "to": (500, 10)}], (200, 200))
    assert "stages[1].to" in msg
    msg = expect_error(mss.check_bounds, (500, 0), [], (200, 200))
    assert "start" in msg


@case("bounds：hold 不参与（它没有 to）")
def _():
    mss.check_bounds((5, 5), [{"type": "hold", "dur": 100}], (10, 10))


# ── run_swipe ──────────────────────────────────────────────────────

@case("run：按下 → 一路移动到目标 → 抬手，首尾坐标正确")
def _():
    t = run([{"type": "move", "to": (100, 100), "dur": 200}], start=(100, 500))
    assert t.calls[0] == ("down", 100, 500)
    assert t.calls[-1] == ("up", 100, 100)
    moves = [c for c in t.calls if c[0] == "move"]
    assert moves, "一次 move 都没有"
    # 单调向目标靠近
    ys = [c[2] for c in moves]
    assert ys == sorted(ys, reverse=True), ys
    assert ys[-1] == 100


@case("run：hold 只等待，不产生 move，位置保持")
def _():
    t = run([{"type": "hold", "dur": 300},
             {"type": "move", "to": (100, 400), "dur": 100}], start=(100, 500))
    assert t.calls[0] == ("down", 100, 500)
    moves = [c for c in t.calls if c[0] == "move"]
    assert moves[-1] == ("move", 100, 400)


@case("run：多段连续，每段从上一段终点起算")
def _():
    t = run([{"type": "move", "to": (100, 300), "dur": 100},
             {"type": "move", "to": (400, 300), "dur": 100}],
            start=(100, 500))
    xs = [c[1] for c in t.calls if c[0] == "move"]
    assert xs[0] == 100              # 第一段只动 y
    assert max(xs) == 400            # 第二段一路到 x=400
    assert t.calls[-1] == ("up", 400, 300)


@case("★ run：中途 move 抛异常 → 仍然抬手（手指不能留在屏幕上）")
def _():
    t = FakeTouch(fail_on={"move"})
    clock = FakeClock()
    try:
        mss.run_swipe(FakeDevice(t), (100, 500),
                      [{"type": "move", "to": (100, 100), "dur": 100}],
                      clock=clock, sleep=clock.sleep)
    except RuntimeError:
        pass
    else:
        raise AssertionError("异常被吞掉了")
    assert any(c[0] == "up" for c in t.calls), f"没抬手：{t.calls}"


@case("★ run：异常照常外抛（不能为了抬手把根因吃掉）")
def _():
    t = FakeTouch(fail_on={"move"})
    clock = FakeClock()
    try:
        mss.run_swipe(FakeDevice(t), (10, 10),
                      [{"type": "move", "to": (10, 20), "dur": 100}],
                      clock=clock, sleep=clock.sleep)
    except RuntimeError as exc:
        assert "device gone" in str(exc)
    else:
        raise AssertionError("没有外抛")


@case("★ run：原本一切正常、只有 up 失败 → 外抛（这是真故障）")
def _():
    t = FakeTouch(fail_on={"up"})
    clock = FakeClock()
    try:
        mss.run_swipe(FakeDevice(t), (10, 10), [{"type": "hold", "dur": 10}],
                      clock=clock, sleep=clock.sleep)
    except RuntimeError as exc:
        assert "up failed" in str(exc)
    else:
        raise AssertionError("up 失败被吞了")


@case("★ run：down 就失败 → 不尝试抬手（手都没按下去）")
def _():
    t = FakeTouch(fail_on={"down"})
    clock = FakeClock()
    try:
        mss.run_swipe(FakeDevice(t), (10, 10), [{"type": "hold", "dur": 10}],
                      clock=clock, sleep=clock.sleep)
    except RuntimeError:
        pass
    assert t.calls == [], t.calls


@case("run：空 stages 也能安全按一下再抬手")
def _():
    t = run([], start=(5, 5))
    assert t.calls == [("down", 5, 5), ("up", 5, 5)], t.calls


# ── act.py 命令行接线（不需要真机：坏参数在连设备之前就应当被拒） ──

def _act_cli(start, stages_json):
    """跑一次真实的 `act.py multi_stage_swipe`，返回 (rc, json)。

    用临时 ANDROID_TEST_STORAGE，避免污染真实会话库。
    """
    import json
    import os
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(dir=_tmp_parent()) as tmp:
        env = dict(os.environ, ANDROID_TEST_STORAGE=tmp)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "act.py"), "multi_stage_swipe",
             "--start", start, "--stages", stages_json],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), env=env, timeout=90)
    text = (proc.stdout or "").strip()
    try:
        return proc.returncode, (json.loads(text) if text else {})
    except ValueError:
        return proc.returncode, {"raw": text[:400]}


@case("★ CLI：坏参数 → rc=1 + rejected=true + 未执行（连设备之前就拒）")
def _():
    for start, stages in [
        ("0,0", '[{"type":"move","dur":300}]'),                     # move 缺 to
        ("abc", '[{"type":"hold","dur":300}]'),                     # start 不是坐标
        ("0,0", '[{"type":"move","to":"10,10","dur":10,"ease":"x"}]'),
        ("0,0", '[{"type":"hold","dur":0}]'),
        ("0,0", 'not-json'),                                        # JSON 本身坏
    ]:
        rc, out = _act_cli(start, stages)
        assert rc != 0, f"{stages} 竟然 rc=0：{out}"
        assert out.get("rejected") is True, f"{stages} 没标 rejected：{out}"
        assert not out.get("event_ids"), f"{stages} 竟然写了事件：{out}"
        assert out.get("error"), f"{stages} 没有 error 文案：{out}"


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name} -> {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name} -> {type(exc).__name__}: {exc}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

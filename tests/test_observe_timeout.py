# -*- coding: utf-8 -*-
"""验证：observe 的各步都有硬超时，不会无限重试卡死。

不复现真机异常（那要运气），而是**人为把底层调用换成永远不返回的**，
看超时机制会不会把我们捞出来。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.device.direct import DirectDevice  # noqa: E402

ok = bad = 0


def ck(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        bad += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


print("=" * 70)
print("1) dump_hierarchy 卡住时，必须在超时后抛出来")
print("=" * 70)
d = DirectDevice.__new__(DirectDevice)   # 不连真机


class HangingD:
    def dump_hierarchy(self, *a, **k):
        time.sleep(3600)                  # 模拟 u2 无限重试

    def screenshot(self, *a, **k):
        time.sleep(3600)


d._d = HangingD()
t0 = time.time()
try:
    d._dump_hierarchy(timeout=2.0)
    ck("应该抛 TimeoutError", False, "没抛")
except TimeoutError as e:
    dt = time.time() - t0
    ck("抛了 TimeoutError", True, f"{dt:.1f}s")
    ck("在超时附近返回（不是 3600s）", dt < 5, f"{dt:.1f}s")
    ck("错误信息说明了原因", "无限重试" in str(e), str(e)[:50])

print()
print("=" * 70)
print("2) screenshot 卡住时，返回 False 而不是卡死")
print("=" * 70)
t0 = time.time()
r = d._screenshot("dummy.png", timeout=2.0)
dt = time.time() - t0
ck("返回 False", r is False)
ck("在超时附近返回", dt < 5, f"{dt:.1f}s")

print()
print("=" * 70)
print("3) 正常情况不受影响（不误伤）")
print("=" * 70)


class GoodD:
    def dump_hierarchy(self, *a, **k):
        return "<hierarchy/>"

    def screenshot(self, *a, **k):
        pass


d._d = GoodD()
ck("dump 正常返回", d._dump_hierarchy(timeout=5) == "<hierarchy/>")
ck("screenshot 正常返回 True", d._screenshot("dummy.png", timeout=5) is True)

print()
print("=" * 70)
print("4) 底层抛异常时，原样传出来（不能被吞）")
print("=" * 70)


class ErrD:
    def dump_hierarchy(self, *a, **k):
        raise ValueError("u2 坏了")


d._d = ErrD()
try:
    d._dump_hierarchy(timeout=5)
    ck("应该抛出原异常", False)
except ValueError as e:
    ck("原异常传出来了", str(e) == "u2 坏了", str(e))

print()
print("=" * 70)
print(f"  {bad} 失败 / {ok + bad} 项")
print("=" * 70)
sys.exit(1 if bad else 0)

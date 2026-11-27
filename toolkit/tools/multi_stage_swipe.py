"""单指分阶段滑动；本模块不负责取证、存储或连接设备。"""
from __future__ import annotations

import math
import time


def parse_swipe(start, stages, parse_xy):
    """复用调用方已有的 xy() 解析器，在按下前完成全部参数校验。"""

    def coordinate(value, field):
        if not isinstance(value, str):
            raise ValueError(f"{field} 必须是 x,y 坐标字符串")
        try:
            point = parse_xy(value)
        except (ValueError, TypeError) as error:
            raise ValueError(f"{field} 坐标格式错误，要求整数 x,y: {value!r}") from error
        if any(n < 0 for n in point):
            raise ValueError(f"{field} 坐标不能为负数")
        return point

    origin = coordinate(start, "start")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages 必须是非空数组")
    parsed = []
    for index, stage in enumerate(stages, 1):
        field = f"stages[{index}]（阶段从 1 起算）"
        if not isinstance(stage, dict):
            raise ValueError(f"{field} 必须是对象")
        kind = stage.get("type")
        if kind not in ("move", "hold"):
            raise ValueError(f"{field}.type 必须为 move 或 hold")
        allowed = {"type", "dur", "to"} if kind == "move" else {"type", "dur"}
        unknown = set(stage) - allowed
        if unknown:
            raise ValueError(f"{field} 不接受字段: {', '.join(sorted(unknown))}")
        duration = stage.get("dur")
        if type(duration) is not int or duration <= 0:
            raise ValueError(f"{field}.dur 必须填写正整数毫秒数")
        try:
            if not math.isfinite(duration / 1000):
                raise OverflowError
        except OverflowError as error:
            raise ValueError(f"{field}.dur 超出可执行范围") from error
        item = {"type": kind, "dur": duration}
        if kind == "move":
            item["to"] = coordinate(stage.get("to"), f"{field}.to")
        parsed.append(item)
    return origin, parsed


def check_bounds(start, stages, size):
    width, height = size
    points = [("start", start)]
    points.extend((f"stages[{index}].to", stage["to"])
                  for index, stage in enumerate(stages, 1) if stage["type"] == "move")
    for field, (x, y) in points:
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"{field} 坐标超出当前屏幕 {width}x{height}")


def run_swipe(device, start, stages, *, clock=time.monotonic, sleep=time.sleep):
    """按顺序执行按下、移动或停留、松手；时长为目标值，不保证严格实时。

    finally 统一尝试抬手，包括按下失败、移动失败和中断。
    down/move 响应丢失时触摸可能已经执行；清理失败不能覆盖原始异常，
    正常完成动作后若抬手失败，则向调用方抛出该异常。
    """
    touch = device.touch
    position = start
    ok = False
    try:
        touch.down(*position)
        for stage in stages:
            seconds = stage["dur"] / 1000
            if stage["type"] == "hold":
                sleep(seconds)
                continue
            origin, target = position, stage["to"]
            segment_start = clock()
            deadline = segment_start + seconds
            while True:
                now = clock()
                if now < deadline:
                    sleep(min(0.020, deadline - now))
                now = clock()
                fraction = 1.0 if now >= deadline else (now - segment_start) / seconds
                point = tuple(round(a + (b - a) * fraction)
                              for a, b in zip(origin, target))
                if point != position or fraction >= 1:
                    # 响应丢失时移动可能已执行，先记录本次尝试的目标坐标。
                    position = point
                    touch.move(*position)
                if fraction >= 1:
                    break
        ok = True
    finally:
        # 抬手失败或被中断时保留原始异常；动作成功时才外抛清理异常。
        try:
            touch.up(*position)
        except BaseException:
            if ok:
                raise

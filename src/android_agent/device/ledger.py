# -*- coding: utf-8 -*-
"""点击账本：每一次点击都留一条完整记录，方便事后排查。

## 为什么单独做一个模块

用户的原话："每一步点击了啥都要增加记录，好方便排查。"

现在的日志只写 `-> 点 (1795,1583) 理由...`，缺三样东西：

  ① **点到的是什么控件** —— 坐标是像素，人看不出那是「同意」还是「完成」
  ② **点之前页面是什么** —— 不知道起点就没法判断这步该不该点
  ③ **点之后变成了什么** —— 不知道结果就分不清"点了没反应"和"点了但没跳"

没有这三样，"一直点完成"这种问题只能靠猜。所以账本必须记全。

## 每条记录记什么

| 字段 | 为什么要有 |
|------|-----------|
| seq        | 第几次点击（判断有没有重复点同一个） |
| step       | 哪一步（s3 还是 s2） |
| target     | 点中的控件：rid/text/坐标 —— **这是排查的核心** |
| before/after | 前后页面 activity |
| ms         | 点击本身 + 观察各花多久（找出卡在哪） |
| changed    | 前后 XML 是否一致（判断"点了没反应"） |
| why        | 决策理由（模型/代码给的） |

## 落在哪

JSONL（每行一个 JSON），追加写。路径 `<workdir>/clicks_<case>.jsonl`。
JSONL 的好处：一边跑一边能 `tail`，中途崩了也不丢已经写完的。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class ClickLedger:
    """一次运行一份账本。线程/重入都不需要——执行是串行的。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._rows: list[dict[str, Any]] = []

    def record(
        self,
        *,
        step: str = "",
        action: str = "click",
        target: dict[str, Any] | None = None,
        before: str = "",
        after: str = "",
        ms: float = 0.0,
        changed: bool | None = None,
        why: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记一次动作。返回这条记录（方便调用方顺手打印）。"""
        self._seq += 1
        row: dict[str, Any] = {
            "seq": self._seq,
            "t": time.strftime("%H:%M:%S"),
            "ts": round(time.time(), 3),
            "step": step,
            "action": action,
            "target": target or {},
            "before": before,
            "after": after,
            "ms": round(ms, 1),
            "changed": changed,
            "why": (why or "")[:300],
        }
        if extra:
            row.update(extra)
        self._rows.append(row)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001  账本写不进去不能影响执行
            pass
        return row

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)

    # -- 人读的摘要 -------------------------------------------------------
    def render(self, *, last: int | None = None) -> str:
        """把账本渲染成表格，排查时直接看这个。"""
        rows = self._rows[-last:] if last else self._rows
        if not rows:
            return "（还没有点击记录）"
        out = [
            f"{'#':>3}  {'时刻':8} {'步':4} {'控件':28} "
            f"{'前':22} {'后':22} {'耗时':>7}  变化",
            "-" * 110,
        ]
        for r in rows:
            t = r.get("target") or {}
            ctrl = _fmt_target(t)
            chg = {True: "是", False: "否", None: "?"}[r.get("changed")]
            out.append(
                f"{r['seq']:>3}  {r['t']:8} {str(r.get('step') or '-'):4} "
                f"{ctrl:28} {_short(r.get('before'), 22):22} "
                f"{_short(r.get('after'), 22):22} "
                f"{r.get('ms', 0):6.0f}ms  {chg}")
        return "\n".join(out)


def _fmt_target(t: dict[str, Any]) -> str:
    """控件渲染成人能看的一句：优先 rid，其次 text，最后坐标。"""
    rid = str(t.get("rid") or "")
    txt = str(t.get("text") or "")
    c = t.get("c") or []
    bits = []
    if txt:
        bits.append(f"「{txt}」")
    if rid:
        bits.append(rid)
    s = " ".join(bits) if bits else ""
    if c:
        s = f"{s} {c}" if s else f"({c})"
    return s or "(未知)"


def _short(s: Any, n: int) -> str:
    s = str(s or "-")
    # activity 名太长，只留最后两段
    if "." in s and len(s) > n:
        s = s.split(".")[-1]
    return s[:n]


def target_from_screen(scr: Any, x: int, y: int) -> dict[str, Any]:
    """从 Screen 里反查 (x,y) 落在哪个可点控件上 —— 这是账本最有价值的一列。

    坐标本身没有意义，人看到「(1795,1583)」不知道那是「同意」还是「完成」。
    这里把控件信息补上，让账本直接可读。
    """
    best: dict[str, Any] | None = None
    best_d = None
    for n in getattr(scr, "clickable", []) or []:
        c = n.get("c") or n.get("center")
        if not c or len(c) < 2:
            continue
        d = abs(int(c[0]) - x) + abs(int(c[1]) - y)
        if d <= 30 and (best_d is None or d < best_d):
            best, best_d = n, d
    if best:
        return {"rid": best.get("r") or best.get("rid") or "",
                "text": best.get("t") or best.get("text") or "",
                "c": [x, y]}
    return {"c": [x, y]}

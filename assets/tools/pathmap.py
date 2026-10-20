"""路径图执行器：把「路径图里已有的边」变成可批量走的计划。

背景：会话时长的大头**不是工具执行**，而是 AI 在两次调用之间的判断
（实测占 58~64%）。当一条路径**上次已经走过、且已记进路径图**时，
就不该再让 AI 逐步 "看一眼 → 想一下 → 点一下"。

设计要点（三条来自实测）：
  1. **不硬编码坐标** —— 路径图的锚点是 rid/desc/text，跨设备/分辨率可用；
     坐标只在锚点找不到时按当前设备现场派生（走 observe 的 next）。
  2. **每步带检查点** —— 落点不符即停，报告第几步、期望什么、实际什么。
  3. **不改路径图** —— 本模块只*读*；边的采集仍走 session.py 的既有流程。
"""

from __future__ import annotations

import re
from pathlib import Path

import checkpoint


def _cells(line: str) -> list[str]:
    """把 markdown 表格行切成单元格（去掉首尾空 cell）。"""
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


def parse_nodes(text: str) -> list[dict]:
    """解析节点：{name, activity, keys, edges}。

    edges 元素：{anchor, rid, desc, text, to}
    出边表在节点的 `**出边**：` 之后，格式：| 锚点 | rid | desc | text | → 去向 |
    """
    nodes: list[dict] = []
    cur: dict | None = None
    in_edges = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            name = ln[3:].strip()
            if _is_non_node(name):
                cur = None
                continue
            cur = {"name": name, "activity": "", "keys": [], "edges": []}
            nodes.append(cur)
            in_edges = False
            continue
        if cur is None:
            continue
        s = ln.strip()
        if s.startswith("**出边**"):
            in_edges = True
            continue
        if s.startswith("- **activity**:"):
            cur["activity"] = s.split(":", 1)[1].strip().strip("`")
            in_edges = False
            continue
        if s.startswith("- **识别键**:"):
            for part in s.split(":", 1)[1].split("·"):
                p = part.strip().strip("`").strip()
                if "=" in p:
                    k, v = p.split("=", 1)
                    cur["keys"].append((k.strip(), v.strip()))
            in_edges = False
            continue
        if s.startswith(">"):          # 引用块是注释，不是表格
            continue
        if in_edges and s.startswith("|"):
            cells = _cells(s)
            if len(cells) < 5:
                continue
            first = cells[0]
            if set(first) <= set("-: "):       # 表头分隔行 |---|---|
                continue
            # 表头：首格是「锚点…」或整行就是列名
            if first.startswith("锚点") or (cells[1] == "rid" and cells[2] == "desc"):
                continue
            cur["edges"].append({
                "anchor": cells[0],
                "rid": _clean(cells[1]),
                "desc": _clean(cells[2]),
                "text": _clean(cells[3]),
                "to": cells[4].lstrip("→ ").strip(),
            })
    return nodes


def _clean(v: str) -> str:
    """清掉 `—`（实测为空）、反引号、以及 ⚠️多态 之类的标注。"""
    v = v.strip().strip("`").strip()
    if v in ("—", "-", ""):
        return ""
    v = re.sub(r"⚠️.*$", "", v).strip()
    return v.strip("`").strip()


# 不是节点的二级标题（索引、说明、自动采集区）。
def _is_non_node(name: str) -> bool:
    return (name.startswith("⚠️") or name.startswith("节点索引")
            or name.startswith("自动采集") or name.startswith("锚点表读法"))


def find_node(nodes: list[dict], name: str) -> dict | None:
    """按节点名精确/包含匹配。"""
    name = (name or "").strip()
    for n in nodes:
        if n["name"] == name:
            return n
    for n in nodes:
        if name and (name in n["name"] or n["name"] in name):
            return n
    return None


def edge_selector(edge: dict) -> str:
    """把一条边转成 `--expect`/定位用的选择器（rid 优先，其次 desc/text）。

    与路径图的定位降级链一致：rid → desc → text。
    """
    if edge.get("rid"):
        # 同 rid 多态时（如 item_title）文字才是区分依据，必须带上
        if edge.get("text"):
            return f"rid={edge['rid']},text={edge['text']}"
        return f"rid={edge['rid']}"
    if edge.get("desc"):
        return f"desc={edge['desc']}"
    if edge.get("text"):
        return f"text={edge['text']}"
    return ""


def path_between(nodes: list[dict], start: str, goal: str,
                 max_hops: int = 12) -> dict:
    """在路径图里找 start → goal 的边序列（BFS，最少跳数）。

    返回 {"found": bool, "nodes": [...节点名...], "steps": [...边...], "reason": str}
    边里带 from/to，便于报告"走到第几步"。
    """
    by_name = {n["name"]: n for n in nodes}
    s, g = find_node(nodes, start), find_node(nodes, goal)
    if not s or not g:
        return {"found": False, "nodes": [], "steps": [],
                "reason": f"路径图里找不到节点: {'起点 ' + start if not s else ''}"
                          f"{' 终点 ' + goal if not g else ''}".strip()}
    if s["name"] == g["name"]:
        return {"found": True, "nodes": [s["name"]], "steps": [], "reason": "起点即终点"}

    from collections import deque
    q = deque([(s["name"], [s["name"]], [])])
    seen = {s["name"]}
    while q:
        cur_name, names, steps = q.popleft()
        if len(steps) >= max_hops:
            continue
        cur = by_name.get(cur_name)
        if not cur:
            continue
        for e in cur["edges"]:
            tgt = e["to"]
            tname = find_node(nodes, tgt)
            if not tname:
                continue          # 去向不是本图节点（如「相机」这类跨 App）→ 跳过
            if tname["name"] in seen:
                continue
            nsteps = steps + [{**e, "from": cur_name, "to": tname["name"]}]
            nnames = names + [tname["name"]]
            if tname["name"] == g["name"]:
                return {"found": True, "nodes": nnames, "steps": nsteps,
                        "reason": f"{len(nsteps)} 步"}
            seen.add(tname["name"])
            q.append((tname["name"], nnames, nsteps))
    return {"found": False, "nodes": [], "steps": [],
            "reason": f"路径图里没有 {' → '.join([s['name'], g['name']])} 的通路"
                      f"（最多试了 {max_hops} 跳）"}


def load(path_map: Path) -> list[dict]:
    return parse_nodes(path_map.read_text(encoding="utf-8")) if path_map.is_file() else []


def finish_expect(node: dict | None) -> str:
    """到达目标节点后，用什么校验（优先 activity，其次该节点的识别键）。

    ⚠️ 识别键要挑**稳定的那个**：优先 `rid=`，其次 `desc=`，最后才 `text=`。
    很多节点是**多形态**的，text 形态会变 —— 例如「课程表基本信息编辑页」的
    识别键之一是占位符 `text=课程表名称（必填）`，**一旦填了名字这个 text 就没了**，
    用它做检查点会在"编辑态"误报未命中（真实事故：会话 334 第 2 步）。
    rid 与形态无关（输入框填没填都是 `et_schedule_name`），所以优先。
    """
    if not node:
        return ""
    parts = []
    if node.get("activity"):
        parts.append("activity=" + node["activity"].lstrip("."))
    keys = node.get("keys") or []
    for kind in ("rid", "desc", "text"):
        pick = next((v for k, v in keys if k == kind and v), "")
        if pick:
            parts.append(f"{kind}={pick}")
            break
    return ",".join(parts)

"""read：OCR 读屏。读 UI 树里没有的东西：toast、Canvas 自绘控件文字。

用法：
  python read.py --image storage/evidence/case172/step03/shot.png
  python read.py --image <png> --region 100,200,900,400      # x1,y1,x2,y2
  python read.py --name case172/step03 --region ...          # 不传 --image 则现场截一张
"""
from __future__ import annotations
from runtime import execution

import argparse
import os
from pathlib import Path

from common import (apply_stdin_args, connect, emit, evidence_paths, fail, guard_terminated,
                    log_auto, rel)
import time
from datetime import datetime
from runtime import stage, timed_call



# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

def main() -> None:
    p = argparse.ArgumentParser(description="read：OCR 读取屏幕/截图文字")
    # 只挂 read 真正用得到的参数：read 不产出 nodes，
    # 故 --full/--no-filter/--settle/--perm-action/--perm 都不该出现（会误导）。
    p.add_argument("--serial", default=None, help="多设备时指定 adb serial")
    p.add_argument("--dir", default=None, help="证据根目录（默认 storage/evidence）")
    p.add_argument("--name", default=None, help="证据名，如 case172/step03")
    p.add_argument("--image", default=None, help="已有截图路径；缺省则现场截图")
    p.add_argument("--region", default=None, help="x1,y1,x2,y2（截图坐标系）")
    apply_stdin_args()   # 支持 --stdin-json（参数走 stdin，避开 Windows 引号问题）
    args = p.parse_args()
    execution("not_executed")

    # 现场截图会被拦截；读"已有图片"（如崩溃前落盘的证据）放行——
    # 那是复核证据，不是对已终止会话继续操作。
    if not args.image:
        guard_terminated("read")
        try:
            d = connect(args.serial)
        except Exception as e:  # noqa: BLE001
            fail(f"设备连接失败: {e}")

    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")

    try:
        with stage("ocr_import"):
            # OCR 库模块名：新包叫 `rapidocr`（3.x，原生支持 3.13），
            # 旧包叫 `rapidocr_onnxruntime`。先试新的，再退回旧的 —— 两种环境都能跑。
            # 两者**返回类型不同**，见下面 ocr_call 的注释。
            from rapidocr import RapidOCR
            _OCR_NEW = True
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR   # 旧环境兜底
            _OCR_NEW = False
        except ImportError:
            fail("缺依赖：pip install rapidocr "
                 + ("（或 pwsh -File scripts/setup.ps1 一键装）" if os.name == "nt"
                    else "（或 bash toolkit/scripts/setup.sh 一键装）"))

    if args.image:
        img_path = Path(args.image)
        if not img_path.exists():
            fail(f"截图不存在: {img_path}")
        evidence = rel(img_path)
    else:
        execution("may_have_executed")
        png, _, meta = evidence_paths(args.dir, args.name)
        png.parent.mkdir(parents=True, exist_ok=True)
        image = timed_call("screenshot", d.screenshot)
        timed_call("png_save", image.save, str(png))
        img_path = png
        evidence = rel(png)

    ocr = timed_call("ocr_initialize", RapidOCR)
    execution("may_have_executed")

    # ══════════════════════════════════════════════════════════════
    # OCR 两种返回结构都要认，不靠包名猜，直接看结构：
    #
    #   旧包 rapidocr_onnxruntime 返回**元组** `(result, elapse)`，
    #       result = [[box, text, score], ...]，box 是 4 个点
    #   新包 rapidocr 3.x 返回**对象** RapidOCROutput（**不可迭代**，
    #       按元组解包会抛 TypeError: cannot unpack non-iterable）：
    #       res.boxes / res.txts / res.scores
    # ══════════════════════════════════════════════════════════════
    raw = timed_call("ocr_inference", ocr, str(img_path))
    execution("executed")

    triples: list[tuple] = []      # 统一成 (box, text, score)
    if isinstance(raw, tuple) and len(raw) == 2:
        # 旧包：(result, elapse)
        for item in (raw[0] or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                triples.append((item[0], item[1], item[2]))
    else:
        # 新包：RapidOCROutput 对象（或任何带 boxes/txts/scores 的东西）
        #
        # ⚠️ 这里**不能用 `getattr(...) or []`** —— boxes 是 numpy 数组，
        #    对它做真值判断会抛：
        #      ValueError: The truth value of an array with more than one
        #                  element is ambiguous.
        #    用 `is None` 判断才是安全的。
        boxes = getattr(raw, "boxes", None)
        txts = getattr(raw, "txts", None)
        scores = getattr(raw, "scores", None)
        if boxes is None:
            boxes = []
        if txts is None:
            txts = []
        if scores is None:
            scores = []
        for box, text, score in zip(boxes, txts, scores):
            triples.append((box, text, score))

    lines = []
    from PIL import Image
    with stage("ocr_image_info"):
        with Image.open(img_path) as source:
            img_w, img_h = source.size
    conversion_started = time.perf_counter()
    for box, text, score in triples:
        # ⚠️ box 的三种形态都要认（都是实测见过的）：
        #   ① 新包：numpy 数组，shape=(4,2)  -> [[x,y],[x,y],[x,y],[x,y]]
        #   ② 旧包：list of list of list      -> [[[x,y],...]]
        #   ③ 旧包偶尔：扁平 8 个数            -> [x1,y1,x2,y2,x3,y3,x4,y4]
        #
        # 第一版用 `isinstance(pts[0], (list, tuple))` 判断，
        # 对 numpy 数组恒为 False（元素是 np.ndarray，不是 list），
        # 于是错误地走进"扁平化"分支，抛：
        #   TypeError: only 0-dimensional arrays can be converted to Python scalars
        # 所以**不猜类型，直接尝试按点对解析**：能取出 x,y 就用，取不出再扁平化。
        def _points(b) -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            try:
                for item in b:
                    try:
                        out.append((float(item[0]), float(item[1])))
                    except (TypeError, IndexError, ValueError):
                        out = []
                        break
            except TypeError:
                out = []
            if out:
                return out
            # 扁平形态：[x1,y1,x2,y2,...]
            try:
                flat = [float(v) for v in b]
            except (TypeError, ValueError):
                return []
            return [(flat[i], flat[i + 1])
                    for i in range(0, len(flat) - 1, 2)]

        pts = _points(box)
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        cx, cy = int(sum(xs) / len(xs)), int(sum(ys) / len(ys))
        lines.append({
            "text": str(text),
            "score": round(float(score), 3),
            "center": [cx, cy],
            "norm": [round(cx / img_w, 4), round(cy / img_h, 4)],  # 屏宽高百分比，跨设备可移植
            "bounds": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
        })

    if args.region:
        x1, y1, x2, y2 = map(int, args.region.split(","))
        lines = [l for l in lines
                 if x1 <= l["center"][0] <= x2 and y1 <= l["center"][1] <= y2]

    from runtime import STAGES
    STAGES["ocr_result_conversion"] = (time.perf_counter() - conversion_started) * 1000
    duration_ms = round((time.perf_counter() - t0) * 1000)
    log_auto("read", "read",
             f"OCR {len(lines)} 行" + (f" @ {args.region}" if args.region else ""),
             {"lines": [l["text"] for l in lines],
              "duration_scope": "core OCR including initialization/acquisition/conversion; excludes connection, event write and response",
              "ocr_library_preprocessing_ms": None}, evidence, duration_ms, started_at)
    emit(True, {"image": str(img_path), "lines": lines,
                "hint": "center 可直接用于 act.py tap；toast 断言请在 act 后尽快 read（浮层会消失）"})


if __name__ == "__main__":
    main()

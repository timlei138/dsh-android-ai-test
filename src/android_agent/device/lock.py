"""设备级互斥锁 —— 同一台设备同一时刻只允许一条用例在跑。

为什么需要（2026-10 实测踩到）：

    用户派了两个子 Agent 同时跑两条用例，两个 `run_case_json.py` 进程
    同时操作 `<设备序列号>`，后果是**一堆看起来像产品 bug 的假失败**：

      · 起点检查失败：期望 `.timetable.display.TimetableActivity`，
        实际 `.AllInOneActivity` —— 因为另一条用例把页面翻走了
      · 点了没反应：连点 `btnCreateManually` 两次无变化
      · 判定卡死：连续 3 次点同一坐标无效，被迫终止
      · activity 在两条用例之间来回跳

    这些**全都不是被测 App 的问题**，是两条用例在抢同一台设备。
    最恶劣的是它们不会报"冲突"，只会产出一份**看起来正常的错误报告** ——
    这种假失败比直接报错危险得多。

设计：
    · 粒度是**设备序列号**（不是全局单锁）—— 有两台设备时应该能并行
    · 文件锁（msvcrt / fcntl），进程崩了由操作系统自动释放，不会留死锁
    · 拿不到锁时**等**（设备是独占资源，排队才是正常语义），
      但要打印"在等谁"，否则使用者会以为卡死了
    · 超过 max_wait 仍拿不到 -> 抛 DeviceBusy，让调用方明确失败，
      **不要**默默往下跑（那就是回到原来出问题的状态）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 锁文件放这里，和 runner 源码不同目录 —— 避免被当成资产清理掉
LOCK_DIR = Path(__file__).resolve().parent / ".locks"


class DeviceBusy(RuntimeError):
    """等锁超时：设备被别人占着。"""


def _lock_path(serial: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in serial)
    return LOCK_DIR / f"device_{safe}.lock"


class DeviceLock:
    """用法：

        with DeviceLock(serial) as lk:
            lk.waited()      # 等了多久（秒），可写进报告
            ... 跑用例 ...

    拿不到锁且超时 -> 抛 DeviceBusy。
    """

    def __init__(self, serial: str, *, max_wait_s: float = 1800.0,
                 poll_s: float = 1.0, verbose: bool = True) -> None:
        self.serial = str(serial or "").strip() or "unknown"
        self.max_wait_s = max_wait_s
        self.poll_s = poll_s
        self.verbose = verbose
        self._fh = None
        self._waited = 0.0

    # ── 内部 ───────────────────────────────────────────────
    def _try_acquire(self) -> bool:
        """尝试拿锁，成功返回 True。不阻塞。"""
        path = _lock_path(self.serial)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+b")
        except OSError:
            # 建不了锁文件（只读盘等）-> 退化：不锁，但要吼一声
            if self.verbose:
                print(f"[lock] ⚠️ 无法创建锁文件 {path}，本设备不做互斥",
                      file=sys.stderr, flush=True)
            self._fh = None
            return True

        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        self._fh = fh
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                     .encode("utf-8"))
            fh.flush()
        except OSError:
            pass
        return True

    def _holder_hint(self) -> str:
        """尽量读出当前持有者，纯粹为了让人看懂在等谁。"""
        try:
            txt = _lock_path(self.serial).read_text(encoding="utf-8",
                                                    errors="replace").strip()
            return f"（{txt}）" if txt else ""
        except OSError:
            return ""

    # ── 对外 ───────────────────────────────────────────────
    def acquire(self) -> "DeviceLock":
        t0 = time.time()
        announced = False
        while True:
            if self._try_acquire():
                self._waited = time.time() - t0
                if announced and self.verbose:
                    print(f"[lock] ✅ 拿到设备 {self.serial} 的锁"
                          f"（等了 {self._waited:.1f}s）", file=sys.stderr, flush=True)
                return self

            if not announced and self.verbose:
                print(f"[lock] ⏳ 设备 {self.serial} 正被另一条用例使用"
                      f"{self._holder_hint()}，排队等待…", file=sys.stderr, flush=True)
                announced = True

            if time.time() - t0 > self.max_wait_s:
                raise DeviceBusy(
                    f"设备 {self.serial} 已被其他用例占用超过 "
                    f"{self.max_wait_s:.0f}s，本次不执行。\n"
                    f"  同一台设备**不能并发跑用例** —— 两条用例会互相翻页，"
                    f"产出看起来正常、实际是假的失败报告。\n"
                    f"  请等前一条跑完，或改用另一台设备。"
                    f"{self._holder_hint()}")
            time.sleep(self.poll_s)

    def waited(self) -> float:
        return self._waited

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __enter__(self) -> "DeviceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

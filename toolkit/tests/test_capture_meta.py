# -*- coding: utf-8 -*-
"""`tools/common.py` 的 `capture()` —— meta.json 必须记 `execution_state`。

## 为什么（N-38，真实数据）

`meta.json` 是证据目录里**唯一长期留存**的「这一步是怎么来的」记录。
`execution_state`（`executed` / `may_have_executed` / `unknown`）原来只写进
`storage/calls/<sid>/<call_id>.json` 那个账本，而账本是按调用分片的、且没有
和证据目录的对应关系。

后果很具体：事后单看 `run_history/<run>/evidence/<sid>/step12/`，无法判断
那一帧是「动作做完之后拍的」还是「不确定做没做（超时/中断）」。而这两者的
结论强度完全不同 —— 真的库里 6244 份 meta.json 里 **0 份**有这个字段。

`capture()` 里的 `runtime.EXECUTION_STATE` 是模块级全局，`act.py` 在
`capture()` **之前**就会 `execution("executed")` / `execution("may_have_executed")`，
所以拿到的是本次动作的真实状态。

不需要设备：把 `_dump_hierarchy` / `_rotation` / `foreground_of` 换成桩函数，
`d.screenshot()` 用假对象即可。仍然是纯函数级验证。
"""
import io
import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402
import runtime as RT  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def _tmp_parent() -> str:
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()


class _Shot:
    size = (1080, 1920)

    def save(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")


class _Dev:
    def screenshot(self):
        return _Shot()


class _Stubs:
    """把 capture() 的三个外部依赖换成确定性桩。"""

    def __enter__(self):
        self._saved = (common._dump_hierarchy, common._rotation, common.foreground_of)
        common._dump_hierarchy = lambda d, xml, timeout=20.0: ("<hierarchy/>", 1)
        common._rotation = lambda d, shot=None: 0
        common.foreground_of = lambda d: {
            "package": "com.zui.calendar",
            "activity": "com.zui.calendar.AllInOneActivity",
        }
        return self

    def __exit__(self, *exc):
        (common._dump_hierarchy, common._rotation,
         common.foreground_of) = self._saved
        return False


class _Dir:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.path = Path(self._tmp.name)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


def _capture(tmp: Path, state: str) -> dict:
    RT.execution(state)
    png, xml, meta = tmp / "shot.png", tmp / "dump.xml", tmp / "meta.json"
    info = common.capture(_Dev(), png, xml, meta)
    on_disk = json.loads(meta.read_text(encoding="utf-8"))
    # 返回值和落盘内容必须一致 —— 调用方拿返回值，事后复盘拿文件
    assert on_disk.get("execution_state") == info.get("execution_state"), (
        on_disk.get("execution_state"), info.get("execution_state"))
    return on_disk


@case("★ meta.json 里必须有 execution_state 字段（N-38 的原缺陷：0/6244）")
def _():
    with _Dir() as d, _Stubs():
        meta = _capture(d.path, "executed")
        assert "execution_state" in meta, sorted(meta)
        assert meta["execution_state"] == "executed"


@case("三种状态原样落盘（不是一律写 unknown）")
def _():
    with _Dir() as d, _Stubs():
        for state in ("executed", "may_have_executed", "unknown"):
            meta = _capture(d.path, state)
            assert meta["execution_state"] == state, (state, meta["execution_state"])


@case("★ may_have_executed 要能区分出来（这是「可能做了也可能没做」的唯一痕迹）")
def _():
    with _Dir() as d, _Stubs():
        meta = _capture(d.path, "may_have_executed")
        assert meta["execution_state"] != "executed"
        assert meta["execution_state"] == "may_have_executed"


@case("未显式设置过 → unknown（默认值，不能凭空写 executed）")
def _():
    with _Dir() as d, _Stubs():
        saved = RT.EXECUTION_STATE
        try:
            RT.EXECUTION_STATE = "unknown"
            meta = _capture(d.path, "unknown")
            assert meta["execution_state"] == "unknown"
        finally:
            RT.EXECUTION_STATE = saved


@case("原有的 meta 字段没被挤掉（package/activity/rotation/size/ts/call_id/timing 都在）")
def _():
    with _Dir() as d, _Stubs():
        meta = _capture(d.path, "executed")
        for key in ("ts", "package", "activity", "rotation", "size",
                    "call_id", "timing", "execution_state"):
            assert key in meta, f"meta 缺字段 {key}：{sorted(meta)}"
        assert meta["package"] == "com.zui.calendar"
        assert meta["activity"] == "com.zui.calendar.AllInOneActivity"
        assert meta["size"] == [1080, 1920]


@case("extra 参数仍能覆盖/追加字段（act / system 都靠它带 action）")
def _():
    with _Dir() as d, _Stubs():
        RT.execution("executed")
        png, xml, meta = d.path / "s.png", d.path / "d.xml", d.path / "m.json"
        info = common.capture(_Dev(), png, xml, meta,
                              extra={"action": {"type": "tap"}})
        assert info["action"] == {"type": "tap"}
        on_disk = json.loads(meta.read_text(encoding="utf-8"))
        assert on_disk["action"] == {"type": "tap"}
        assert on_disk["execution_state"] == "executed"


def main() -> int:
    print("=" * 70)
    print("tools/common.py —— capture() 的 meta.json")
    print("=" * 70)
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            print(f"  [FAIL] {name}  {error}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}  异常：{type(error).__name__}: {error}")
        else:
            print(f"  [PASS] {name}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

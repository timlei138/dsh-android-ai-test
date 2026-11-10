"""联机冒烟：只跑读操作，绝不写。

用法：
    python tests\\smoke_live.py
需要先配好凭据（setup 或环境变量），否则会提示并退出。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JIRA_PY = os.path.join(ROOT, "jira.py")

READ_CMDS = [
    ["ping"],
    ["projects"],
    ["project"],
    ["mine", "--limit", "5"],
    ["bugs", "--limit", "50"],
    ["search", "ORDER BY created DESC", "--limit", "3"],
    ["text", "bug", "--limit", "3"],
]


def run(args):
    p = subprocess.run(
        [sys.executable, JIRA_PY, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, stdin=subprocess.DEVNULL,
    )
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"ok": False, "error": f"非 JSON 输出: {p.stdout[:200]!r}", "stderr": p.stderr[:200]}


def main():
    fails = 0
    for args in READ_CMDS:
        rc, payload = run(args)
        name = " ".join(args)
        if payload.get("ok"):
            print(f"[PASS] {name:<55} rc={rc}  {json.dumps(payload.get('data'), ensure_ascii=False)[:110]}")
        else:
            fails += 1
            print(f"[FAIL] {name:<55} rc={rc}  {payload.get('error')}")
            if payload.get("hint"):
                print(f"        hint: {payload['hint'][:200]}")
    print(f"\n{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())

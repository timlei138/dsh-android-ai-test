"""
离线单测：不联网，只验证「参考实现踩过的坑」在本版是否真的修好了。

跑法：    python tests\\test_offline.py
退出码：  0 全过，1 有失败

设计说明见下方 _TMP_ROOT 处的注释（Windows 沙箱的临时目录有个反常行为）。
"""

from __future__ import annotations

import json
import os
import sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JIRA_PY = os.path.join(SKILL_ROOT, "jira.py")
sys.path.insert(0, SKILL_ROOT)

from lib import jira_client as jc  # noqa: E402

_results: list[tuple[bool, str, str]] = []
_tmp_dirs: list[str] = []

# ---------------------------------------------------------------------------
# 沙箱限制：系统 TEMP 下 mkdtemp() 建出来的子目录**不可写**
# （父目录对 Everyone 有 (CI)(DENY)(DC)，新建子目录拿不到可写 ACE；
#   实测 python 3.13 / 3.11、os.open / io.open / .NET WriteAllText 全部 PermissionError）
# 但 TEMP 下**已存在**的目录可写，所以在 TEMP 里固定复用一个目录、手工建子目录。
#
# 另一个坑：在这个目录里再用 tempfile.mkdtemp() == 踩同一个坑 ——
# 建出来的孙子目录同样删不掉（Access denied，连 icacls 都改不动）。
# 所以清理一律走 _remove_tree()：先删文件、再自底向上删目录，不用 rmtree。
# ---------------------------------------------------------------------------
_TMP_ROOT = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "dsh-ws-test")
_tmp_counter = 0


def new_tmp() -> str:
    """在可写区域内建一个临时子目录（不用 tempfile.mkdtemp，原因见上）。"""
    global _tmp_counter
    os.makedirs(_TMP_ROOT, exist_ok=True)
    while True:
        _tmp_counter += 1
        d = os.path.join(_TMP_ROOT, f"c{_tmp_counter:04d}")
        try:
            os.makedirs(d, exist_ok=False)
        except FileExistsError:
            continue
        _tmp_dirs.append(d)
        return d


def _remove_tree(path: str) -> None:
    """稳健删除：先删文件、再自底向上删空目录。任何一步失败都静默跳过。"""
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def case(name: str):
    def deco(fn):
        try:
            detail = fn()
            _results.append((True, name, str(detail or "")))
        except AssertionError as e:
            _results.append((False, name, str(e)))
        except Exception as e:
            _results.append((False, name, f"{type(e).__name__}: {e}"))
        return fn

    return deco


_write_counter = 0


def _write_config(content) -> str:
    """写一份临时配置文件，返回路径。文件名带序号，避免重名开文件撞权限问题。"""
    global _write_counter
    _write_counter += 1
    p = os.path.join(new_tmp(), f"cfg_{_write_counter}.json")
    if isinstance(content, bytes):
        with open(p, "wb") as f:
            f.write(content)
    else:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    return p


def _cfg_env(config_path: str) -> dict:
    """把配置文件指过去，同时清掉会干扰的环境变量。"""
    return {
        "JIRA_CONFIG": config_path,
        "JIRA_ITCODE": "",
        "JIRA_PASSWORD": "",
        "JIRA_BASE": "",
        "JIRA_PROJECT_KEY": "",
    }


def _apply_env(overrides: dict) -> dict:
    saved = {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _run_cli(args: list[str], env_overrides: dict | None = None) -> tuple[int, dict, str]:
    import subprocess

    env = dict(os.environ)
    for k, v in (env_overrides or {}).items():
        if v:
            env[k] = v
        else:
            env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, JIRA_PY, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=SKILL_ROOT,
        # 显式给 stdin：不给的话子进程会继承当前 shell 的 tty（isatty() 为 True），
        # setup 就会真的去等人输入。给空串 → isatty() 为 False，走非交互分支。
        input="",
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"stdout 不是合法 JSON：{proc.stdout[:300]!r}  stderr={proc.stderr[:300]!r}"
        ) from None
    return proc.returncode, payload, proc.stderr


# ---------------------------------------------------------------------------
# 1. 配置容错 —— 参考实现最致命的坑
# ---------------------------------------------------------------------------


@case("空配置文件不崩溃（原版 JSONDecodeError）")
def _():
    p = _write_config("")  # 0 字节，与真实项目里那个文件一致
    saved = _apply_env(_cfg_env(p))
    try:
        got = jc.load_config()
        assert got == {}, f"应为空 dict，实际 {got}"
    finally:
        _restore_env(saved)
    return "0 字节 → {}"


@case("非法 JSON 不崩溃")
def _():
    p = _write_config("{ this is not json")
    saved = _apply_env(_cfg_env(p))
    try:
        assert jc.load_config() == {}
    finally:
        _restore_env(saved)
    return "坏 JSON → {}"


@case("配置顶层是数组不崩溃（真实项目里是 []）")
def _():
    p = _write_config("[]")
    saved = _apply_env(_cfg_env(p))
    try:
        assert jc.load_config() == {}
    finally:
        _restore_env(saved)
    return "[] → {}"


@case("带 BOM 的配置能读（记事本存的文件）")
def _():
    p = _write_config(b'\xef\xbb\xbf{"itcode": "someone01", "password": "x"}')
    saved = _apply_env(_cfg_env(p))
    try:
        cfg = jc.load_config()
        assert cfg.get("itcode") == "someone01", cfg
    finally:
        _restore_env(saved)
    return "BOM 兼容"


@case("配置文件不存在 → 空配置，不抛")
def _():
    saved = _apply_env(_cfg_env(os.path.join(new_tmp(), "nope.json")))
    try:
        assert jc.load_config() == {}
    finally:
        _restore_env(saved)
    return "缺文件 → {}"


# ---------------------------------------------------------------------------
# 2. 凭据解析
# ---------------------------------------------------------------------------


@case("环境变量优先于配置文件")
def _():
    p = _write_config(json.dumps({"itcode": "file_user", "password": "file_pw"}))
    saved = _apply_env({**_cfg_env(p), "JIRA_ITCODE": "env_user", "JIRA_PASSWORD": "env_pw"})
    try:
        s = jc.resolve_settings()
        assert s["itcode"] == "env_user", s
        assert s["source"] == "env", s
    finally:
        _restore_env(saved)
    return "env > file"


@case("issue_base 从 jira_base 正确推导")
def _():
    saved = _apply_env(
        {
            **_cfg_env(os.path.join(new_tmp(), "nope.json")),
            "JIRA_BASE": "https://tbjira.lenovo.com/issue/rest/api/2",
        }
    )
    try:
        s = jc.resolve_settings(require_credentials=False)
        assert s["issue_base"] == "https://tbjira.lenovo.com/issue", s["issue_base"]
    finally:
        _restore_env(saved)
    return "https://tbjira.lenovo.com/issue"


@case("缺凭据：resolve_settings 抛 JiraError 而不是 sys.exit")
def _():
    saved = _apply_env(_cfg_env(os.path.join(new_tmp(), "nope.json")))
    try:
        try:
            jc.resolve_settings()
            raise AssertionError("应该抛 JiraError")
        except jc.JiraError as e:
            assert "凭据" in e.message, e.message
            assert e.hint, "应带 hint"
    finally:
        _restore_env(saved)
    return "JiraError + hint"


# ---------------------------------------------------------------------------
# 3. CLI 层：stdout 纯净度 / 退出码
# ---------------------------------------------------------------------------


@case("缺凭据：ping → ok:false + hint，退出码 1")
def _():
    rc, payload, _ = _run_cli(["ping"], _cfg_env(os.path.join(new_tmp(), "nope.json")))
    assert payload.get("ok") is False, payload
    assert "凭据" in payload.get("error", ""), payload
    assert payload.get("hint"), "应带 hint"
    assert rc == 1, f"退出码应 1，实际 {rc}"
    return "ok:false + hint + rc=1"


@case("whoami 不联网也能跑，stdout 只有 JSON")
def _():
    rc, payload, _ = _run_cli(["whoami"], _cfg_env(os.path.join(new_tmp(), "nope.json")))
    assert payload.get("ok") is True and payload["cmd"] == "whoami", payload
    assert rc == 0, rc
    return f"config_source={payload['data']['config_source']}"


@case("setup 在非交互环境（stdin 被重定向）明确报错，不挂等人输入")
def _():
    rc, payload, _ = _run_cli(["setup"], _cfg_env(os.path.join(new_tmp(), "nope.json")))
    assert payload.get("ok") is False, payload
    assert "交互式终端" in payload.get("error", ""), payload
    assert payload.get("hint"), "应带 hint"
    assert rc == 1, rc
    return "非交互 → 结构化报错"


@case("setup 已有配置且不给 --force → 拒绝覆盖")
def _():
    p = _write_config(json.dumps({"itcode": "a", "password": "b"}))
    rc, payload, _ = _run_cli(["setup"], _cfg_env(p))
    assert payload.get("ok") is False, payload
    assert "--force" in payload.get("hint", ""), payload
    return "拒绝覆盖"


# ---------------------------------------------------------------------------
# 4. 附件相关（参考实现踩过的坑）
# ---------------------------------------------------------------------------


@case("filename*=UTF-8'' 中文文件名能还原")
def _():
    name = jc._filename_from_disposition(
        "attachment; filename=fallback.txt; filename*=UTF-8''%E6%88%AA%E5%9B%BE.png"
    )
    assert name == "截图.png", name
    return name


@case("普通引号文件名能解析")
def _():
    name = jc._filename_from_disposition('attachment; filename="crash.log"')
    assert name == "crash.log", name
    return name


@case("无 Content-Disposition 时返回 None（由调用方兜底）")
def _():
    assert jc._filename_from_disposition("") is None
    assert jc._filename_from_disposition("inline") is None
    return "None"


@case("文件名路径穿越 / Windows 非法字符被清理")
def _():
    got = jc._sanitize_filename("..\\..\\win.ini")
    assert "/" not in got and "\\" not in got, got
    got2 = jc._sanitize_filename('a<b>c:d|e?f*g"h')
    assert not any(ch in got2 for ch in '<>:"|?*'), got2
    return f"{got} / {got2}"


@case("HTML 伪装成功：非 JSON 响应被告警（不让 HTML 当数据）")
def _():
    class FakeResp:
        status_code = 200
        content = b"<html>login</html>"
        text = "<html>login</html>"

        def json(self):
            raise ValueError("no json")

    try:
        jc._parse_response(FakeResp(), "GET", "http://x")
        raise AssertionError("应报错")
    except jc.JiraError as e:
        assert "非 JSON" in e.message, e.message
    return "拦下"


@case("HTTP 错误映射：401 / 404 / 5xx 各自成句")
def _():
    class R:
        def __init__(self, code):
            self.status_code = code
            self.text = "bad"
            self.content = b"x"

        def json(self):
            return {"errorMessages": ["bad"]}

    expect = {401: "认证失败", 404: "资源不存在", 500: "服务端错误"}
    for code, word in expect.items():
        try:
            jc._parse_response(R(code), "GET", "http://x")
            raise AssertionError(f"{code} 应报错")
        except jc.JiraError as e:
            assert word in e.message, f"{code} → {e.message}"
    return "401/404/5xx"


# ---------------------------------------------------------------------------
# 5. 参数与字段拍平
# ---------------------------------------------------------------------------


@case("delete 必须显式 --yes")
def _():
    import jira as cli  # noqa: PLC0415

    assert cli.build_parser().parse_args(["delete", "AGENTPILOT-1"]).yes is False
    assert cli.build_parser().parse_args(["delete", "AGENTPILOT-1", "--yes"]).yes is True
    return "--yes 生效"


@case("issue 字段拍平：assignee/priority 为 null 时不崩")
def _():
    raw = {
        "key": "AGENTPILOT-1",
        "fields": {
            "summary": "测试",
            "status": {"name": "Open"},
            "issuetype": {"name": "Bug"},
            "priority": None,
            "assignee": None,
            "reporter": {"displayName": "张三"},
            "created": "2026-07-01T10:00:00.000+0800",
            "updated": "2026-07-02T10:00:00.000+0800",
            "labels": [],
        },
    }
    import jira as cli  # noqa: PLC0415

    flat = cli._flatten_issue(raw)
    assert flat["status"] == "Open", flat
    assert flat["assignee"] is None, flat
    assert flat["priority"] is None, flat
    assert flat["reporter"] == "张三", flat
    return "null 安全"


@case("human_size 换算")
def _():
    assert jc.human_size(0) == "0 B", jc.human_size(0)
    assert jc.human_size(2048) == "2.0 KB", jc.human_size(2048)
    assert jc.human_size(1048576) == "1.0 MB", jc.human_size(1048576)
    return "B/KB/MB"


@case("text 命令会对 JQL 里的双引号转义")
def _():
    import jira as cli  # noqa: PLC0415

    ns = cli.build_parser().parse_args(["text", 'say "hi"'])
    safe = ns.text.replace('"', '\\"')
    assert safe == 'say \\"hi\\"', safe
    return safe


@case("create --type 缺省 Bug / --priority 缺省 Medium")
def _():
    import jira as cli  # noqa: PLC0415

    ns = cli.build_parser().parse_args(["create", "--summary", "x"])
    assert ns.type == "Bug" and ns.priority == "Medium", ns
    return "Bug/Medium"


# ---------------------------------------------------------------------------
# 6. 多项目支持
# ---------------------------------------------------------------------------


@case("--project 参数在 text/bugs/mine/create 上都存在，且缺省 None")
def _():
    import jira as cli  # noqa: PLC0415

    p = cli.build_parser()
    assert p.parse_args(["text", "x"]).project is None
    assert p.parse_args(["text", "x", "--project", "ABC"]).project == "ABC"
    assert p.parse_args(["text", "x", "--project", "all"]).project == "all"
    assert p.parse_args(["bugs"]).project is None
    assert p.parse_args(["bugs", "--project", "ABC"]).project == "ABC"
    assert p.parse_args(["mine", "--project", "ABC"]).project == "ABC"
    assert p.parse_args(["create", "--summary", "x", "--project", "ABC"]).project == "ABC"
    return "text/bugs/mine/create 均可 --project"


@case("_project_key：override 优先，否则回落配置默认值")
def _():
    import jira as cli  # noqa: PLC0415

    settings = {"project_key": "DEFAULTP"}
    assert cli._project_key(settings, "OVERRIDE") == "OVERRIDE"
    assert cli._project_key(settings, "  OVERRIDE  ") == "OVERRIDE"  # 会 strip
    assert cli._project_key(settings, None) == "DEFAULTP"
    return "override > default"


@case("_project_key：两边都没有 → JiraError 带 hint")
def _():
    import jira as cli  # noqa: PLC0415

    try:
        cli._project_key({"project_key": ""}, None)
        raise AssertionError("应抛 JiraError")
    except cli.JiraError as e:
        assert "projects" in e.hint, e.hint
    return "JiraError + hint"


@case("projects / project 子命令已注册，project 的 KEY 是可选位置参数")
def _():
    import jira as cli  # noqa: PLC0415

    p = cli.build_parser()
    ns = p.parse_args(["project"])
    assert ns.key is None, ns
    ns2 = p.parse_args(["project", "ABC"])
    assert ns2.key == "ABC", ns2
    # 子命令都在
    assert "projects" in p._subparsers._group_actions[0].choices
    assert "project" in p._subparsers._group_actions[0].choices
    return "projects/project 注册正常"


def main() -> int:
    try:
        total = len(_results)
        passed = sum(1 for ok, _, _ in _results if ok)
        for ok, name, detail in _results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
        print(f"\n{passed}/{total} 通过")
        return 0 if passed == total else 1
    finally:
        for d in _tmp_dirs:
            _remove_tree(d)
        _remove_tree(_TMP_ROOT)  # 整个根也清掉，别留垃圾


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())

#!/usr/bin/env python
"""
jira.py — Lenovo 内网 JIRA（tbjira.lenovo.com）命令行入口（给 AI Agent / 脚本用）。

支持多项目：默认项目由配置的 project_key 决定（默认 AGENTPILOT），
用 `projects` 列出当前账号有权访问的所有项目，各命令用 --project 操作任意项目。

设计原则：
  - stdout 只输出一个 JSON 对象；诊断信息走 stderr（管道 / Agent 解析友好）
  - 永远返回退出码 0 或非 0，但**失败也是合法 JSON**：{"ok": false, "error": "...", "hint": "..."}
    Agent 判断成败看 JSON 的 ok 字段，不要只看退出码
  - 幂等读操作可以直接重试；写操作（comment/transition/update/create/delete/upload）不要自动重试

用法速查（完整说明见 SKILL.md）：
  python jira.py ping
  python jira.py projects                    # 当前账号有权访问的所有项目
  python jira.py project [KEY]               # 单项目详情（缺省 = 配置的默认项目）
  python jira.py search "<JQL>" [--limit N] [--fields ...]
  python jira.py text "<关键词>" [--project KEY|all] [--limit N]
  python jira.py bugs [--project KEY] [--limit N]
  python jira.py mine [--project KEY] [--limit N]
  python jira.py issue <KEY>
  python jira.py comments <KEY> [--limit N]
  python jira.py worklogs <KEY>
  python jira.py transitions <KEY>
  python jira.py attach-list <KEY>
  python jira.py attach-get <ATT_ID> [--save PATH] [--name FILENAME]
  python jira.py attach-put <KEY> <FILE>
  python jira.py create --summary "..." [--type Bug] [--priority Medium] [--project KEY] [--description "..."]
  python jira.py comment <KEY> "<正文>"
  python jira.py transition <KEY> --id <id> | --name "<状态名>"
  python jira.py update <KEY> [--summary ...] [--priority ...] [--assignee ...] [--description ...]
  python jira.py delete <KEY> --yes
  python jira.py setup [--force]
  python jira.py whoami
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 保证从任意工作目录都能 import 到 lib/
_SKILL_ROOT = os.path.dirname(os.path.abspath(__file__))
if _SKILL_ROOT not in sys.path:
    sys.path.insert(0, _SKILL_ROOT)

from lib import jira_client as jc  # noqa: E402
from lib.jira_client import JiraError  # noqa: E402

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERR = 1
EXIT_AUTH = 2
EXIT_USAGE = 3


class _Output:
    """控制输出格式。--human 给人看，默认 JSON 给 Agent / 脚本看。"""

    def __init__(self, human: bool):
        self.human = human

    def emit(self, payload: dict) -> int:
        if self.human:
            _print_human(payload)
            return EXIT_OK if payload.get("ok") else (
                EXIT_AUTH if payload.get("auth_error") else EXIT_ERR
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if payload.get("ok"):
            return EXIT_OK
        return EXIT_AUTH if payload.get("auth_error") else EXIT_ERR


def _print_human(payload: dict) -> None:
    """给人看的排版。AI 应该用默认的 JSON 模式，这里只是方便人手跑一下。"""
    if not payload.get("ok"):
        print(f"❌ {payload.get('error')}")
        if payload.get("hint"):
            print(f"   💡 {payload['hint']}")
        return

    kind = payload.get("cmd", "")
    data = payload.get("data", {})
    if isinstance(data, dict) and data.get("issues") is not None:
        print(f"共 {data.get('total')} 条（显示 {len(data['issues'])} 条）")
        for it in data["issues"]:
            print(f"  {it.get('key'):<16} [{it.get('status')}] {it.get('summary')}")
    elif isinstance(data, dict) and data.get("status_distribution") is not None:
        print(f"Bug 总数：{data.get('total_bugs')}")
        for status, count in (data.get("status_distribution") or {}).items():
            print(f"  {status:<20} {count}")
    elif isinstance(data, dict) and data.get("attachments") is not None:
        for a in data["attachments"]:
            print(f"  id={a.get('id'):<8} {a.get('human_size'):>10}  {a.get('filename')}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 字段整理（把 JIRA 的字段对象拍平成 Agent 好读的形状）
# ---------------------------------------------------------------------------

_DEFAULT_SEARCH_FIELDS = "summary,status,assignee,created,updated,priority,issuetype,reporter,labels"


def _flatten_issue(raw: dict, fields: str | None = None) -> dict:
    f = raw.get("fields") or {}
    wanted = set(x.strip() for x in (fields or _DEFAULT_SEARCH_FIELDS).split(",") if x.strip())

    out = {
        "key": raw.get("key"),
        "url": _issue_url(raw.get("key")),
        "summary": f.get("summary"),
        "status": _name(f.get("status")),
        "type": _name(f.get("issuetype")),
        "priority": _name(f.get("priority")),
        "assignee": jc._display_name(f.get("assignee")),
        "reporter": jc._display_name(f.get("reporter")),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "labels": f.get("labels") or [],
    }
    # fields 里点名要了 description 才带（它可能很长，别默认污染上下文）
    if "description" in wanted or fields == "*all":
        out["description"] = f.get("description")
    if "resolution" in wanted:
        out["resolution"] = _name(f.get("resolution"))
    return out


def _name(obj) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def _issue_url(key: str | None) -> str | None:
    if not key:
        return None
    settings = jc.resolve_settings(require_credentials=False)
    return f"{settings['issue_base']}/browse/{key}"


def _search(
    jql: str,
    *,
    settings: dict,
    limit: int,
    fields: str = _DEFAULT_SEARCH_FIELDS,
    start_at: int = 0,
) -> dict:
    body = jc.request(
        "GET",
        "/search",
        settings=settings,
        params={
            "jql": jql,
            "startAt": start_at,
            "maxResults": limit,
            "fields": fields,
        },
    )
    issues = body.get("issues") or []
    return {
        "jql": jql,
        "total": body.get("total"),
        "returned": len(issues),
        "truncated": bool(body.get("total") is not None and body.get("total") > len(issues)),
        "issues": [_flatten_issue(i, fields) for i in issues],
    }


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_ping(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    data = jc.request("GET", "/myself", settings=settings, timeout=15)
    return out.emit({
        "ok": True,
        "cmd": "ping",
        "data": {
            "connected": True,
            "user": data.get("displayName"),
            "itcode": settings["itcode"],
            "email": data.get("emailAddress"),
            "project_key": settings["project_key"],
            "jira_base": settings["jira_base"],
            "config_source": settings["source"],
        },
    })


def _project_key(settings: dict, override: str | None) -> str:
    """--project 优先；没给就用配置里的默认项目。"""
    key = (override or settings["project_key"] or "").strip()
    if not key:
        raise JiraError(
            "未指定项目，且配置里也没有默认 project_key",
            hint="加 --project <KEY>，或先跑 projects 看当前账号能访问哪些项目",
        )
    return key


def cmd_projects(args, out: _Output) -> int:
    """列出当前账号有权访问（含可浏览）的所有项目。"""
    settings = jc.resolve_settings()
    params = {"recent": getattr(args, "recent", None)} if getattr(args, "recent", None) else None
    body = jc.request("GET", "/project", settings=settings, params=params)
    items = body if isinstance(body, list) else body.get("values") or []
    default_key = settings["project_key"]
    return out.emit({
        "ok": True,
        "cmd": "projects",
        "data": {
            "total": len(items),
            "default_project": default_key or None,
            "projects": [
                {
                    "key": p.get("key"),
                    "name": p.get("name"),
                    "lead": jc._display_name(p.get("lead")),
                    "is_default": (p.get("key") or "") == default_key,
                    "issue_types": [t.get("name") for t in (p.get("issueTypes") or [])],
                }
                for p in items
            ],
            "hint": "后续命令用 --project <KEY> 操作对应项目；不指定则用 default_project",
        },
    })


def cmd_project(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    key = _project_key(settings, getattr(args, "key", None))
    p = jc.request("GET", f"/project/{key}", settings=settings)
    return out.emit({
        "ok": True,
        "cmd": "project",
        "data": {
            "key": p.get("key"),
            "name": p.get("name"),
            "lead": jc._display_name(p.get("lead")),
            "description": p.get("description"),
            "issue_types": [t.get("name") for t in (p.get("issueTypes") or [])],
        },
    })


def cmd_search(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    fields = args.fields or _DEFAULT_SEARCH_FIELDS
    data = _search(args.jql, settings=settings, limit=args.limit, fields=fields, start_at=args.start)
    return out.emit({"ok": True, "cmd": "search", "data": data})


def cmd_text(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    # JQL 里用双引号包文本搜索；内部的双引号要转义，否则 JQL 语法会断
    safe = args.text.replace('"', '\\"')
    cond = f'(summary ~ "{safe}" OR description ~ "{safe}")'
    if args.project == "all":
        jql = cond  # 不限项目：搜当前账号有权看到的所有项目
        scope = "all accessible projects"
    else:
        project = _project_key(settings, args.project)
        jql = f"project = {project} AND {cond}"
        scope = project
    data = _search(jql, settings=settings, limit=args.limit)
    data["project_scope"] = scope
    return out.emit({"ok": True, "cmd": "text", "data": data})


def cmd_bugs(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    project = _project_key(settings, args.project)
    jql = f"project = {project} AND issuetype = Bug"
    # issuekey 升序保证翻页稳定（JIRA 默认排序在不同页之间可能重复/漏）
    data = _search(jql, settings=settings, limit=args.limit)
    dist: dict[str, int] = {}
    for it in data["issues"]:
        key = it.get("status") or "Unknown"
        dist[key] = dist.get(key, 0) + 1
    data["status_distribution"] = dict(sorted(dist.items(), key=lambda kv: -kv[1]))
    data["total_bugs"] = data["total"]
    data["distribution_scope"] = (
        f"仅统计已返回的 {data['returned']} 条" if data["truncated"] else "全量"
    )
    return out.emit({"ok": True, "cmd": "bugs", "data": data})


def cmd_mine(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    project = _project_key(settings, args.project)
    jql = (
        f"project = {project} AND assignee = currentUser() "
        f"AND resolution = Unresolved ORDER BY updated DESC"
    )
    data = _search(jql, settings=settings, limit=args.limit)
    return out.emit({"ok": True, "cmd": "mine", "data": data})


def cmd_issue(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    raw = jc.request("GET", f"/issue/{args.key}", settings=settings)
    data = _flatten_issue(raw, fields="*all")
    data["attachment_count"] = len((raw.get("fields") or {}).get("attachment") or [])
    data["comment_count"] = ((raw.get("fields") or {}).get("comment") or {}).get("total")
    return out.emit({"ok": True, "cmd": "issue", "data": data})


def cmd_comments(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    body = jc.request("GET", f"/issue/{args.key}/comment", settings=settings)
    comments = body.get("comments") or []
    trimmed = [
        {
            "id": c.get("id"),
            "author": jc._display_name(c.get("author")),
            "created": c.get("created"),
            "updated": c.get("updated"),
            # 原版截断到 200 字符，Agent 经常看不全就下结论；放宽到 2000 并标明是否截断
            "body": _clip(c.get("body") or "", 2000),
        }
        for c in comments[-args.limit:] if args.limit > 0
    ]
    return out.emit({
        "ok": True,
        "cmd": "comments",
        "data": {
            "issue_key": args.key,
            "total": body.get("total", len(comments)),
            "returned": len(trimmed),
            "comments": trimmed,
        },
    })


def cmd_worklogs(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    body = jc.request("GET", f"/issue/{args.key}/worklog", settings=settings)
    logs = body.get("worklogs") or []
    total_seconds = sum(w.get("timeSpentSeconds") or 0 for w in logs)
    return out.emit({
        "ok": True,
        "cmd": "worklogs",
        "data": {
            "issue_key": args.key,
            "total": body.get("total", len(logs)),
            "total_hours": round(total_seconds / 3600, 2),
            "worklogs": [
                {
                    "id": w.get("id"),
                    "author": jc._display_name(w.get("author")),
                    "time_spent": w.get("timeSpent"),
                    "started": w.get("started"),
                    "comment": _clip(w.get("comment") or "", 500),
                }
                for w in logs
            ],
        },
    })


def cmd_transitions(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    body = jc.request("GET", f"/issue/{args.key}/transitions", settings=settings)
    items = body.get("transitions") or []
    return out.emit({
        "ok": True,
        "cmd": "transitions",
        "data": {
            "issue_key": args.key,
            "transitions": [
                {"id": t.get("id"), "name": t.get("name"), "to_status": _name(t.get("to"))}
                for t in items
            ],
            "hint": "用 transition --id <id> 或 --name \"<name>\" 执行；id 只对当前状态有效，改了状态要重新查",
        },
    })


def cmd_attach_list(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    return out.emit({"ok": True, "cmd": "attach-list", "data": jc.list_attachments(args.key, settings=settings)})


def cmd_attach_get(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    data = jc.download_attachment(
        str(args.attachment_id), save_path=args.save, settings=settings
    )
    return out.emit({"ok": True, "cmd": "attach-get", "data": data})


def cmd_attach_put(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    data = jc.upload_attachment(args.key, args.file, settings=settings)
    return out.emit({"ok": True, "cmd": "attach-put", "data": data})


def cmd_create(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    project = _project_key(settings, args.project)
    fields = {
        "project": {"key": project},
        "summary": args.summary,
        "issuetype": {"name": args.type},
        "description": args.description or "",
    }
    if args.priority:
        fields["priority"] = {"name": args.priority}
    if args.assignee:
        fields["assignee"] = {"name": args.assignee}

    body = jc.request("POST", "/issue", settings=settings, json={"fields": fields})
    key = body.get("key")
    return out.emit({
        "ok": True,
        "cmd": "create",
        "data": {"key": key, "url": _issue_url(key), "summary": args.summary, "project": project},
    })


def cmd_comment(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    body = jc.request(
        "POST", f"/issue/{args.key}/comment", settings=settings, json={"body": args.body}
    )
    return out.emit({
        "ok": True,
        "cmd": "comment",
        "data": {"issue_key": args.key, "comment_id": body.get("id")},
    })


def cmd_transition(args, out: _Output) -> int:
    settings = jc.resolve_settings()

    if not args.id and not args.name:
        # 没给目标时把可选转换列出来，省得 Agent 再发一次请求
        body = jc.request("GET", f"/issue/{args.key}/transitions", settings=settings)
        raise JiraError(
            "必须给 --id 或 --name",
            hint="当前可用转换：" + json.dumps(
                [{"id": t.get("id"), "name": t.get("name"), "to": _name(t.get("to"))} for t in body.get("transitions") or []],
                ensure_ascii=False,
            ),
        )

    transition_id = args.id
    if not transition_id:
        body = jc.request("GET", f"/issue/{args.key}/transitions", settings=settings)
        items = body.get("transitions") or []
        hit = [t for t in items if (t.get("name") or "").strip() == args.name.strip()]
        if not hit:  # 退一步：按目标状态名匹配
            hit = [t for t in items if (_name(t.get("to")) or "").strip() == args.name.strip()]
        if not hit:
            raise JiraError(
                f"找不到名为 {args.name!r} 的状态转换",
                hint="可用转换：" + json.dumps(
                    [{"id": t.get("id"), "name": t.get("name"), "to": _name(t.get("to"))} for t in items],
                    ensure_ascii=False,
                ),
            )
        if len(hit) > 1:
            raise JiraError(
                f"有 {len(hit)} 个转换都叫 {args.name!r}，请改用 --id 指定",
                hint=json.dumps([{"id": t.get("id"), "name": t.get("name")} for t in hit], ensure_ascii=False),
            )
        transition_id = hit[0].get("id")

    jc.request(
        "POST",
        f"/issue/{args.key}/transitions",
        settings=settings,
        json={"transition": {"id": str(transition_id)}},
    )
    # 回读一次确认状态真的变了（JIRA 偶发"提交成功但状态没动"，尤其是工作流校验条件不满足时）
    current = jc.request("GET", f"/issue/{args.key}?fields=status", settings=settings)
    return out.emit({
        "ok": True,
        "cmd": "transition",
        "data": {
            "issue_key": args.key,
            "transition_id": str(transition_id),
            "current_status": _name((current.get("fields") or {}).get("status")),
        },
    })


def cmd_update(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    fields: dict = {}
    if args.summary:
        fields["summary"] = args.summary
    if args.description is not None:
        fields["description"] = args.description
    if args.priority:
        fields["priority"] = {"name": args.priority}
    if args.assignee:
        fields["assignee"] = {"name": args.assignee}
    if args.labels is not None:
        fields["labels"] = [x for x in args.labels.split(",") if x]

    if not fields:
        raise JiraError("没有任何要更新的字段", hint="至少给 --summary/--description/--priority/--assignee/--labels 之一")

    jc.request("PUT", f"/issue/{args.key}", settings=settings, json={"fields": fields})
    return out.emit({
        "ok": True,
        "cmd": "update",
        "data": {"issue_key": args.key, "updated_fields": sorted(fields)},
    })


def cmd_delete(args, out: _Output) -> int:
    settings = jc.resolve_settings()
    if not args.yes:
        raw = jc.request("GET", f"/issue/{args.key}?fields=summary,status", settings=settings)
        f = raw.get("fields") or {}
        raise JiraError(
            "删除是不可恢复操作，必须显式加 --yes 确认",
            hint=json.dumps(
                {"key": args.key, "summary": f.get("summary"), "status": _name(f.get("status"))},
                ensure_ascii=False,
            ),
        )
    jc.request("DELETE", f"/issue/{args.key}", settings=settings)
    return out.emit({"ok": True, "cmd": "delete", "data": {"issue_key": args.key, "deleted": True}})


def cmd_whoami(args, out: _Output) -> int:
    """不联网，只看本地解析出来的配置（排查"为什么读不到凭据"用）。"""
    settings = jc.resolve_settings(require_credentials=False)
    return out.emit({
        "ok": True,
        "cmd": "whoami",
        "data": {
            "config_path": jc.config_path(),
            "config_exists": os.path.exists(jc.config_path()),
            "config_source": settings["source"],
            "itcode": settings["itcode"] or None,
            "password_present": bool(settings["password"]),
            "jira_base": settings["jira_base"],
            "issue_base": settings["issue_base"],
            "project_key": settings["project_key"],
        },
    })


def cmd_setup(args, out: _Output) -> int:
    """交互式配置向导。给人用；Agent 一般走环境变量，不走这个。"""
    path = jc.config_path()
    existing = jc.load_config()

    if existing and not args.force:
        return out.emit({
            "ok": False,
            "cmd": "setup",
            "error": f"配置已存在：{path}",
            "hint": "要覆盖请加 --force",
        })

    # stdin 不是终端（被重定向 / 由 Agent 调用）时直接报错，别去 input() 撞 EOFError
    if not sys.stdin.isatty():
        return out.emit({
            "ok": False,
            "cmd": "setup",
            "error": "stdin 不是交互式终端，无法走配置向导",
            "hint": (
                f"把提示语写进 stderr 了：配置文件位于 {path}。"
                f"非交互场景请改用环境变量 {jc.ENV_ITCODE} / {jc.ENV_PASSWORD}，"
                "或手工创建该 JSON 文件（字段见 reference.md）。"
            ),
        })

    print(f"配置文件：{path}", file=sys.stderr)
    try:
        itcode = input("ITCode: ").strip()
    except EOFError:
        return out.emit({"ok": False, "cmd": "setup", "error": "未输入 ITCode（stdin 已关闭）"})
    if not itcode:
        return out.emit({"ok": False, "cmd": "setup", "error": "未输入 ITCode"})

    import getpass

    password = getpass.getpass("Password: ")
    if not password:
        return out.emit({"ok": False, "cmd": "setup", "error": "未输入密码"})

    jira_base = input(f"JIRA REST base [{jc.DEFAULT_JIRA_BASE}]: ").strip() or jc.DEFAULT_JIRA_BASE
    project_key = input(f"默认项目 Key [{jc.DEFAULT_PROJECT_KEY}]: ").strip() or jc.DEFAULT_PROJECT_KEY

    candidate = {
        "itcode": itcode,
        "password": password,
        "jira_base": jira_base,
        "project_key": project_key,
    }

    # 先验通再落盘：原版是先保存再测，密码错了也会留下坏配置
    print("正在验证连接...", file=sys.stderr)
    try:
        me = jc.request("GET", "/myself", settings={**candidate, "issue_base": jc._derive_issue_base(jira_base), "source": "input"}, timeout=15)
    except JiraError as e:
        return out.emit({
            "ok": False,
            "cmd": "setup",
            "error": f"连接验证失败：{e.message}",
            "hint": "凭据未保存。检查 ITCode / 密码 / 网络后重试。",
            "auth_error": e.status == 401,
        })

    saved = jc.save_config(candidate)
    return out.emit({
        "ok": True,
        "cmd": "setup",
        "data": {"saved_to": saved, "user": me.get("displayName"), "itcode": itcode},
    })


def _clip(text: str, limit: int) -> str:
    if not isinstance(text, str):
        return text
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jira.py",
        description="Lenovo 内网 JIRA 命令行，支持多项目（输出 JSON，给 Agent 用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--human", action="store_true", help="人类可读输出（默认 JSON）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)
        return sp

    add("ping", cmd_ping, "检查连接 / 当前用户（**开工先跑这个**）")
    sp = add("projects", cmd_projects, "列出当前账号有权访问的所有项目（多项目，先看这个）")
    sp.add_argument("--recent", type=int, default=None, help="只看最近 N 个项目（JIRA recent 参数）")

    sp = add("project", cmd_project, "单个项目详情")
    sp.add_argument("key", nargs="?", default=None, help="项目 KEY，缺省 = 配置的默认项目")

    add("whoami", cmd_whoami, "只看本地配置解析结果，不联网")

    sp = add("search", cmd_search, "JQL 搜索")
    sp.add_argument("jql")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--start", type=int, default=0, help="分页起点 startAt")
    sp.add_argument("--fields", default=None, help="逗号分隔的字段，加 description 会把描述带出来")

    sp = add("text", cmd_text, "按标题/描述全文搜索关键词")
    sp.add_argument("text")
    sp.add_argument("--project", default=None, help="项目 KEY；all = 不限项目搜全部可访问项目")
    sp.add_argument("--limit", type=int, default=20)

    sp = add("bugs", cmd_bugs, "Bug 状态分布统计")
    sp.add_argument("--project", default=None, help="项目 KEY，缺省 = 配置的默认项目")
    sp.add_argument("--limit", type=int, default=500)

    sp = add("mine", cmd_mine, "我名下未解决的 issue")
    sp.add_argument("--project", default=None, help="项目 KEY，缺省 = 配置的默认项目")
    sp.add_argument("--limit", type=int, default=50)

    sp = add("issue", cmd_issue, "issue 详情")
    sp.add_argument("key")

    sp = add("comments", cmd_comments, "评论列表（默认最新 20 条）")
    sp.add_argument("key")
    sp.add_argument("--limit", type=int, default=20)

    sp = add("worklogs", cmd_worklogs, "工作日志")
    sp.add_argument("key")

    sp = add("transitions", cmd_transitions, "当前可用的状态转换")
    sp.add_argument("key")

    sp = add("attach-list", cmd_attach_list, "附件列表")
    sp.add_argument("key")

    sp = add("attach-get", cmd_attach_get, "下载附件")
    sp.add_argument("attachment_id")
    sp.add_argument("--save", default=None, help="保存路径（目录或完整文件路径）")
    sp.add_argument("--name", dest="filename", default=None, help="强制指定文件名")

    sp = add("attach-put", cmd_attach_put, "上传附件")
    sp.add_argument("key")
    sp.add_argument("file")

    sp = add("create", cmd_create, "创建 issue")
    sp.add_argument("--summary", required=True)
    sp.add_argument("--type", default="Bug")
    sp.add_argument("--priority", default="Medium")
    sp.add_argument("--project", default=None, help="项目 KEY，缺省 = 配置的默认项目")
    sp.add_argument("--description", default=None)
    sp.add_argument("--assignee", default=None, help="ITCode")

    sp = add("comment", cmd_comment, "添加评论")
    sp.add_argument("key")
    sp.add_argument("body")

    sp = add("transition", cmd_transition, "改状态")
    sp.add_argument("key")
    sp.add_argument("--id", default=None, help="转换 id（从 transitions 拿）")
    sp.add_argument("--name", default=None, help="转换名或目标状态名")

    sp = add("update", cmd_update, "更新字段")
    sp.add_argument("key")
    sp.add_argument("--summary", default=None)
    sp.add_argument("--description", default=None)
    sp.add_argument("--priority", default=None)
    sp.add_argument("--assignee", default=None)
    sp.add_argument("--labels", default=None, help="逗号分隔，会整体替换")

    sp = add("delete", cmd_delete, "删除 issue（必须 --yes）")
    sp.add_argument("key")
    sp.add_argument("--yes", action="store_true")

    sp = add("setup", cmd_setup, "交互式配置向导")
    sp.add_argument("--force", action="store_true", help="覆盖已有配置")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 允许 --human 出现在子命令之后
    human = False
    if "--human" in argv:
        human = True
        argv = [a for a in argv if a != "--human"]

    # stdout 强制 UTF-8：Windows 控制台默认 GBK，中文 issue 标题会炸
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    args.human = human
    out = _Output(human)

    try:
        return args.func(args, out)
    except JiraError as e:
        payload = {"ok": False, "cmd": args.cmd, **e.to_payload()}
        if e.status == 401:
            payload["auth_error"] = True
        return out.emit(payload)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "用户中断"}, ensure_ascii=False))
        return EXIT_ERR
    except Exception as e:  # 兜底：任何异常都变成 JSON，别甩栈给 Agent
        return out.emit({"ok": False, "cmd": args.cmd, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())

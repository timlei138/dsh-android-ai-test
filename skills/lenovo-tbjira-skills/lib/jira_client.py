"""
JIRA 客户端公共层（Lenovo 内网 JIRA，支持多项目；默认项目 AGENTPILOT 仅是缺省值）。

从参考实现（jira_mcp_server.py / interactive_demo.py）搬过来的核心逻辑：
  - 认证：ITCode + Password，HTTP Basic Auth
  - 配置：jira_config.json（兼容原格式 itcode/password/jira_base/project_key）
  - 请求：requests + verify=False（内网自签名证书）
  - 附件：必须走 /secure/attachment/{id}/（REST 的 content 字段会返回 HTML 错误页）

与参考实现的关键差异（都是踩过的坑，别改回去）：
  1. 配置读取容错：文件不存在 / 空文件 / 非法 JSON / 字段不全，一律当"未配置"，
     不再让 json.load 直接抛 JSONDecodeError 把进程打死。
     （原版只判 os.path.exists，一个 0 字节的 jira_config.json 就能让首次运行崩掉。）
  2. 认证缺失不 sys.exit：返回结构化错误，让调用方（CLI/MCP/Agent）自己决定怎么办。
     MCP 场景下 stdout 是协议通道，原版 print + sys.exit 会污染协议。
  3. 所有诊断信息走 stderr，stdout 只留 JSON 结果（管道友好）。
  4. 密码来源优先级：环境变量 > 配置文件 > 交互式向导。
     这样 CI / Agent 场景可以把密码放在环境变量里，不落到磁盘上。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from urllib.parse import unquote

try:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:  # pragma: no cover - 依赖缺失时给一句人话
    sys.stderr.write(
        "缺少依赖 requests/urllib3。请先执行：\n"
        "  python -m pip install -r skills/lenovo-tbjira-skills/requirements.txt\n"
    )
    raise

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_JIRA_BASE = "https://tbjira.lenovo.com/issue/rest/api/2"
DEFAULT_ISSUE_BASE = "https://tbjira.lenovo.com/issue"
DEFAULT_PROJECT_KEY = "AGENTPILOT"

ENV_ITCODE = "JIRA_ITCODE"
ENV_PASSWORD = "JIRA_PASSWORD"
ENV_BASE = "JIRA_BASE"
ENV_PROJECT = "JIRA_PROJECT_KEY"
ENV_CONFIG = "JIRA_CONFIG"

_CONFIG_FILENAME = "jira_config.json"


class JiraError(Exception):
    """业务层错误。payload 会原样进 JSON 输出的 error 字段。"""

    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint

    def to_payload(self) -> dict:
        out = {"error": self.message}
        if self.status is not None:
            out["status"] = self.status
        if self.hint:
            out["hint"] = self.hint
        return out


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def config_path() -> str:
    """
    配置文件位置。

    优先级：
      1. 环境变量 JIRA_CONFIG 指定的路径
      2. 本 skill 目录下的 jira_config.json（跟着 skill 走，换工作目录不会丢）
      3. 当前工作目录下的 jira_config.json（兼容原版：原版是相对路径读写的）
    """
    env = os.environ.get(ENV_CONFIG)
    if env:
        return os.path.abspath(os.path.expanduser(env))

    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    in_skill = os.path.join(skill_root, _CONFIG_FILENAME)
    if os.path.exists(in_skill):
        return in_skill

    return os.path.abspath(_CONFIG_FILENAME)


def load_config() -> dict:
    """
    读配置。**任何异常都不抛**，读不出来就当空配置。

    这是相对参考实现的第一处修复：原版 `os.path.exists` + `json.load`，
    碰到 0 字节文件直接 JSONDecodeError 崩溃（这个项目里真实存在一个 0 字节的
    jira_config.json）。
    """
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:  # utf-8-sig：兼容记事本存的 BOM
            raw = f.read().strip()
    except OSError as e:
        sys.stderr.write(f"[jira] 读取配置失败（忽略）：{e}\n")
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[jira] 配置文件不是合法 JSON（忽略）：{path} — {e}\n")
        return {}
    if not isinstance(data, dict):
        sys.stderr.write(f"[jira] 配置文件顶层不是对象（忽略）：{path}\n")
        return {}
    return data


def save_config(config: dict, *, path: str | None = None) -> str:
    """写配置，并在 POSIX 上收紧到 0600（里面是明文密码）。返回实际写入路径。"""
    target = path or config_path()
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    try:  # 明文密码，尽量别让同机其他用户读
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return target


def resolve_settings(*, require_credentials: bool = True) -> dict:
    """
    合并「环境变量 > 配置文件 > 默认值」，返回一份完整设置。

    返回键：itcode / password / jira_base / issue_base / project_key / source
    """
    cfg = load_config()

    itcode = os.environ.get(ENV_ITCODE) or cfg.get("itcode") or ""
    password = os.environ.get(ENV_PASSWORD) or cfg.get("password") or ""
    jira_base = (
        os.environ.get(ENV_BASE)
        or cfg.get("jira_base")
        or DEFAULT_JIRA_BASE
    ).rstrip("/")
    project_key = os.environ.get(ENV_PROJECT) or cfg.get("project_key") or DEFAULT_PROJECT_KEY

    # issue_base 是 REST base 去掉 /rest/api/2，用于 /secure/attachment/ 这类非 REST 路径
    issue_base = cfg.get("issue_base") or _derive_issue_base(jira_base)

    settings = {
        "itcode": str(itcode).strip(),
        "password": str(password),
        "jira_base": jira_base,
        "issue_base": issue_base.rstrip("/"),
        "project_key": str(project_key).strip(),
        "source": _describe_source(cfg),
    }

    if require_credentials and not (settings["itcode"] and settings["password"]):
        raise JiraError(
            "未配置 JIRA 凭据",
            hint=(
                "任选其一：\n"
                f"  a) python {_cli_relpath()} setup      # 交互式向导\n"
                f"  b) 设置环境变量 {ENV_ITCODE} / {ENV_PASSWORD}\n"
                f"当前会读取的配置文件：{config_path()}"
            ),
        )
    return settings


def _derive_issue_base(rest_base: str) -> str:
    """https://host/issue/rest/api/2 -> https://host/issue"""
    marker = "/rest/api/"
    idx = rest_base.find(marker)
    if idx > 0:
        return rest_base[:idx]
    return DEFAULT_ISSUE_BASE


def _describe_source(cfg: dict) -> str:
    if os.environ.get(ENV_ITCODE) and os.environ.get(ENV_PASSWORD):
        return "env"
    if cfg:
        return f"file:{config_path()}"
    return "none"


def _cli_relpath() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jira.py")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _session(settings: dict) -> requests.Session:
    s = requests.Session()
    s.auth = (settings["itcode"], settings["password"])
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    s.verify = False  # 内网自签名证书；同时已在模块顶部屏蔽 InsecureRequestWarning
    return s


def request(
    method: str,
    endpoint: str,
    *,
    settings: dict | None = None,
    timeout: int = 30,
    **kwargs,
) -> dict:
    """
    调 JIRA REST API，返回解析后的 JSON。

    - endpoint 以 / 开头，拼在 jira_base 之后（如 /issue/ABC-1）
    - 204 / 空响应返回 {}
    - 401 给出"密码过期请重配"的 hint（原版只是 print 一行就返回 error）
    """
    settings = settings or resolve_settings()
    url = f"{settings['jira_base']}/{endpoint.lstrip('/')}" if endpoint else settings["jira_base"]

    try:
        with _session(settings) as s:
            resp = s.request(method, url, timeout=timeout, **kwargs)
    except requests.exceptions.Timeout:
        raise JiraError(f"请求超时（{timeout}s）：{method} {url}") from None
    except requests.exceptions.SSLError as e:
        raise JiraError(f"TLS 握手失败：{e}") from None
    except requests.exceptions.ConnectionError as e:
        raise JiraError(
            f"连不上 JIRA：{e}",
            hint="确认在内网 / VPN 已连接，或 JIRA 地址是否正确（JIRA_BASE）。",
        ) from None
    except requests.exceptions.RequestException as e:
        raise JiraError(f"请求失败：{e}") from None

    return _parse_response(resp, method, url)


def _parse_response(resp: "requests.Response", method: str, url: str) -> dict:
    status = resp.status_code

    if status == 401:
        raise JiraError(
            "认证失败（401）：ITCode 或密码错误，或密码已过期",
            status=401,
            hint=(
                "密码不会自动恢复，重试无用。请重新配置：\n"
                f"  python {_cli_relpath()} setup --force\n"
                "（会覆盖现有凭据）"
            ),
        )
    if status == 403:
        raise JiraError("无权限（403）：当前账号对该资源没有操作权限", status=403)
    if status == 404:
        raise JiraError("资源不存在（404）：检查 issue key / 项目 key 是否写错", status=404)
    if status == 400:
        body = _safe_json(resp)
        detail = json.dumps(body.get("errorMessages") or body, ensure_ascii=False)[:400] if body else resp.text[:400]
        raise JiraError(f"请求参数被 JIRA 拒绝（400）：{detail}", status=400)
    if status >= 500:
        raise JiraError(f"JIRA 服务端错误（{status}）：{resp.text[:300]}", status=status)
    if not (200 <= status < 300):
        raise JiraError(f"HTTP {status}：{resp.text[:300]}", status=status)

    if status == 204 or not resp.content:
        return {}
    body = _safe_json(resp)
    if body is None:
        # 非 JSON 响应（登录页 / 反向代理错误页），别把 HTML 当数据
        raise JiraError(
            f"{method} {url} 返回了非 JSON 内容（可能是登录页或被网关拦截）",
            hint=f"响应前 200 字符：{resp.text[:200]!r}",
        )
    return body


def _safe_json(resp: "requests.Response"):
    try:
        return resp.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 附件（参考实现里反复踩坑的地方，集中在这里）
# ---------------------------------------------------------------------------

_HTML_MARKERS = (b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML")


def download_attachment(
    attachment_id: str,
    *,
    save_path: str | None = None,
    filename_hint: str | None = None,
    settings: dict | None = None,
    timeout: int = 60,
) -> dict:
    """
    下载附件。

    ⚠️ 必须走 `{issue_base}/secure/attachment/{id}/`：
    REST API 的 `attachment.content` 地址实测返回的是 HTML 错误页（几百字节），
    以 200 状态码返回，看起来"成功了"但文件打不开。参考实现就是在这上面栽的。

    额外防线：既然服务器会用 200+HTML 冒充附件，这里同时校验
    Content-Type 与内容魔数，发现是 HTML 直接报错，不落盘。
    """
    settings = settings or resolve_settings()
    uri = f"{settings['issue_base']}/secure/attachment/{attachment_id}/"

    try:
        with _session(settings) as s:
            resp = s.get(uri, timeout=timeout, stream=True)
            if resp.status_code != 200:
                raise JiraError(
                    f"附件下载失败：HTTP {resp.status_code}（附件 ID {attachment_id} 不存在或无权访问）",
                    status=resp.status_code,
                )

            content_type = resp.headers.get("Content-Type", "")
            is_html_ctype = "text/html" in content_type.lower()

            chunks: list[bytes] = []
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                chunks.append(chunk)
            payload = b"".join(chunks)
    except requests.exceptions.RequestException as e:
        raise JiraError(f"附件下载失败：{e}") from None

    if not payload:
        raise JiraError(f"附件内容为空（ID {attachment_id}），服务器返回 0 字节")

    head = payload[:200].lstrip()
    if head.startswith(_HTML_MARKERS) or (is_html_ctype and not _has_binary_sig(payload)):
        raise JiraError(
            f"服务器返回的是 HTML 页面而非附件内容（ID {attachment_id}）",
            hint=(
                "通常是附件 ID 不对，或者服务端把下载重定向到了登录页。"
                "先用 attach-list 拿正确的 attachment id。"
            ),
        )

    filename = (
        filename_hint
        or _filename_from_disposition(resp.headers.get("Content-Disposition", ""))
        or f"attachment_{attachment_id}"
    )
    filename = _sanitize_filename(filename)

    resolved = _resolve_save_path(save_path, filename)
    parent = os.path.dirname(resolved)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(resolved, "wb") as f:
        f.write(payload)

    return {
        "success": True,
        "attachment_id": str(attachment_id),
        "filename": filename,
        "saved_to": os.path.abspath(resolved),
        "size": os.path.getsize(resolved),
        "content_type": content_type or "unknown",
    }


def _has_binary_sig(payload: bytes) -> bool:
    """粗判二进制：含 NUL 字节基本可以断定不是文本错误页。"""
    return b"\x00" in payload[:1024]


def _filename_from_disposition(disposition: str) -> str | None:
    """
    解析 Content-Disposition。

    优先 RFC 5987 的 `filename*=UTF-8''...`（中文文件名走这条），
    退化到普通 `filename=`。两种都可能带引号，最后统一 unquote。
    """
    if not disposition:
        return None
    import re

    match = re.search(r"filename\*\s*=\s*(?:utf-8|UTF-8)''([^;]+)", disposition)
    if match:
        return unquote(match.group(1).strip().strip('"').strip())
    match = re.search(r'filename\s*=\s*"([^"]*)"', disposition)
    if match:
        return unquote(match.group(1))
    match = re.search(r"filename\s*=\s*([^;\n]+)", disposition)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    return None


def _sanitize_filename(name: str) -> str:
    """防路径穿越 + 去 Windows 非法字符。"""
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace(":", "_").replace("*", "_").replace("?", "_")
    name = name.replace('"', "_").replace("<", "_").replace(">", "_").replace("|", "_")
    name = name.strip().strip(".")
    return name or "attachment"


def _resolve_save_path(save_path: str | None, filename: str) -> str:
    if not save_path:
        return filename
    expanded = os.path.expanduser(save_path)
    if os.path.isdir(expanded) or save_path.endswith(("/", "\\")):
        return os.path.join(expanded, filename)
    return expanded


def upload_attachment(
    issue_key: str,
    file_path: str,
    *,
    settings: dict | None = None,
    timeout: int = 120,
) -> dict:
    """上传附件。必须带 X-Atlassian-Token: no-check（JIRA 的 XSRF 校验）。"""
    settings = settings or resolve_settings()
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        raise JiraError(f"文件不存在：{path}")
    if not os.path.isfile(path):
        raise JiraError(f"不是普通文件：{path}")

    uri = f"{settings['jira_base']}/issue/{issue_key}/attachments"
    headers = {"X-Atlassian-Token": "no-check"}  # 不能带 Content-Type，boundary 由 requests 生成

    try:
        with _session(settings) as s:
            s.headers.pop("Content-Type", None)
            with open(path, "rb") as f:
                files = {"file": (os.path.basename(path), f)}
                resp = s.post(uri, headers=headers, files=files, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise JiraError(f"上传失败：{e}") from None

    body = _parse_response(resp, "POST", uri)
    items = body if isinstance(body, list) else []
    return {
        "success": True,
        "issue_key": issue_key,
        "uploaded": len(items),
        "attachments": [
            {"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size")}
            for a in items
        ],
    }


def list_attachments(issue_key: str, *, settings: dict | None = None) -> dict:
    issue = request("GET", f"/issue/{issue_key}?fields=attachment", settings=settings)
    attachments = (issue.get("fields") or {}).get("attachment") or []
    return {
        "issue_key": issue_key,
        "attachment_count": len(attachments),
        "attachments": [
            {
                "id": att.get("id"),
                "filename": att.get("filename"),
                "size": att.get("size"),
                "human_size": human_size(att.get("size") or 0),
                "content_type": att.get("mimeType"),
                "created": att.get("created"),
                "author": _display_name(att.get("author")),
            }
            for att in attachments
        ],
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _display_name(obj) -> str | None:
    if not obj:
        return None
    return obj.get("displayName") or obj.get("name") or obj.get("key")


def human_size(num) -> str:
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def eprint(*args) -> None:
    """诊断信息一律走 stderr，保证 stdout 只有 JSON。"""
    print(*args, file=sys.stderr)

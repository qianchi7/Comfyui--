"""
gpt_image_2 — 端点预检 + 502 诊断
==================================

两个用途：

1. `ping_endpoint(base_url, api_key)` —
   发送请求前先探测 base_url 是否可达 + api_key 是否有效。
   避免在跑 5-15 分钟生成后才被反代 502 / 401 砍掉。

2. `diagnose(response)` —
   把 5xx / 4xx 响应的**完整上下文**（status, headers, body, server, cf-ray,
   x-request-id）打出来，让用户**一眼**看出 502 来自：
   - Cloudflare (cf-ray 头) — 中转反代
   - nginx (server: nginx/x.y.z) — 自建反代
   - new-api / one-api (server: Go) — 上游中继
   - OpenAI 官方 (server: OpenAI) — 真上游

3. `format_error_advice(response)` —
   根据诊断结果给**可执行的下一步建议**（改 timeout / 换 channel / 改 size）。
   永远不自动重试 — 决策权留给用户。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import requests

# 常见的 5xx 重试（不）建议
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# nginx / Cloudflare / Go / OpenAI 指纹
KNOWN_SERVERS = {
    "cloudflare": re.compile(r"cloudflare", re.IGNORECASE),
    "nginx": re.compile(r"nginx", re.IGNORECASE),
    "openai": re.compile(r"openai", re.IGNORECASE),
    "go": re.compile(r"\bgo(1\.\d+)?\b", re.IGNORECASE),  # Go stdlib net/http
    "gunicorn": re.compile(r"gunicorn", re.IGNORECASE),
    "envoy": re.compile(r"envoy", re.IGNORECASE),
    "caddy": re.compile(r"caddy", re.IGNORECASE),
}

# 反代常见的关键 header
DIAGNOSTIC_HEADERS = (
    "server",
    "via",
    "cf-ray",
    "cf-cache-status",
    "x-request-id",
    "x-amz-request-id",
    "x-trace-id",
    "x-real-ip",
    "x-forwarded-for",
    "x-newapi-channel",  # new-api 内部
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "retry-after",
    "alt-svc",
)


# ───────── 1. 端点预检 ─────────

def ping_endpoint(
    base_url: str,
    api_key: str,
    timeout: float = 15.0,
    disable_proxy: bool = True,
) -> dict[str, Any]:
    """探测 base_url 可达性 + api_key 有效性。

    返回 dict:
      {
        "ok": bool,             # 端点 + 认证都通
        "status": int | None,   # HTTP 状态码
        "latency_ms": float,    # 响应耗时
        "server": str,          # Server 头
        "url_reachable": bool,  # 端点能连通
        "auth_ok": bool,        # 鉴权通过 (200)
        "auth_msg": str,        # 鉴权失败时的原因
        "elapsed": float,
      }

    不抛异常。所有错误都装进 dict 里。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "url_reachable": False, "auth_ok": False,
                "auth_msg": "base_url 为空", "elapsed": 0.0, "status": None, "latency_ms": 0, "server": ""}

    # 探测路径：先 GET / 拿 server fingerprint，再 POST /v1/images/generations 验证 key
    t0 = time.time()
    info: dict[str, Any] = {
        "ok": False,
        "url_reachable": False,
        "auth_ok": False,
        "auth_msg": "",
        "status": None,
        "latency_ms": 0.0,
        "server": "",
    }

    session = requests.Session()
    session.trust_env = not disable_proxy
    session.headers.update({"User-Agent": "gpt-image-2-preflight/1.0"})

    # --- Step 1: GET / 看端点是否活着 + 抓 server 头 ---
    try:
        r = session.get(base + "/", timeout=timeout, allow_redirects=True)
        info["url_reachable"] = True
        info["status"] = r.status_code
        info["server"] = r.headers.get("server", "")
        info["latency_ms"] = (time.time() - t0) * 1000
    except requests.Timeout:
        info["auth_msg"] = f"GET {base}/ 超时 ({timeout}s) — 端点没回响"
        return info
    except requests.ConnectionError as e:
        info["auth_msg"] = f"GET {base}/ 连接失败: {type(e).__name__}: {str(e)[:120]}"
        return info
    except requests.RequestException as e:
        info["auth_msg"] = f"GET {base}/ 失败: {type(e).__name__}: {str(e)[:120]}"
        return info

    # --- Step 2: 用 api_key 发一个最小有效请求, 验证鉴权 ---
    # 选个最便宜最不可能扣费的: 1x1 size 1 quality low
    test_payload = {
        "model": "gpt-image-2",
        "prompt": "ping",
        "size": "1024x1024",
        "quality": "low",
        "n": 1,
    }
    try:
        t1 = time.time()
        r2 = session.post(
            base + "/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=test_payload,
            timeout=timeout,
        )
        info["latency_ms"] = (time.time() - t1) * 1000
        info["status"] = r2.status_code
        info["server"] = info["server"] or r2.headers.get("server", "")

        if r2.status_code == 200:
            info["auth_ok"] = True
            info["ok"] = True
            info["auth_msg"] = "✅ 端点可达 + 鉴权通过"
        elif r2.status_code in (401, 403):
            try:
                err = r2.json()
                msg = err.get("error", {}).get("message", str(err)[:200])
            except Exception:
                msg = r2.text[:200]
            info["auth_msg"] = f"鉴权失败 ({r2.status_code}): {msg}"
        elif r2.status_code in (400, 404, 422):
            # 鉴权本身过了, 只是参数不被接受 (比如 model 名错)
            info["auth_ok"] = True
            info["ok"] = True
            info["auth_msg"] = f"✅ 鉴权通过 (参数 4xx 不算: {r2.status_code})"
        else:
            # 5xx 之类
            try:
                err = r2.json()
                msg = err.get("error", {}).get("message", str(err)[:200])
            except Exception:
                msg = r2.text[:200]
            info["auth_msg"] = f"鉴权不确定 ({r2.status_code}): {msg}"
    except requests.Timeout:
        info["auth_msg"] = f"鉴权测试超时 ({timeout}s)"
    except requests.RequestException as e:
        info["auth_msg"] = f"鉴权测试网络错误: {type(e).__name__}: {str(e)[:120]}"

    return info


# ───────── 2. 响应诊断 ─────────

def diagnose(response: Optional[requests.Response]) -> dict[str, Any]:
    """提取响应的诊断信息（不抛异常）.

    返回:
      {
        "status": int,
        "elapsed_ms": float,
        "url": str,
        "server": str,
        "server_kind": "cloudflare" | "nginx" | "openai" | "go" | "envoy" | "caddy" | "gunicorn" | "unknown",
        "via": str,
        "cf_ray": str,
        "request_id": str,
        "headers": {...},        # 关键诊断头
        "body": str,            # 响应体（截断）
        "body_is_json": bool,
        "body_json": dict,      # 解析成功时给 dict
      }
    """
    if response is None:
        return {
            "status": 0,
            "elapsed_ms": 0,
            "url": "",
            "server": "",
            "server_kind": "unknown",
            "via": "",
            "cf_ray": "",
            "request_id": "",
            "headers": {},
            "body": "",
            "body_is_json": False,
            "body_json": {},
        }

    headers_lower = {k.lower(): v for k, v in response.headers.items()}
    server = headers_lower.get("server", "")
    server_kind = "unknown"
    for kind, pat in KNOWN_SERVERS.items():
        if pat.search(server):
            server_kind = kind
            break

    body = response.text or ""
    body_is_json = False
    body_json: Any = {}
    if body:
        try:
            body_json = response.json()
            body_is_json = True
        except Exception:
            pass

    # 截断 body 防止日志爆炸
    body_preview = body[:2000] + ("...<truncated>" if len(body) > 2000 else "")

    diag_headers = {h: headers_lower.get(h, "") for h in DIAGNOSTIC_HEADERS}

    return {
        "status": response.status_code,
        "elapsed_ms": (response.elapsed.total_seconds() * 1000) if response.elapsed else 0,
        "url": response.url,
        "server": server,
        "server_kind": server_kind,
        "via": headers_lower.get("via", ""),
        "cf_ray": headers_lower.get("cf-ray", ""),
        "request_id": (
            headers_lower.get("x-request-id", "")
            or headers_lower.get("x-amz-request-id", "")
            or headers_lower.get("x-trace-id", "")
        ),
        "headers": diag_headers,
        "body": body_preview,
        "body_is_json": body_is_json,
        "body_json": body_json,
    }


# ───────── 3. 错误建议 ─────────

def format_error_advice(diag: dict[str, Any], model: str = "", endpoint: str = "") -> str:
    """根据诊断结果给**可执行的下一步建议**。纯文本, 多行.

    注意: 这里**不**建议重试 — 用户偏好「重试会扣费, 不重试」.
    """
    status = diag.get("status", 0)
    server_kind = diag.get("server_kind", "unknown")
    cf_ray = diag.get("cf_ray", "")
    request_id = diag.get("request_id", "")
    body_json = diag.get("body_json", {}) or {}
    body = diag.get("body", "")

    # 从 OpenAI-style error body 提取 message
    api_msg = ""
    if isinstance(body_json, dict):
        err = body_json.get("error", {})
        if isinstance(err, dict):
            api_msg = err.get("message", "")
        elif isinstance(err, str):
            api_msg = err

    lines: list[str] = []

    if status == 0:
        # 网络层错误 (Connection refused / timeout / DNS fail)
        lines.append("⚠️ 网络层失败 — 请求根本没到服务器")
        lines.append("可能: 1) base_url 拼错 / 端口不通")
        lines.append("      2) 中间网络断开 (VPN / 中继挂了)")
        lines.append("      3) DNS 解析失败")
        return "\n".join(lines)

    if status in (502, 504):
        lines.append(f"⚠️ HTTP {status} — 反代/网关从上游拿了无效响应")
        if server_kind == "cloudflare":
            lines.append("原因: Cloudflare 在等上游 (OpenAI / 中转) 响应时超时")
            lines.append("      cf-ray=" + cf_ray)
            lines.append("      上游生成时间 > Cloudflare 默认 100s/30s 超时")
        elif server_kind in ("nginx", "caddy"):
            lines.append(f"原因: 自建反代 ({server_kind}) proxy_read_timeout 砍掉了连接")
            lines.append("      上游生成时间 > 反代 proxy_read_timeout (默认 60s)")
        elif server_kind == "go":
            lines.append("原因: Go 服务 (new-api / one-api 类) 自身超时或上游断流")
        elif server_kind == "openai":
            lines.append("原因: OpenAI 官方 5xx — 平台过载或区域限制")
        else:
            lines.append(f"原因: server={diag.get('server','?')!r} 不可识别")
        lines.append("")
        lines.append("可执行的下一步 (不重试):")
        lines.append("  1) 把 size 从 4K 降到 2K, quality 从 high 降到 medium")
        lines.append("  2) 如果是 new-api 链路, 检查反代 proxy_read_timeout (建议 1800s+)")
        lines.append("  3) 检查 new-api channel: 上游是否活着 (curl 上游 + 看 channel 重试日志)")
        lines.append("  4) 如果是 Cloudflare 中转, 上游生成时间必须 < 100s")

    elif status == 503:
        lines.append("⚠️ HTTP 503 — 服务暂时不可用")
        if "upstream" in api_msg.lower() or "capacity" in api_msg.lower():
            lines.append("原因: 上游容量满 / 限流")
        lines.append("可执行的下一步: 等几分钟再试, 或换 channel 上游")

    elif status == 500:
        lines.append("⚠️ HTTP 500 — 上游内部错误")
        if api_msg:
            lines.append(f"上游说: {api_msg}")
        lines.append("可执行的下一步: 换 channel, 或等几分钟后重试 (本次不重试)")

    elif status == 429:
        lines.append("⚠️ HTTP 429 — 限流")
        retry_after = diag.get("headers", {}).get("retry-after", "")
        if retry_after:
            lines.append(f"上游说: 等 {retry_after} 秒后再试")
        else:
            lines.append("上游没说等多久; 建议等 30-60 秒")
        lines.append("可执行的下一步: 换 key, 或换 channel 上游")

    elif status in (401, 403):
        lines.append(f"⚠️ HTTP {status} — 鉴权失败")
        if api_msg:
            lines.append(f"上游说: {api_msg}")
        lines.append("可执行的下一步: 1) 重新填写 api_key")
        lines.append("                  2) 检查 new-api channel 的 key 是否还有效")

    elif status == 400:
        lines.append("⚠️ HTTP 400 — 参数错误")
        if api_msg:
            lines.append(f"上游说: {api_msg}")
        if "size" in api_msg.lower() or "dimension" in api_msg.lower():
            lines.append("可执行的下一步: 改 size 字段 (16 倍数, ≤3840px)")
        elif "model" in api_msg.lower():
            lines.append(f"可执行的下一步: model={model!r} 名字在 channel 找不到对应映射")
        elif "image" in api_msg.lower():
            lines.append("可执行的下一步: 输入图超过 50MB 或格式不支持 (要 PNG/JPEG/WebP)")

    elif status == 413:
        lines.append("⚠️ HTTP 413 — 请求体过大")
        lines.append("可执行的下一步:")
        lines.append("  1) 减少参考图张数 (≤8 张)")
        lines.append("  2) 缩小输入图 (≤1024x1024)")
        lines.append("  3) 改 endpoint=generations 走 JSON image 数组 (对小图更友好)")

    elif status in (404, 405):
        lines.append(f"⚠️ HTTP {status} — 端点不存在或方法不允许")
        lines.append(f"路径: {endpoint}")
        lines.append("可执行的下一步: 检查 base_url 后面是否多了路径, 或反代路由写错")

    else:
        lines.append(f"⚠️ HTTP {status} — 未识别状态码")
        if api_msg:
            lines.append(f"上游说: {api_msg}")

    if request_id:
        lines.append("")
        lines.append(f"🔍 排查时把 request_id 交给上游: {request_id}")

    # === 内容审核拦截识别 ===
    # 各渠道常把审核拦截以 400/403、甚至空体 5xx 的形式返回；命中审核关键词时明确提示，
    # 避免用户把「被审核拦了」误当成网络/超时问题反复重试(还可能扣费)。
    moderation_hints = (
        "moderation", "safety", "safety system", "blocked", "flagged",
        "content_policy", "content policy", "content_filter", "rejected",
        "violat", "not allowed", "sensitive", "审核", "违规", "拦截", "敏感",
    )
    blob = f"{api_msg} {body}".lower()
    if any(h in blob for h in moderation_hints):
        lines.append("")
        lines.append("🛡️ 疑似【内容审核拦截】: 上游对提示词/输入图做了语义审核。")
        lines.append("   现各渠道审核偏严(透明度高/暴露类描述易被误触发)。")
        lines.append("   下一步: 换个说法重述提示词，或多试几次(审核有随机性)，或换渠道，而非死磕重试。")

    return "\n".join(lines) if lines else "（无建议）"


# ───────── 4. 完整错误信息组装 ─────────

def build_error_message(diag: dict[str, Any], model: str = "", endpoint: str = "") -> str:
    """把诊断 + 建议组装成多行错误信息（给 ComfyUI RuntimeError 用）."""
    status = diag.get("status", 0)
    if status == 0:
        # 网络层
        return f"[GPT Image 2] 网络失败: {diag.get('body', 'unknown')[:200]}"

    body = diag.get("body", "")
    body_one_line = body[:300].replace("\n", " ") if body else "<empty body>"

    parts = [
        f"API Request Failed (HTTP {status})",
        f"  model: {model}",
        f"  endpoint: {endpoint}",
        f"  server: {diag.get('server','?')!r} ({diag.get('server_kind','?')})",
        f"  body: {body_one_line!r}",
    ]
    if diag.get("cf_ray"):
        parts.append(f"  cf-ray: {diag['cf_ray']}")
    if diag.get("request_id"):
        parts.append(f"  x-request-id: {diag['request_id']}")
    if diag.get("via"):
        parts.append(f"  via: {diag['via']}")
    if diag.get("elapsed_ms"):
        parts.append(f"  elapsed: {diag['elapsed_ms']:.0f} ms")

    parts.append("")
    parts.append(format_error_advice(diag, model=model, endpoint=endpoint))

    return "\n".join(parts)

# -*- coding: utf-8 -*-
"""HTTP 客户端（纯 urllib，零依赖）。

职责：
- 统一 User-Agent / API 版本头 / 可选 Authorization
- 429 与 403（限额耗尽）按 Retry-After / X-RateLimit-Reset 退避等待后重试
- 5xx 与网络抖动做指数退避重试
- 记录最近一次响应的限额余量，供日志与额度保护使用

只承担"发请求、拿 JSON"这一件事，不含任何业务语义。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import env

# 最近一次响应里的限额信息（供 collect 打印/保护用）
LAST_RATE = {"remaining": None, "limit": None, "reset": None}
_last_call_at = [0.0]        # 用列表包一层，方便内部修改


class ApiError(RuntimeError):
    """非重试类错误（4xx）。"""


class RateLimited(RuntimeError):
    """限额耗尽且等待无意义时抛出。"""


# ---------------------------------------------------------------- 头
def github_headers(accept=None):
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
    }
    token = env.github_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def mask_url(url):
    """日志用：隐藏 URL 中的凭据类查询参数。"""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query)
    safe = [(k, "***" if k.lower() in ("key", "token", "access_token") else v)
            for k, v in pairs]
    return urllib.parse.urlunsplit(
        parsed._replace(query=urllib.parse.urlencode(safe))
    )


# ---------------------------------------------------------------- 节流
def throttle(min_interval=None):
    """保证相邻请求之间有最小间隔，避免触发限速。"""
    if min_interval is None:
        min_interval = (config.SEARCH_SLEEP if env.github_mode() == "token"
                        else config.SEARCH_SLEEP_ANON)
    elapsed = time.time() - _last_call_at[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call_at[0] = time.time()


def _record_rate(headers_obj):
    def as_int(name):
        raw = headers_obj.get(name)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    LAST_RATE["remaining"] = as_int("X-RateLimit-Remaining")
    LAST_RATE["limit"] = as_int("X-RateLimit-Limit")
    LAST_RATE["reset"] = as_int("X-RateLimit-Reset")


def rate_remaining():
    return LAST_RATE["remaining"]


# ---------------------------------------------------------------- 请求
def request_json(url, headers=None, retries=None, timeout=None):
    """GET 一个 JSON 端点，返回 (解析后的对象, 响应头)。失败抛 ApiError / RateLimited。"""
    retries = config.MAX_RETRY if retries is None else retries
    timeout = timeout or config.REQUEST_TIMEOUT
    headers = headers or {}
    attempt = 0
    last_error = None

    while attempt <= retries:
        attempt += 1
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _record_rate(resp.headers)
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {}, resp.headers

        except urllib.error.HTTPError as exc:
            _record_rate(exc.headers)
            status = exc.code
            if status in (429, 403):
                # 403 既可能是"限额耗尽"，也可能是"无权限"——靠 remaining 区分
                remaining = None
                try:
                    remaining = int(exc.headers.get("X-RateLimit-Remaining", ""))
                except (TypeError, ValueError):
                    pass
                if status == 403 and remaining not in (0, None):
                    raise ApiError("HTTP 403（权限不足或查询非法）：%s" % mask_url(url))
                wait = _retry_after_seconds(exc.headers)
                last_error = "HTTP %d 限速" % status
                if attempt > retries:
                    raise RateLimited("%s，重试 %d 次仍失败：%s"
                                      % (last_error, retries, mask_url(url)))
                time.sleep(wait)
                continue
            if 500 <= status < 600:
                last_error = "HTTP %d" % status
                if attempt > retries:
                    raise ApiError("%s，重试 %d 次仍失败：%s"
                                   % (last_error, retries, mask_url(url)))
                time.sleep(config.BACKOFF_BASE * (2 ** (attempt - 1)))
                continue
            # 其余 4xx 重试无意义
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = payload.get("message", "")
            except Exception:                       # noqa: BLE001 - 只为拼日志
                pass
            raise ApiError("HTTP %d %s：%s" % (status, detail, mask_url(url)))

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = "网络错误 %s" % exc
            if attempt > retries:
                raise ApiError("%s，重试 %d 次仍失败：%s"
                               % (last_error, retries, mask_url(url)))
            time.sleep(config.BACKOFF_BASE * (2 ** (attempt - 1)))

    raise ApiError("未知失败：%s" % last_error)


def _retry_after_seconds(headers_obj):
    """从响应头推断该等多久：优先 Retry-After，其次 X-RateLimit-Reset。"""
    raw = headers_obj.get("Retry-After")
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    reset = headers_obj.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(1.0, min(70.0, float(reset) - time.time() + 1))
        except (TypeError, ValueError):
            pass
    return 10.0


# ---------------------------------------------------------------- GitHub 语义封装
def search_repositories(query, sort="stars", order="desc", per_page=100, page=1):
    """GET /search/repositories —— 候选发现 + star 快照一次拿全。"""
    params = urllib.parse.urlencode({
        "q": query, "sort": sort, "order": order,
        "per_page": per_page, "page": page,
    })
    url = "%s/search/repositories?%s" % (config.GITHUB_API, params)
    payload, _ = request_json(url, headers=github_headers())
    return payload


def stargazers(repo_full_name, page, per_page=100):
    """GET /repos/{o}/{r}/stargazers —— 需 star+json 才有 starred_at 字段。"""
    url = "%s/repos/%s/stargazers?%s" % (
        config.GITHUB_API, repo_full_name,
        urllib.parse.urlencode({"per_page": per_page, "page": page}),
    )
    headers = github_headers(accept="application/vnd.github.star+json")
    payload, _ = request_json(url, headers=headers)
    return payload


def rate_limit():
    """GET /rate_limit —— 不消耗额度，用来做额度保护。"""
    url = "%s/rate_limit" % config.GITHUB_API
    payload, _ = request_json(url, headers=github_headers())
    return payload

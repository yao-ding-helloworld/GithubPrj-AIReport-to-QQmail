# -*- coding: utf-8 -*-
"""开启 / 查看 GitHub Pages（把仓库里的 docs/ 目录托管成网页）。

用法：
    python tools/gh_pages.py enable     # 开启 Pages，源 = main 分支的 /docs
    python tools/gh_pages.py status     # 看当前配置与最近一次构建
    python tools/gh_pages.py wait       # 轮询到构建完成，并验证链接可访问

注：Pages 的源有两种，这里用"从分支读取 /docs 目录"这种最稳的方式 ——
不需要额外的 workflow 权限（pages: write / id-token: write），
也不依赖 actions/deploy-pages 那套，少一个失败点。
"""

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402

OWNER, REPO = "yao-ding-helloworld", "GithubPrj-AIReport-to-QQmail"
BASE = "/repos/%s/%s" % (OWNER, REPO)
BRANCH, FOLDER = "main", "/docs"
LINES = []


def say(text=""):
    LINES.append(str(text))


def flush():
    out = config.ROOT / ".data" / "pages.txt"
    out.write_text("\n".join(LINES), encoding="utf-8")
    print("\n".join(LINES))


def api(method, path, token, payload=None):
    url = path if path.startswith("http") else config.GITHUB_API + path
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
        "Authorization": "Bearer " + token,
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:300]}
    except Exception as exc:                                # noqa: BLE001
        return None, {"error": "%s: %s" % (type(exc).__name__, exc)}


def show(settings, build=None):
    say("站点地址：%s" % settings.get("html_url", "-"))
    say("状态    ：%s" % settings.get("status", "-"))
    src = settings.get("source") or {}
    say("来源    ：%s %s" % (src.get("branch", "-"), src.get("path", "-")))
    if build:
        say("最近构建：%s（%s）" % (build.get("status"), build.get("error") or "无错误"))


def main(argv):
    env.load_env()
    token = env.github_token()
    if not token:
        say("[FAIL] .env 里没有 GITHUB_TOKEN")
        return flush() or 1

    cmd = argv[1] if len(argv) > 1 else "status"
    say("== GitHub Pages ==")
    say("仓库：%s/%s" % (OWNER, REPO))
    say("")

    if cmd == "enable":
        status, data = api("GET", "%s/pages" % BASE, token)
        if status == 200:
            say("[i] Pages 已开启，更新来源配置…")
            code, body = api("PUT", "%s/pages" % BASE, token,
                             {"source": {"branch": BRANCH, "path": FOLDER}})
        else:
            say("Pages 当前未开启，正在开启…")
            code, body = api("POST", "%s/pages" % BASE, token,
                             {"source": {"branch": BRANCH, "path": FOLDER},
                              "build_type": "legacy"})
        if code in (201, 204):
            say("[PASS] 已提交 Pages 配置：%s %s" % (BRANCH, FOLDER))
        elif code == 409:
            say("[i] 配置已存在（HTTP 409），无需重复开启")
        else:
            say("[FAIL] 配置失败：HTTP %s %s" % (code, body))
            return flush() or 1
        say("")
        time.sleep(6)

    # status / wait / enable 之后都统一看一眼现状
    status, settings = api("GET", "%s/pages" % BASE, token)
    if status != 200:
        say("[FAIL] 读取 Pages 状态失败：HTTP %s %s" % (status, settings))
        if status == 404:
            say("       Pages 尚未开启 —— 先跑 enable")
        return flush() or 1
    status, builds = api("GET", "%s/pages/builds?per_page=1" % BASE, token)
    build = (builds or [{}]).get("latest") if isinstance(builds, dict) else None
    show(settings, build)
    url = settings.get("html_url")

    if cmd == "wait":
        say("")
        say("等待构建完成（最多 300 秒）…")
        deadline = time.time() + 300
        last = None
        while time.time() < deadline:
            time.sleep(15)
            status, builds = api("GET", "%s/pages/builds?per_page=1" % BASE, token)
            build = (builds or {}).get("latest") if isinstance(builds, dict) else None
            state = (build or {}).get("status")
            if state != last:
                say("  [%d 秒] %s" % (int(time.time() - (deadline - 300)), state))
                last = state
            if state == "built":
                break
        say("")
        say("--- 验证链接 ---")
        code, headers, body = fetch(url)
        say("HTTP %s · %d 字节" % (code, len(body)))
        if code == 200 and b"GitHub AI" in body:
            say("[PASS] 链接可访问，页面内容正确")
        elif code == 200:
            say("[!] 可访问但内容不像我们的报告，可能还在用旧构建")
        else:
            say("[!] 暂不可访问，Pages 首次生效有时需要几分钟")
        say("")
        say("页面地址：%s" % url)
    return flush() or 0


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:                                # noqa: BLE001
        return None, {}, str(exc).encode()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

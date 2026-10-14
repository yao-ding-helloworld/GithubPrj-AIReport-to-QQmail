# -*- coding: utf-8 -*-
"""推送前置检查：仓库能不能推、Token 有没有写权限、github.com 通不通。

跑法：python tools/check_remote.py [仓库URL]
不显式传 URL 时，读本地 git remote 的 origin。

为什么要先跑这个：
本地 git push 需要 (1) 能连上 github.com，(2) Token 有 Contents 写权限。
两者任一不满足，push 都会以看不懂的报错收场；这个脚本把结论提前说清楚。
"""

import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def flush(code=0):
    out = config.ROOT / ".data" / "check_remote.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LINES), encoding="utf-8")
    try:
        print("\n".join(LINES))
    except Exception:                                       # noqa: BLE001
        pass
    return code


def parse_owner_repo(url):
    """支持 https://github.com/owner/repo(.git) 与 git@github.com:owner/repo.git。"""
    url = url.strip()
    if url.startswith("git@"):
        path = url.split(":", 1)[1]
    else:
        path = urllib.parse.urlsplit(url).path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def git_remote_url():
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(config.ROOT), capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or None
    except Exception:                                       # noqa: BLE001
        return None


def http_get(url, token=None, timeout=20):
    """返回 (status, data, headers)。headers 用来看 GitHub 回带的 token 元信息。"""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return resp.status, data, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except ValueError:
            data = {"raw": body[:200]}
        return exc.code, data, dict(exc.headers)
    except Exception as exc:                                # noqa: BLE001
        return None, {"error": "%s: %s" % (type(exc).__name__, exc)}, {}


def check_token(token):
    """查 Token 本身：是否有效、属于谁、有哪些 scope、什么时候过期。

    GitHub 会把 scope 放在响应头里回带：
      x-oauth-scopes                        —— Classic token 的 scope 列表
      x-accepted-github-permissions         —— Fine-grained token 的权限
      github-authentication-token-expiration —— 过期时间
    """
    if not token:
        say("[FAIL] .env 里的 GITHUB_TOKEN 是空的 —— 先填 Token 再看这一节")
        return False

    status, data, headers = http_get("%s/user" % config.GITHUB_API, token=token)
    if status != 200:
        say("[FAIL] Token 无效或已过期：HTTP %s %s"
            % (status, data.get("message", data)))
        say("       GitHub 上删掉重建一个新的即可（token 明文只在生成时显示一次，找不回来）")
        return False

    # 响应头大小写不固定，统一用小写键查
    lower = {k.lower(): v for k, v in headers.items()}

    say("[PASS] Token 有效，属于账号：%s" % data.get("login"))
    say("       类型：%s"
        % ("Classic" if "x-oauth-scopes" in lower else "Fine-grained（或 GitHub App）"))

    scope_list = [s.strip() for s in (lower.get("x-oauth-scopes") or "").split(",")
                  if s.strip()]
    if scope_list:
        say("       scope：%s" % ", ".join(scope_list))
    else:
        say("       scope：（此头为空——Fine-grained token 的权限不通过它返回）")

    expires = lower.get("github-authentication-token-expiration")
    if expires:
        say("       过期时间：%s" % expires)

    perms = lower.get("x-accepted-github-permissions")
    if perms:
        say("       接受的权限粒度：%s" % perms)

    if scope_list:
        has_repo = "repo" in scope_list
        has_workflow = "workflow" in scope_list
        if has_repo and has_workflow:
            say("[PASS] 推送所需 scope 齐全：repo + workflow")
        else:
            lack = []
            if not has_repo:
                lack.append("repo")
            if not has_workflow:
                lack.append("workflow")
            say("[FAIL] 缺 scope：%s" % ", ".join(lack))
            say("       repo 管推送内容；workflow 是推 .github/workflows/ 文件必需的，"
                "缺它会被 GitHub 拒：")
            say("       refusing to allow a Personal Access Token to create or update workflow")
    else:
        say("[i] Fine-grained token 的 scope 不在此返回；"
            "下面用「仓库 permissions.push」间接判断写权限")
    return True


def main(argv):
    env.load_env()
    url = argv[1] if len(argv) > 1 else git_remote_url()
    token = env.github_token()

    say("== 推送前置检查 ==")
    say("")

    # ---- 0. Token 本身 ----
    if token:
        say("Token：%s（来自 .env 的 GITHUB_TOKEN）" % env.mask(token))
    else:
        say("Token：<未配置>")
    check_token(token)
    say("")

    if not url:
        say("[FAIL] 没有 origin 远程地址。用法：python tools/check_remote.py <仓库URL>")
        return flush(1)
    say("远程地址：%s" % url)

    owner, repo = parse_owner_repo(url)
    if not owner:
        say("[FAIL] 解析不出 owner/repo，检查 URL 格式")
        return flush(1)
    say("解析结果：owner=%s repo=%s" % (owner, repo))
    say("")

    # ---- 1. github.com 是否可达（git push 靠它） ----
    status, _, _ = http_get("https://github.com")
    if status:
        say("[PASS] github.com 可达（HTTP %s）" % status)
    else:
        say("[FAIL] github.com 不可达 —— 本机出网走代理，很可能只放行了 api.github.com。")
        say("       git push 会报 CONNECT tunnel failed / 502。")
        say("       对策：见本文件末尾「推不上去怎么办」。")
    say("")

    # ---- 2. 仓库是否可见 / 是否为空 ----
    api = "%s/repos/%s/%s" % (config.GITHUB_API, owner, repo)
    status, data, _ = http_get(api, token=token)
    if status == 200:
        say("[PASS] 仓库可见：%s" % data.get("full_name"))
        say("       可见性：%s | 默认分支：%s | 体积：%s KB"
            % ("private" if data.get("private") else "public",
               data.get("default_branch"), data.get("size", 0)))

        # size 字段对小于 1MB 的仓库恒为 0，不能用来判断空仓库；查 commits 才准。
        cstatus, commits, _ = http_get("%s/commits?per_page=1" % api, token=token)
        if cstatus == 409:
            say("       仓库状态：完全空仓库（没有任何提交）→ 可直接 push")
        elif cstatus == 200 and isinstance(commits, list) and commits:
            head = commits[0]
            say("       仓库状态：已有提交 → 直接 push 可能被拒（非快进）")
            say("                  最新提交 %s  %s"
                % (head.get("sha", "")[:7],
                   (head.get("commit", {}).get("message") or "").splitlines()[0][:50]))
            say("                  对策：让本地提交以该提交为父（git commit-tree -p），"
                "或 git push --force 覆盖它")
        else:
            say("       仓库状态：未知（commits 查询返回 %s）" % cstatus)
        perms = data.get("permissions") or {}
        if perms:
            say("       当前 Token 权限：push=%s admin=%s"
                % (perms.get("push"), perms.get("admin")))
            if not perms.get("push"):
                say("       [FAIL] Token 没有写权限！push 会被拒（403）。")
                say("              Classic → 勾 repo + workflow；"
                    "Fine-grained → Contents: Read and write + Workflows: Read and write")
        else:
            say("       [i] 响应未含 permissions 字段（匿名请求），无法判断写权限")
    elif status == 404:
        say("[FAIL] 仓库 404：要么不存在，要么是私有仓库而当前 Token 无权访问。")
        say("       若仓库确实存在且为私有，说明 .env 的 GITHUB_TOKEN 为空或权限不足。")
    else:
        say("[FAIL] 查询仓库失败：HTTP %s %s" % (status, data))
    say("")

    # ---- 3. 本地分支与提交 ----
    try:
        branch = subprocess.run(["git", "branch", "--show-current"],
                                cwd=str(config.ROOT), capture_output=True,
                                text=True, timeout=20).stdout.strip()
        head = subprocess.run(["git", "log", "-1", "--format=oneline"],
                              cwd=str(config.ROOT), capture_output=True,
                              text=True, timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               cwd=str(config.ROOT), capture_output=True,
                               text=True, timeout=20).stdout.strip()
        say("[i] 本地分支：%s | HEAD：%s" % (branch, head.split(" ")[0] if head else "-"))
        if dirty:
            say("    未提交改动：%d 处（推送前需先 commit）"
                % len([x for x in dirty.splitlines() if x.strip()]))
    except Exception as exc:                                # noqa: BLE001
        say("[i] 本地 git 状态读取失败：%s" % exc)
    say("")

    say("推不上去怎么办：")
    say("  1. 若 github.com 不可达 → 改走 SSH（22 或 443），或在本机 VSCode 里推送")
    say("  2. 若 Token 无写权限 → 去 GitHub 重新生成一个带 Contents/Workflows 写的 Token")
    say("  3. 若仓库非空（有 README）→ 先 git pull --rebase origin main 再 push")
    return flush(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

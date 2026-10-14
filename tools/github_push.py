# -*- coding: utf-8 -*-
"""用 GitHub Git Data API 把本地提交推到远端（阶段 7.4 的兜底通道）。

为什么需要它：
本机出网对 github.com:443 是间歇性可达的，`git push` 经常报
"Failed to connect to github.com:443" 或 "CONNECT tunnel failed, response 502"；
而 api.github.com 一直稳定。GitHub 的 Git Data API 能完成同样的事：
上传 blob → 组装 tree → 创建 commit → 更新 ref。

关键设计：**复刻本地提交的元信息**（tree / parent / author / committer / message），
所以远端产出的 commit sha 与本地 `main` 完全一致 —— 不会产生历史分叉，
用户下次在自己机器上 `git push` 也不会被拒。

用法：python tools/github_push.py
"""

import base64
import datetime
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402

API = config.GITHUB_API
LINES = []


def say(text=""):
    LINES.append(str(text))


def flush(code=0):
    out = config.ROOT / ".data" / "apipush.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LINES), encoding="utf-8")
    try:
        print("\n".join(LINES))
    except Exception:                                       # noqa: BLE001
        pass
    return code


# ---------------------------------------------------------------- 本地 git
def git(*args, binary=False):
    result = subprocess.run(["git"] + list(args), cwd=str(config.ROOT),
                            capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("git %s 失败：%s"
                           % (" ".join(args),
                              result.stderr.decode("utf-8", "replace").strip()))
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def parse_owner_repo(url):
    path = url.split(":", 1)[1] if url.startswith("git@") else \
        urllib.parse.urlsplit(url).path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    return (parts[0], parts[1]) if len(parts) >= 2 else (None, None)


def to_iso(epoch, tz):
    """'1760441178 +0800' → '2026-10-14T19:26:09+08:00'"""
    sign = -1 if tz.startswith("-") else 1
    offset = sign * datetime.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
    dt = datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone(offset))
    return dt.isoformat()


def local_commit_meta():
    """解析 `git log -1 --pretty=raw`，拿到复刻提交所需的全部字段。"""
    raw = git("log", "-1", "--pretty=raw")
    lines = raw.splitlines()
    meta = {"tree": None, "parents": [], "author": None, "committer": None}
    idx = 0
    for idx, line in enumerate(lines):
        if line.startswith("tree "):
            meta["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            meta["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            name, email, epoch, tz = re.match(
                r"author (.*) <(.*)> (\d+) ([+-]\d{4})", line).groups()
            meta["author"] = {"name": name, "email": email, "date": to_iso(epoch, tz)}
        elif line.startswith("committer "):
            name, email, epoch, tz = re.match(
                r"committer (.*) <(.*)> (\d+) ([+-]\d{4})", line).groups()
            meta["committer"] = {"name": name, "email": email, "date": to_iso(epoch, tz)}
        elif line == "" and meta["committer"]:
            break
    meta["message"] = "\n".join(lines[idx + 1:]).rstrip() + "\n"
    return meta


def index_entries():
    """`git ls-files -s` → [(mode, blob_sha, path)]；内容从对象库取，保证字节精确。"""
    entries = []
    for line in git("ls-files", "-s").splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, blob_sha, _stage = meta.split()
        entries.append((mode, blob_sha, path))
    return entries


# ---------------------------------------------------------------- API
def api(method, path, token, payload=None):
    url = path if path.startswith("http") else API + path
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
        "Authorization": "Bearer " + token,
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:300]}
    except Exception as exc:                                # noqa: BLE001
        return None, {"error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    env.load_env()
    token = env.github_token()
    if not token:
        say("[FAIL] .env 里没有 GITHUB_TOKEN")
        return flush(1)

    url = git("remote", "get-url", "origin").strip()
    owner, repo = parse_owner_repo(url)
    base = "/repos/%s/%s" % (owner, repo)

    say("== Git Data API 推送 ==")
    say("远端：%s/%s" % (owner, repo))
    say("")

    meta = local_commit_meta()
    local_sha = git("rev-parse", "HEAD").strip()
    say("本地提交：%s" % local_sha)
    say("  tree      %s" % meta["tree"])
    say("  parent    %s" % (", ".join(meta["parents"]) or "(无)"))
    say("  author    %s <%s> %s"
        % (meta["author"]["name"], meta["author"]["email"], meta["author"]["date"]))
    say("")

    # ---- 1. 远端当前 main ----
    status, ref = api("GET", "%s/git/ref/heads/main" % base, token)
    if status == 200:
        remote_sha = ref["object"]["sha"]
        say("远端 main：%s" % remote_sha)
        if remote_sha == local_sha:
            say("[i] 远端已是本地这个提交，无需推送。")
            return flush(0)
    elif status == 404:
        remote_sha = None
        say("远端 main：不存在（空仓库）")
    else:
        say("[FAIL] 读取远端 ref 失败：HTTP %s %s" % (status, ref))
        return flush(1)
    say("")

    # ---- 2. 上传 blob ----
    entries = index_entries()
    say("上传 %d 个文件：" % len(entries))
    tree_entries = []
    for i, (mode, blob_sha, path) in enumerate(entries, 1):
        content = git("cat-file", "blob", blob_sha, binary=True)
        status, blob = api("POST", "%s/git/blobs" % base, token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        if status not in (200, 201):
            say("  [FAIL] %s：HTTP %s %s" % (path, status, blob))
            return flush(1)
        tree_entries.append({"path": path, "mode": mode,
                             "type": "blob", "sha": blob["sha"]})
        say("  [%2d/%d] %-52s %6d B" % (i, len(entries), path, len(content)))
    say("")

    # ---- 3. 组 tree ----
    status, tree = api("POST", "%s/git/trees" % base, token, {"tree": tree_entries})
    if status not in (200, 201):
        say("[FAIL] 创建 tree 失败：HTTP %s %s" % (status, tree))
        return flush(1)
    say("新 tree：%s%s" % (tree["sha"],
                          "（与本地 tree 一致 ✓）" if tree["sha"] == meta["tree"] else
                          "（与本地 tree 不一致，请检查）"))
    say("")

    # ---- 4. 创建 commit（复刻本地元信息，争取 sha 完全一致）----
    payload = {
        "message": meta["message"],
        "tree": tree["sha"],
        "parents": meta["parents"],
        "author": meta["author"],
        "committer": meta["committer"],
    }
    status, commit = api("POST", "%s/git/commits" % base, token, payload)
    if status not in (200, 201):
        say("[FAIL] 创建 commit 失败：HTTP %s %s" % (status, commit))
        return flush(1)
    new_sha = commit["sha"]
    say("新提交：%s" % new_sha)
    say("  与本地一致：%s" % ("是 ✓（不会产生历史分叉）" if new_sha == local_sha
                              else "否（tree 内容相同，但提交元信息有差异）"))
    say("")

    # ---- 5. 更新 ref ----
    if remote_sha:
        status, body = api("PATCH", "%s/git/refs/heads/main" % base, token,
                           {"sha": new_sha, "force": False})
    else:
        status, body = api("POST", "%s/git/refs" % base, token,
                           {"ref": "refs/heads/main", "sha": new_sha})
    if status not in (200, 201):
        say("[FAIL] 更新 ref 失败：HTTP %s %s" % (status, body))
        return flush(1)
    say("[PASS] 远端 main 已更新 → %s" % body.get("object", {}).get("sha", new_sha))
    say("")
    say("下一步：去仓库 Settings → Secrets and variables → Actions 添加 6 个 Secret，")
    say("然后到 Actions 页面手动触发一次 workflow。")
    return flush(0)


if __name__ == "__main__":
    sys.exit(main())

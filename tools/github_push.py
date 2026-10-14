# -*- coding: utf-8 -*-
"""用 GitHub Git Data API 推送本地工作树（绕开 git push 的网络限制）。

为什么需要它：
本机出网对 github.com:443 是间歇性可达的，`git push` 经常报
"Failed to connect to github.com:443" 或 "CONNECT tunnel failed, response 502"；
而 api.github.com 一直稳定。Git Data API 能完成同样的事。

两个必须记住的坑（都是实测踩出来的）：
1. **不要用 `git log --pretty=raw` 取提交信息** —— 它会给消息体每行加 4 个空格缩进，
   写进 GitHub 的提交信息就被污染了。要用 `git cat-file commit <sha>` 取真·原始内容。
2. **GitHub 会把提交日期规范化成 UTC**（`+0800` → `+0000`，同一时刻）。
   所以本地按 +0800 算出的 sha 永远对不上远端。想本地/远端一致，就得用 UTC 形态。

策略：
- parent 取**远端当前 main**，保证远端历史是严格快进的线性追加，不需要 force
- 推送前先在本地按 UTC 形态算出"预期 sha"，与 API 返回的 sha 比对（自校验）
- **默认只推代码**（`--with-data` 可关闭）：`.data/` 里的快照 CSV 由云端工作流维护，
  本机推代码时不该把它们覆盖回旧版本
- **不把本地分支指向 API 创建的提交**：那些对象本机没有，硬指过去会让
  `git status` / `git log` 报 `bad tree object` / `Failed to traverse parents`。
  本地与远端因此是两条平行线：内容一致、历史不同。要看远端 sha 查 `.data/remote_main.txt`

用法：
    python tools/github_push.py                # 只推代码（默认）
    python tools/github_push.py --with-data    # 连 .data/ 快照 CSV 一起推
"""

import base64
import datetime
import hashlib
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


def raw_commit(sha):
    """用 git cat-file 取真·原始提交内容（不会被 --pretty 缩进污染）。"""
    raw = git("cat-file", "commit", sha)
    head, _, message = raw.partition("\n\n")
    fields = {"parents": []}
    for line in head.splitlines():
        key, _, value = line.partition(" ")
        if key == "parent":
            fields["parents"].append(value)
        elif key == "tree":
            fields["tree"] = value
        elif key in ("author", "committer"):
            name, email, epoch, tz = re.match(
                r"(.*) <(.*)> (\d+) ([+-]\d{4})", value).groups()
            fields[key] = {"name": name, "email": email,
                           "epoch": int(epoch), "tz": tz}
    return fields, message


def to_utc(person):
    """把 (epoch, tz) 归一成 GitHub 会存下来的 UTC 形态。"""
    return {
        "name": person["name"],
        "email": person["email"],
        "date": datetime.datetime.fromtimestamp(
            person["epoch"], tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def commit_object_bytes(tree, parents, author_epoch, committer_epoch,
                        author, committer, message):
    """严格按 git 的对象字节格式拼装，返回 (对象字节, sha1)。"""
    raw = "tree %s\n" % tree
    for parent in parents:
        raw += "parent %s\n" % parent
    raw += "author %s <%s> %d +0000\n" % (author["name"], author["email"], author_epoch)
    raw += "committer %s <%s> %d +0000\n" % (committer["name"], committer["email"],
                                             committer_epoch)
    raw += "\n" + message
    body = ("commit %d\0" % len(raw.encode("utf-8"))).encode("utf-8") + raw.encode("utf-8")
    return body, hashlib.sha1(body).hexdigest()


def index_entries(code_only=False):
    """从索引取要上传的文件。

    code_only=True 时跳过 .data/ 下的快照 CSV —— 那些是**云端工作流自己维护的数据**，
    本机推送代码时不该覆盖它们（否则会把云端刚提交的快照回退成本机的旧版本）。
    """
    entries = []
    for line in git("ls-files", "-s").splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, blob_sha, _stage = meta.split()
        if code_only and path.startswith(".data/"):
            continue
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

    base = "/repos/%s/%s" % parse_owner_repo(git("remote", "get-url", "origin").strip())

    say("== Git Data API 推送 ==")
    say("远端仓库：%s" % base)
    say("")

    head = git("rev-parse", "HEAD").strip()
    fields, message = raw_commit(head)
    say("本地 HEAD：%s" % head)
    say("  tree      %s" % fields["tree"])
    say("  message   首行 %r（总长 %d 字节，无缩进 ✓）"
        % (message.splitlines()[0] if message else "", len(message)))
    say("")

    # ---- 1. 远端当前 main（作为 parent，保证快进）----
    status, ref = api("GET", "%s/git/ref/heads/main" % base, token)
    if status == 200:
        remote_sha = ref["object"]["sha"]
        say("远端 main：%s" % remote_sha)
        if remote_sha == head:
            say("[i] 远端已是本地这个提交，无需推送。")
            return flush(0)
        parents = [remote_sha]
    elif status == 404:
        say("远端 main：不存在（空仓库）")
        parents = []
    else:
        say("[FAIL] 读取远端 ref：HTTP %s %s" % (status, ref))
        return flush(1)
    say("新提交的 parent 取远端 main → 远端历史保持线性快进，无需 force")
    say("")

    # ---- 2. 上传 blob ----
    # 默认只推代码：.data/ 下的快照 CSV 归**云端工作流**维护，本机推代码时不该覆盖它们
    # （否则会把云端刚提交的快照回退成本机的旧版本）。要用本机数据覆盖时加 --with-data。
    code_only = "--with-data" not in sys.argv
    base_tree = None
    if code_only:
        status, rcommit = api("GET", "%s/git/commits/%s" % (base, remote_sha), token)
        if status == 200:
            base_tree = rcommit["tree"]["sha"]
        say("模式：只推代码（.data/ 快照 CSV 保持云端的版本不动）")
        if base_tree:
            say("      tree 以远端 %s 为基底" % base_tree)
    say("")

    entries = index_entries(code_only=code_only)
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
        tree_entries.append({"path": path, "mode": mode, "type": "blob",
                             "sha": blob["sha"]})
        say("  [%2d/%d] %-52s %6d B" % (i, len(entries), path, len(content)))
    say("")

    # ---- 3. tree ----
    payload_tree = {"tree": tree_entries}
    if base_tree:
        payload_tree["base_tree"] = base_tree
    status, tree = api("POST", "%s/git/trees" % base, token, payload_tree)
    if status not in (200, 201):
        say("[FAIL] 创建 tree：HTTP %s %s" % (status, tree))
        return flush(1)
    if not code_only and tree["sha"] == fields["tree"]:
        say("[i] tree 与本地一致：%s" % tree["sha"])
    else:
        say("[i] 新 tree：%s" % tree["sha"])
    say("")

    # ---- 4. commit（先本地预算 sha 做自校验）----
    author, committer = to_utc(fields["author"]), to_utc(fields["committer"])
    body, expect = commit_object_bytes(
        tree["sha"], parents, fields["author"]["epoch"], fields["committer"]["epoch"],
        author, committer, message)
    say("预期 sha（本地按 UTC 形态预算）：%s" % expect)

    status, commit = api("POST", "%s/git/commits" % base, token, {
        "message": message,
        "tree": tree["sha"],
        "parents": parents,
        "author": author,
        "committer": committer,
    })
    if status not in (200, 201):
        say("[FAIL] 创建 commit：HTTP %s %s" % (status, commit))
        return flush(1)
    new_sha = commit["sha"]
    say("远端返回 sha：%s" % new_sha)
    say("  自校验：%s" % ("一致 ✓（本地对象格式推导正确）" if new_sha == expect
                          else "不一致（GitHub 改写了某些元信息，属可接受）"))
    say("")

    # ---- 5. 更新 ref ----
    if parents:
        status, body_ref = api("PATCH", "%s/git/refs/heads/main" % base, token,
                               {"sha": new_sha, "force": False})
    else:
        status, body_ref = api("POST", "%s/git/refs" % base, token,
                               {"ref": "refs/heads/main", "sha": new_sha})
    if status not in (200, 201):
        say("[FAIL] 更新 ref：HTTP %s %s" % (status, body_ref))
        return flush(1)
    say("[PASS] 远端 main 已更新 → %s" % new_sha)
    say("")

    # ---- 6. 只记录远端 sha，**不**把本地分支指过去 ----
    # 曾经做过"把本地 main 指向 API 创建的提交"的本地对齐，结论是**不能做**：
    # 那个提交的 tree / parent 只存在于 GitHub，本机没有这些对象；
    # 一旦本地分支指过去，git status 立刻报
    #     error: bad tree object HEAD
    #     fatal: Failed to traverse parents of commit <sha>
    # 仓库反而变得不可用（只能靠 update-ref 手工救回）。
    # 所以：本地 main 保持为一条独立的本地提交线，内容与远端一致、历史不同。
    # 每次推送的 parent 都取远端当前 main，因此远端历史始终是线性快进。
    (config.ROOT / ".data" / "remote_main.txt").write_text(new_sha + "\n",
                                                          encoding="utf-8")
    say("[i] 远端 sha 已记录到 .data/remote_main.txt")
    say("    本地 main 未改动（%s）—— 本机与远端是两条**平行线**：内容一致、历史不同"
        % git("rev-parse", "--short", "HEAD").strip())
    say("    原因：API 创建的提交对象本机没有，硬指过去会让 git status/log 直接报错")
    say("")
    say("下一步：python tools/gh_actions.py watch   # 触发云端 workflow 并跟踪结果")
    return flush(0)


if __name__ == "__main__":
    sys.exit(main())

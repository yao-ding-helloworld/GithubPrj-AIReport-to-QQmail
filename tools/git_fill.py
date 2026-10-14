# -*- coding: utf-8 -*-
"""补齐本地缺失的提交对象。

场景：`tools/github_push.py` 通过 API 在远端创建提交，本地对象库里没有这些对象。
如果本地分支后来指向了它们（为了让本地/远端对齐），git 遍历历史时就会报
    fatal: Failed to traverse parents of commit <sha>
    error: Could not read <parent-sha>

本工具从 GitHub 把缺失的提交对象拉回来：按 git 的对象字节格式拼装、用 sha1 自校验，
通过后写入 .git/objects。因为 git 的对象是纯内容寻址的，只要字节拼对了，
算出来的 sha 必然等于目标 sha —— 这本身就是最强的正确性验证。

用法：python tools/git_fill.py [起始 sha]      # 默认取当前 HEAD
"""

import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402

LINES = []
MAX_DEPTH = 30


def say(text=""):
    LINES.append(str(text))


def git(*args, check=False):
    result = subprocess.run(["git"] + list(args), cwd=str(config.ROOT),
                            capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("utf-8", "replace").strip()


def has_object(sha):
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=str(config.ROOT),
                          capture_output=True).returncode == 0


def iso_to_epoch(iso):
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def build_commit_bytes(commit, message=None, tz="+0000"):
    """按 git 对象格式拼装提交字节（GitHub 存的日期一律为 +0000）。"""
    tree = commit["tree"]["sha"]
    parents = [p["sha"] for p in commit.get("parents", [])]
    raw = "tree %s\n" % tree
    for parent in parents:
        raw += "parent %s\n" % parent
    for role in ("author", "committer"):
        person = commit[role]
        raw += "%s %s <%s> %d %s\n" % (role, person["name"], person["email"],
                                       iso_to_epoch(person["date"]), tz)
    raw += "\n" + (commit["message"] if message is None else message)
    body = ("commit %d\0" % len(raw.encode("utf-8"))).encode("utf-8") + raw.encode("utf-8")
    return body, hashlib.sha1(body).hexdigest()


def dedent4(text):
    """去掉每行开头 4 个空格（`git log --pretty=raw` 会给消息体加缩进）。"""
    out = []
    for line in text.split("\n"):
        out.append(line[4:] if line.startswith("    ") else line)
    return "\n".join(out)


def variants(commit):
    """候选形态：不同 message 缩进/尾换行 + 不同时区，用 sha1 当判据择优。"""
    msg = commit["message"]
    candidates = [
        ("原样 +0000", msg, "+0000"),
        ("原样+换行 +0000", msg.rstrip() + "\n", "+0000"),
        ("去缩进 +0000", dedent4(msg), "+0000"),
        ("去缩进+换行 +0000", dedent4(msg).rstrip() + "\n", "+0000"),
        ("去缩进 +0800", dedent4(msg), "+0800"),
        ("去缩进+换行 +0800", dedent4(msg).rstrip() + "\n", "+0800"),
        ("原样 +0800", msg, "+0800"),
        ("原样+换行 +0800", msg.rstrip() + "\n", "+0800"),
    ]
    for label, message, tz in candidates:
        yield label, build_commit_bytes(commit, message, tz)


def write_object(sha, body):
    directory = config.ROOT / ".git" / "objects" / sha[:2]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sha[2:]).write_bytes(zlib.compress(body))


def api(path, token):
    req = urllib.request.Request(config.GITHUB_API + path, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
        "Authorization": "Bearer " + token,
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:200]}


def main(argv):
    env.load_env()
    token = env.github_token()
    if not token:
        say("[FAIL] .env 里没有 GITHUB_TOKEN")
        return 2

    url = git("remote", "get-url", "origin")
    path = url.split(":", 1)[1] if url.startswith("git@") else \
        urllib.parse.urlsplit(url).path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    owner, repo = [p for p in path.split("/") if p][:2]
    base = "/repos/%s/%s" % (owner, repo)

    start = argv[1] if len(argv) > 1 else (git("rev-parse", "HEAD") or None)
    if not start:
        say("[FAIL] 拿不到起始 sha")
        return 2

    say("== 补齐本地缺失的提交对象 ==")
    say("起点：%s" % start)
    say("")

    pending = [start]
    filled = 0
    failed = []
    seen = set()

    while pending and len(seen) < MAX_DEPTH:
        sha = pending.pop(0)
        if sha in seen:
            continue
        seen.add(sha)

        # 父提交一律从 API 学 —— 本地历史本身就是断的，不能用 git rev-list 走
        status, commit = api("%s/git/commits/%s" % (base, sha), token)
        if status != 200:
            say("[FAIL] 拉取 %s 失败：HTTP %s %s" % (sha[:7], status, commit))
            failed.append(sha)
            continue

        if has_object(sha):
            say("[skip] %s 本地已有" % sha[:7])
        else:
            body = None
            hit = None
            for label, candidate in variants(commit):
                if candidate[1] == sha:
                    body, hit = candidate[0], label
                    break
            if body is None:
                probe = ", ".join("%s→%s" % (lbl, c[1][:7])
                                  for lbl, c in variants(commit))
                say("[FAIL] %s 各形态均未命中：%s" % (sha[:7], probe))
                failed.append(sha)
                continue
            write_object(sha, body)
            filled += 1
            say("[PASS] 补入 %s  tree=%s  parent=%s  形态=%s  首行 %r"
                % (sha[:7], commit["tree"]["sha"][:7],
                   [p["sha"][:7] for p in commit.get("parents", [])], hit,
                   (commit["message"].splitlines() or [""])[0].strip()[:40]))

        pending.extend(p["sha"] for p in commit.get("parents", []))

    say("")
    say("补入 %d 个提交对象，遍历深度 %d" % (filled, len(seen)))
    if failed:
        say("[!] 以下对象未能补齐：%s" % ", ".join(m[:7] for m in failed))
        return 1

    say("")
    say("--- git log 验证 ---")
    log = git("log", "--oneline", "-5")
    say(log if log else "(空)")
    return 0


if __name__ == "__main__":
    code = main(sys.argv)
    (config.ROOT / ".data" / "gitfill.txt").write_text("\n".join(LINES), encoding="utf-8")
    print("\n".join(LINES))
    sys.exit(code)

# -*- coding: utf-8 -*-
"""从远端仓库拉取快照 CSV 并导入本地库（本机与云端数据对齐）。

用途：本机 `git fetch` 走不通（github.com 出网不稳），但 api.github.com 稳定。
这个工具用 Contents API 把仓库里的三份 CSV 下载下来，直接灌进本地 SQLite，
这样在本机也能看到与云端一致的趋势榜。

用法：python tools/pull_data.py
"""

import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import db          # noqa: E402
import env         # noqa: E402
import persist     # noqa: E402

OWNER, REPO = "yao-ding-helloworld", "GithubPrj-AIReport-to-QQmail"
FILES = {
    "repos": ".data/repos.csv",
    "snapshots": ".data/snapshots.csv",
    "windows": ".data/windows.csv",
}
LINES = []


def say(text=""):
    LINES.append(str(text))


def fetch(path, token):
    url = "%s/repos/%s/%s/contents/%s" % (config.GITHUB_API, OWNER, REPO, path)
    req = urllib.request.Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
        "Authorization": "Bearer " + token,
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    env.load_env()
    token = env.github_token()
    if not token:
        say("[FAIL] .env 里没有 GITHUB_TOKEN")
        return 1

    say("== 从云端拉取快照数据 ==")
    say("仓库：%s/%s" % (OWNER, REPO))
    say("")

    # 先把远端 CSV 落到本机 .data/（persist 直接读这些文件）
    for table, path in FILES.items():
        try:
            payload = fetch(path, token)
        except urllib.error.HTTPError as exc:
            say("[FAIL] %s 下载失败：HTTP %s" % (path, exc.code))
            return 1
        content = base64.b64decode(payload["content"])
        target = config.ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        lines = max(0, len([l for l in content.decode("utf-8-sig").splitlines()
                            if l.strip()]) - 1)
        say("[PASS] 下载 %-22s %6d 行  %7d 字节" % (path, lines, payload.get("size", 0)))
    say("")

    # 导入前先备份本地 CSV，避免误覆盖后找不回
    backup = config.ROOT / ".data" / "backup_before_pull"
    backup.mkdir(parents=True, exist_ok=True)
    for table, path in FILES.items():
        src = config.ROOT / path
        if src.exists():
            (backup / src.name).write_bytes(src.read_bytes())
    say("[i] 覆盖前已备份到 .data/backup_before_pull/")
    say("")

    conn = db.init()
    say("导入本地库：")
    persist.import_all(conn, clear=True, verbose=False)
    for table, path in FILES.items():
        rows = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        say("  %-10s %6d 行" % (table, rows))
    say("")
    say("库内统计：%s" % db.stats(conn))

    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT snap_date FROM snapshots ORDER BY snap_date")]
    say("快照日期：%s（共 %d 天）" % (", ".join(days), len(days)))
    return 0


if __name__ == "__main__":
    code = main()
    (config.ROOT / ".data" / "pulldata.txt").write_text("\n".join(LINES), encoding="utf-8")
    print("\n".join(LINES))
    sys.exit(code)

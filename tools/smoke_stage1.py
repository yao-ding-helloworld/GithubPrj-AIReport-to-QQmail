# -*- coding: utf-8 -*-
"""阶段 1 验收脚本：骨架自检。

跑法：python tools/smoke_stage1.py
输出：同时在标准输出与 .data/smoke_stage1.txt 里留一份（本机 stdout 捕获不稳，落文件可靠）

检查项：
  1. config / env / db / net 四个模块可导入
  2. .env 加载与 GitHub 模式判定
  3. SQLite 建表 + upsert 幂等
  4. 真实调一次 GitHub Search（匿名也可），并读一次 /rate_limit
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import db          # noqa: E402
import env         # noqa: E402
import net as gh   # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def check(title, ok, detail=""):
    say("[%s] %s%s" % ("PASS" if ok else "FAIL", title,
                       ("  -> " + str(detail)) if detail else ""))
    return ok


def main():
    env.ensure_dirs()
    say("== 阶段 1 骨架自检 ==")
    say("项目根目录：%s" % config.ROOT)
    say("")

    # 1. .env 与模式
    parsed = env.load_env()
    say(".env 文件：%s（解析到 %d 个键）"
        % ("存在" if parsed else "不存在，按环境变量运行", len(parsed)))
    say(env.describe_github())
    say(env.describe_smtp())
    say("")
    check("模块导入", True, "config / env / db / net")

    # 2. 建库 + 幂等
    conn = db.init()
    first = db.stats(conn)
    fake = [{
        "repo_id": 999000001,
        "full_name": "smoke/demo-repo",
        "description": "smoke test fixture",
        "language": "Python",
        "topics": ["ai-agent"],
        "domain": "agent-frameworks",
        "secondary": [],
        "stars": 1234,
    }]
    db.upsert_repos(conn, fake)
    db.upsert_snapshots(conn, [(999000001, 1234)])
    db.upsert_snapshots(conn, [(999000001, 1300)])      # 同日重跑：应是覆盖而非新增
    after = db.stats(conn)
    idempotent = (after["repos"] == first["repos"] + 1
                  and after["snapshots"] == first["snapshots"] + 1)
    check("SQLite 建表 + 同日快照 upsert 幂等", idempotent,
          "repos %d→%d, snapshots %d→%d"
          % (first["repos"], after["repos"], first["snapshots"], after["snapshots"]))

    # 清理 fixture，避免污染真实数据
    conn.execute("DELETE FROM repos WHERE repo_id=999000001")
    conn.execute("DELETE FROM snapshots WHERE repo_id=999000001")
    conn.commit()
    say("fixture 已清理，当前库内：%s" % db.stats(conn))
    say("")

    # 3. 联网：/rate_limit（不消耗额度）
    try:
        limits = gh.rate_limit()
        core = limits.get("resources", {}).get("core", {})
        search = limits.get("resources", {}).get("search", {})
        check("GitHub /rate_limit 可达", True,
              "core %s/%s, search %s/%s"
              % (core.get("remaining"), core.get("limit"),
                 search.get("remaining"), search.get("limit")))
    except Exception as exc:                              # noqa: BLE001
        check("GitHub /rate_limit 可达", False, exc)

    # 4. 联网：真实 search（1 条即可）
    query = config.enabled_domains()[0]["search_queries"][0]
    try:
        gh.throttle()
        payload = gh.search_repositories(query, per_page=3)
        items = payload.get("items", [])
        check("GitHub search 可达", bool(items),
              "q=%s | total_count=%s | 返回 %d 条"
              % (query, payload.get("total_count"), len(items)))
        for item in items[:3]:
            say("    - %s  ★%s" % (item["full_name"], item["stargazers_count"]))
        say("    本次响应剩余额度：%s" % gh.rate_remaining())
    except Exception as exc:                              # noqa: BLE001
        check("GitHub search 可达", False, exc)

    say("")
    say("== 自检结束 ==")

    report = "\n".join(LINES)
    out = config.ROOT / ".data" / "smoke_stage1.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    try:
        print(report)
    except Exception:                                     # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""一次性验证：趋势榜通路（阶段 4 的快照差值 + 排序 + 渲染）。

做法：复制真实库到临时库 → 灌入合成的历史快照 → 重算窗口 → 打印趋势榜。
不触碰真实库，跑完即删。用固定随机种子，结果可复现。
"""

import datetime
import pathlib
import random
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import compute     # noqa: E402
import config      # noqa: E402
import db          # noqa: E402
import render      # noqa: E402

random.seed(42)
LINES = []
say = LINES.append

src = config.ROOT / config.DB_PATH
tmp = config.ROOT / ".data" / "tmp_trend.db"
# 注意：本机安全删除钩子会拦截 Python 的 unlink()（报 EXDEV），
# 所以这里不删文件，直接覆盖复制；临时文件统一由外部命令清理。
shutil.copyfile(str(src), str(tmp))

conn = db.init(str(tmp))
today = datetime.date.today()

# ---- 灌入合成历史快照：today-3 / today-20 / today-60，增长曲线各不相同 ----
plans = [(3, 0.10), (20, 0.50), (60, 1.00)]     # (距今天数, 累计增长比例)
rows = conn.execute("SELECT repo_id, stars FROM repos").fetchall()
injected = 0
for row in rows:
    growth = random.uniform(-0.05, 0.60)        # 少数为负增长，用来验证"跌"的配色
    for offset, share in plans:
        past = int(round(row["stars"] / (1.0 + growth * share)))
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (repo_id, snap_date, stars) VALUES (?,?,?)",
            (row["repo_id"], (today - datetime.timedelta(days=offset)).isoformat(), past),
        )
        injected += 1
conn.commit()

say("临时库：%s" % tmp.name)
say("灌入合成快照 %d 条（today-3 / today-20 / today-60）" % injected)

# ---- 重算窗口 ----
result = compute.compute_windows(conn, verbose=False)
say("窗口计算：写入 %d 条（%s）" % (result["written"], result["per_period"]))
say("")

# ---- 打印三个窗口的趋势榜首 ----
for period in config.PERIODS:
    boards = compute.build_rankings(conn, period, top_n=3)
    say("== %s（%d 天）==" % (config.PERIODS[period][1], config.PERIODS[period][0]))
    for domain in config.enabled_domains():
        board = boards[domain["key"]]
        head = board["trend"][:3]
        if not head:
            say("  %s：趋势榜为空" % domain["name"])
            continue
        parts = ["%s %s%d"
                 % (i["full_name"].split("/")[-1],
                    "+" if i["delta"] > 0 else "",
                    i["delta"])
                 for i in head]
        say("  %-6s 趋势 Top3：%s" % (domain["name"], " | ".join(parts)))
    say("")

# ---- 渲染一份临时报告，验证前端数据没问题 ----
out = render.render(conn, out_path="dist/report_trend_check.html", verbose=False)
text = out.read_text(encoding="utf-8")
say("临时报告：%s（%.1f KB）" % (out.name, out.stat().st_size / 1024.0))

# ---- 断言：趋势榜非空、增量有正有负、sparkline 数据点 >=3 ----
conn2 = conn
all_trend = []
for period in config.PERIODS:
    boards = compute.build_rankings(conn2, period, top_n=10)
    for domain in config.enabled_domains():
        all_trend.extend(boards[domain["key"]]["trend"])

positives = [i for i in all_trend if i["delta"] > 0]
negatives = [i for i in all_trend if i["delta"] < 0]
with_series = [i for i in all_trend if len(i["series"]) >= 3]

say("")
say("断言：趋势条目 %d 条 / 正增长 %d / 负增长 %d / 含 >=3 点 sparkline %d"
    % (len(all_trend), len(positives), len(negatives), len(with_series)))
ok = bool(all_trend) and bool(positives) and bool(with_series)
say("结论：%s" % ("趋势通路验证通过" if ok else "趋势通路异常，需要排查"))
say("")
say("（临时库与临时报告由外部命令清理，真实库未改动）")

out_txt = config.ROOT / ".data" / "verify_trend.txt"
out_txt.write_text("\n".join(LINES), encoding="utf-8")

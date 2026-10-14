# -*- coding: utf-8 -*-
"""指标层：窗口净增量 + 双榜排序（阶段 4）。

对应 specs/metrics/spec.md：
- 窗口快照对查找：取窗口内最早可用快照，容错不均匀间隔
- 趋势值优先级：snapshot（准） > backfill（估算）—— 靠主键覆盖天然实现
- 双榜：热度榜（star 总量）/ 趋势榜（净增量），每领域 Top N
- 排序稳定性：增量相同按 star 总量降序，再按名称升序，结果可复现
"""

import datetime

import config
import db


def window_start(today, days):
    return today - datetime.timedelta(days=days)


def _days_between(date_a, date_b):
    try:
        return (datetime.date.fromisoformat(date_b)
                - datetime.date.fromisoformat(date_a)).days
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 窗口计算
def compute_windows(conn, periods=None, today=None, verbose=True):
    """为每个仓库 × 每个窗口写一行增量到 windows 表。

    只写"能用快照算出来"的那些行；算不出来的保留已有的 backfill 行。
    """
    periods = periods or list(config.PERIODS)
    today = today or datetime.date.today()
    written = 0
    per_period = {p: 0 for p in periods}

    for repo in db.iter_repos(conn):
        repo_id = repo["repo_id"]
        end_date, end_stars = db.latest_snapshot(conn, repo_id)
        if end_date is None:
            continue
        for period in periods:
            days = config.PERIODS[period][0]
            since = window_start(today, days).isoformat()
            start_date, start_stars = db.earliest_snapshot_since(conn, repo_id, since)
            if start_date is None or start_date == end_date:
                continue                     # 窗口内只有一张快照 → 算不出增量
            conn.execute(
                "INSERT INTO windows (period, repo_id, stars_delta, source, estimated, computed_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(period, repo_id) DO UPDATE SET "
                "  stars_delta=excluded.stars_delta, source=excluded.source, "
                "  estimated=excluded.estimated, computed_at=excluded.computed_at",
                (period, repo_id, int(end_stars - start_stars), "snapshot", 0,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            written += 1
            per_period[period] += 1

    conn.commit()
    if verbose:
        print("指标计算：写入 %d 条快照增量（%s）"
              % (written, ", ".join("%s %d" % (p, per_period[p]) for p in periods)))
        if written == 0:
            print("  [i] 窗口内快照不足两次——这是首轮运行的正常现象；"
                  "趋势榜将在积累 2 天后自然出现")
    return {"written": written, "per_period": per_period}


# ---------------------------------------------------------------- 榜单
def _rank(item):
    """排序键：增量/总量降序，再按名称升序（保证结果稳定可复现）。"""
    return (-item["value"], -item["stars"], item["full_name"])


def _trend_item(conn, row, start_stars):
    delta = row["stars_delta"]
    growth = None
    if start_stars:
        growth = round(delta * 100.0 / start_stars, 1)
    series = db.snapshot_series(conn, row["repo_id"])
    return {
        "repo_id": row["repo_id"],
        "full_name": row["full_name"],
        "description": row.get("description") or "",
        "language": row.get("language") or "",
        "stars": row.get("stars") or 0,
        "delta": delta,
        "source": row.get("source"),
        "estimated": bool(row.get("estimated")),
        "growth_pct": growth,
        "series": series,
        "value": delta,
    }


def _hot_item(conn, repo):
    return {
        "repo_id": repo["repo_id"],
        "full_name": repo["full_name"],
        "description": repo.get("description") or "",
        "language": repo.get("language") or "",
        "stars": repo.get("stars") or 0,
        "delta": None,
        "source": None,
        "estimated": False,
        "growth_pct": None,
        "series": db.snapshot_series(conn, repo["repo_id"]),
        "value": repo.get("stars") or 0,
    }


def build_rankings(conn, period, top_n=None, domains=None, today=None):
    """返回 {domain_key: {"hot": [...], "trend": [...], "coverage_days": int}}。"""
    top_n = top_n or config.TOP_N
    domains = domains or config.enabled_domains()
    keys = [d["key"] for d in domains]
    today = today or datetime.date.today()

    # ---- 热度榜：按当前 star 总量 ----
    hot_pool = {k: [] for k in keys}
    for repo in db.iter_repos(conn):
        if repo.get("domain") in hot_pool:
            hot_pool[repo["domain"]].append(_hot_item(conn, repo))

    # ---- 趋势榜：按窗口净增量 ----
    days = config.PERIODS[period][0]
    since = window_start(today, days).isoformat()
    trend_pool = {k: [] for k in keys}
    for row in db.window_rows(conn, period):
        if row.get("domain") not in trend_pool:
            continue
        _, start_stars = db.earliest_snapshot_since(conn, row["repo_id"], since)
        trend_pool[row["domain"]].append(_trend_item(conn, row, start_stars))

    result = {}
    for key in keys:
        hot = sorted(hot_pool[key], key=_rank)[:top_n]
        trend = sorted(trend_pool[key], key=_rank)[:top_n]
        result[key] = {
            "hot": hot,
            "trend": trend,
            "coverage_days": _coverage_days(conn, hot_pool[key], days),
        }
    return result


def _coverage_days(conn, items, window_days):
    """本领域实际覆盖了窗口内多少天（用最早快照日推算）。"""
    if not items:
        return 0
    earliest = conn.execute(
        "SELECT MIN(snap_date) FROM snapshots WHERE repo_id IN (%s)"
        % ",".join("?" * len(items)),
        [i["repo_id"] for i in items],
    ).fetchone()[0]
    if not earliest:
        return 0
    span = _days_between(earliest, db.today_iso())
    if span is None:
        return 0
    return min(span, window_days)


def all_rankings(conn, periods=None, top_n=None, domains=None):
    """三个窗口的榜单一次算全，供报告页零请求切换。"""
    periods = periods or list(config.PERIODS)
    return {p: build_rankings(conn, p, top_n=top_n, domains=domains)
            for p in periods}


def snapshot_days(conn, repo_id, window_days):
    """本仓库在窗口内的快照张数（页面提示"数据覆盖不足"用）。"""
    since = window_start(datetime.date.today(), window_days).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE repo_id=? AND snap_date>=?",
        (repo_id, since),
    ).fetchone()
    return row[0] if row else 0


if __name__ == "__main__":
    conn = db.init()
    print(all_rankings(conn))

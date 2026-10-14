# -*- coding: utf-8 -*-
"""采集层：候选池发现 + 当日快照 + 趋势回溯（阶段 2）。

对应 specs/data-collection/spec.md：
- 候选池发现：领域配置 → search 查询（主查询 + 新晋查询）→ 按 repo_id 去重合并
- 采集幂等：同日快照 upsert，重跑覆盖而非新增
- 限速退避：交给 net.py 统一处理；本层只做请求间隔节流与额度预检
- 趋势冷启动回溯：stargazers 尾页向前翻，仅 token 模式启用（ADR-2）

一次 search 响应自带 stargazers_count —— 发现与快照一次请求干两件事。
"""

import datetime
import math

import config
import db
import env
import net

# 回溯阶段的请求计数器（跨仓库累计，防止失控烧额度）
_backfill_requests = [0]

# 回溯可用性：None=未探测 / True=可用 / False=不可用
# 实测教训：某些环境里 /repos/{o}/{r}/stargazers 直接 404，或者忽略 star+json
# 请求头返回不带 starred_at 的裸用户对象。此时若继续跑，会把额度全烧在
# 注定失败的请求上（曾经一次烧掉 600 次请求、耗时数分钟、结果全是 0 增量）。
_backfill_available = [None]


def probe_backfill(repos, verbose=True):
    """先花 1~3 次请求探一下 stargazers 到底能不能用。"""
    if _backfill_available[0] is not None:
        return _backfill_available[0]

    for repo in repos[:3]:
        try:
            entries = net.stargazers(repo["full_name"], 1, per_page=5)
        except Exception as exc:                            # noqa: BLE001
            if verbose:
                print("  [!] 回溯探测失败（%s）：%s" % (repo["full_name"], exc))
            continue
        if not isinstance(entries, list):
            continue
        if not entries:
            continue
        if any("starred_at" in entry for entry in entries):
            _backfill_available[0] = True
            if verbose:
                print("  [i] 回溯探测通过：stargazers 返回带 starred_at，可用")
            return True
        _backfill_available[0] = False
        if verbose:
            print("  [!] stargazers 返回的条目没有 starred_at 字段"
                  "（star+json 未被支持），回溯不可用")
        return False

    _backfill_available[0] = False
    if verbose:
        print("  [!] stargazers 接口不可用（404 或无数据），本次跳过回溯")
    return False


# ---------------------------------------------------------------- 查询构建
def render_query(query, today=None):
    """把查询模板里的 {created_after} 换成绝对日期。

    GitHub 的 created: 只接受绝对日期（created:>2026-04-17），
    写 created:>180d 会 422 —— 所以模板在这里落地成具体日期。
    """
    today = today or datetime.date.today()
    created_after = (today - datetime.timedelta(days=config.NEWCOMER_DAYS)).isoformat()
    return query.replace("{created_after}", created_after)


def build_queries(domain, today=None):
    """把一个领域配置展开成 [(query, kind), ...]；kind = 'main' | 'newcomer'。"""
    queries = [(render_query(q, today), "main")
               for q in domain.get("search_queries", [])]
    newcomer = domain.get("newcomer_query")
    if newcomer:
        queries.append((render_query(newcomer, today), "newcomer"))
    return queries


def queries_for(domains=None, today=None):
    domains = domains or config.enabled_domains()
    total = []
    for domain in domains:
        for query, kind in build_queries(domain, today):
            total.append((domain["key"], query, kind))
    return total


# ---------------------------------------------------------------- 条目转换
def item_to_repo(item):
    """把 search 返回的一条 item 转成 repos 表的一行（domain 留空，交给 classify）。

    描述统一截断（config.DESC_MAX），避免超长文本污染存储、报告与关键词匹配。
    """
    return {
        "repo_id": item["id"],
        "full_name": item["full_name"],
        "description": config.clean_text(item.get("description")),
        "language": config.clean_text(item.get("language"), 40),
        "topics": item.get("topics") or [],
        "domain": None,
        "secondary": [],
        "stars": int(item.get("stargazers_count") or 0),
        "first_seen": db.today_iso(),
    }


def item_is_candidate(item):
    """候选门槛：不是 fork / archived、star 达标、且内容不在黑名单内。"""
    if item.get("fork"):
        return False
    if item.get("archived"):
        return False
    if int(item.get("stargazers_count") or 0) < config.STAR_MIN:
        return False
    haystack = "%s %s" % (item.get("full_name") or "",
                          (item.get("description") or "")[:2000])
    if config.is_blocked(haystack):
        return False
    return True


# ---------------------------------------------------------------- 额度预检
def search_quota():
    """读 search 池余量；(remaining, limit)，读不到返回 (None, None)。"""
    try:
        data = net.rate_limit()
        res = data.get("resources", {}).get("search", {})
        return res.get("remaining"), res.get("limit")
    except Exception:                                     # noqa: BLE001
        return None, None


# ---------------------------------------------------------------- 搜索执行
def run_searches(queries, per_page=100, pages=None, verbose=True):
    """执行全部查询，返回 {repo_id: (item, 命中领域列表)}。"""
    pages = pages or config.MAX_SEARCH_PAGES
    merged = {}
    failures = []
    for idx, (domain_key, query, kind) in enumerate(queries, 1):
        for page in range(1, pages + 1):
            net.throttle()
            try:
                payload = net.search_repositories(query, per_page=per_page, page=page)
            except Exception as exc:                      # noqa: BLE001
                failures.append((query, str(exc)))
                if verbose:
                    print("  [!] 查询失败，跳过：%s -> %s" % (query, exc))
                break
            items = payload.get("items") or []
            if verbose:
                print("  [%d/%d] %s (%s) 第 %d 页：total=%s 返回 %d 条"
                      % (idx, len(queries), query, kind, page,
                         payload.get("total_count"), len(items)))
            for item in items:
                if not item_is_candidate(item):
                    continue
                entry = merged.setdefault(
                    item["id"], {"item": item, "domains": set(), "kinds": set()})
                # 同仓库被多个查询命中时，保留 star 数更高的那条记录（理论上一致）
                if int(item.get("stargazers_count") or 0) > \
                        int(entry["item"].get("stargazers_count") or 0):
                    entry["item"] = item
                entry["domains"].add(domain_key)
                entry["kinds"].add(kind)
            if len(items) < per_page:
                break                                     # 该查询已到末页
    return merged, failures


# ---------------------------------------------------------------- 回溯（ADR-2）
def backfill_windows(full_name, stars, periods=None):
    """用 stargazers 尾页回溯估算各窗口净增量。

    返回 ({period: delta}, estimated) 或 (None, False) 表示不可用。
    - 仅 token 模式启用（匿名 60 req/h 撑不住）
    - 超大仓库分页受限 → estimated=True，值可能偏低
    - 一次后向遍历同时产出三个窗口的值（weekly ⊂ monthly ⊂ quarterly）
    """
    periods = periods or list(config.PERIODS)
    if not config.BACKFILL_ENABLED or env.github_mode() != "token":
        return None, False
    if _backfill_available[0] is False:       # 已知不可用，别再浪费请求
        return None, False
    if stars > config.BACKFILL_STAR_CEILING:
        return None, True

    today = datetime.date.today()
    cutoffs = {p: today - datetime.timedelta(days=config.PERIODS[p][0])
               for p in periods}
    longest = max(cutoffs.values())

    counts = {p: 0 for p in periods}
    estimated = False
    last_page = max(1, int(math.ceil(stars / 100.0)))
    done = False

    for page in range(last_page, 0, -1):
        if _backfill_requests[0] >= config.BACKFILL_MAX_REQUESTS:
            estimated = True
            break
        net.throttle(min_interval=0.3)
        _backfill_requests[0] += 1
        try:
            entries = net.stargazers(full_name, page)
        except Exception:                                 # noqa: BLE001
            estimated = True
            break
        if not entries:
            continue
        for entry in reversed(entries):                   # 页内升序 → 倒过来从新到旧
            raw = entry.get("starred_at")
            if not raw:
                estimated = True
                continue
            starred_on = datetime.date.fromisoformat(raw[:10])
            if starred_on < longest:                      # 越过最长窗口即可停
                done = True
                break
            for period, cutoff in cutoffs.items():
                if starred_on >= cutoff:
                    counts[period] += 1
        if done:
            break
    else:
        # 翻到第一页仍未越过窗口起点：说明仓库全部 star 都落在窗口内
        estimated = True

    return counts, estimated


# ---------------------------------------------------------------- 主入口
def collect(conn, domains=None, snap_date=None, do_backfill=None, max_queries=None,
            verbose=True):
    """执行一次采集，返回统计信息 dict。

    幂等：同日重复运行只会覆盖快照，不产生重复行。
    max_queries：只跑前 N 个查询（本地调试/额度紧张时用）。
    """
    domains = domains or config.enabled_domains()
    snap_date = snap_date or db.today_iso()
    if do_backfill is None:
        do_backfill = (config.BACKFILL_ENABLED and env.github_mode() == "token")

    queries = queries_for(domains)
    if max_queries:
        queries = queries[:max_queries]
    remaining, limit = search_quota()
    if verbose:
        print("采集开始：%d 个查询，search 池余量 %s/%s，日期 %s"
              % (len(queries), remaining, limit, snap_date))
        if remaining is not None and remaining < len(queries):
            print("  [!] search 余量不足以跑完全部查询，可能中途降级（结果仍可用，只是候选偏少）")

    merged, failures = run_searches(queries, verbose=verbose)
    if verbose:
        print("候选池合并去重后：%d 个仓库（跳过的查询：%d）" % (len(merged), len(failures)))

    repos = [item_to_repo(entry["item"]) for entry in merged.values()]
    db.upsert_repos(conn, repos)
    db.upsert_snapshots(conn, [(r["repo_id"], r["stars"]) for r in repos], snap_date)

    result = {
        "date": snap_date,
        "queries": len(queries),
        "queries_failed": len(failures),
        "repos": len(repos),
        "failures": failures,
        "backfilled": 0,
        "backfill_missing": 0,
    }

    # ---- 趋势冷启动回溯 ----
    if do_backfill and repos:
        # 先探测接口可用性，避免把额度烧在注定失败的请求上
        if not probe_backfill(repos, verbose=verbose):
            do_backfill = False
            result["backfill_skipped"] = True

    if do_backfill and repos:
        rows = []
        for repo in repos:
            deltas, estimated = backfill_windows(repo["full_name"], repo["stars"])
            if not deltas:
                result["backfill_missing"] += 1
                continue
            for period, delta in deltas.items():
                rows.append({
                    "period": period, "repo_id": repo["repo_id"],
                    "stars_delta": delta, "source": "backfill",
                    "estimated": 1 if estimated else 0,
                })
        if rows:
            db.upsert_windows(conn, rows)
            result["backfilled"] = len(rows)
        if verbose:
            print("回溯完成：写入 %d 条窗口估算值（消耗 %d 次请求）"
                  % (len(rows), _backfill_requests[0]))
    elif verbose:
        print("回溯：已跳过（%s）" % ("匿名模式" if env.github_mode() != "token"
                                     else "配置关闭"))

    result["stats"] = db.stats(conn)
    return result

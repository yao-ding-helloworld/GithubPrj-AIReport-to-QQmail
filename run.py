# -*- coding: utf-8 -*-
"""命令行入口：把采集 → 分类 → 指标 → 报告 → 推送串成一次运行。

用法示例：
  python run.py                                   # 全流程，三个窗口都算
  python run.py --no-collect                      # 只用库内快照重算并重渲染
  python run.py --max-queries 3                   # 只跑前 3 个查询（调试/省额度）
  python run.py --reclassify                      # 规则改动后重算领域标签
  python run.py --notify                          # 生成后推送邮件
  python run.py --no-collect --notify             # 只推送，不重新采集（云端补发用）
"""

import argparse
import sys
import time

import classify
import collect
import compute
import config
import db
import env
import render


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="GitHub AI 开源热点雷达：按周/月/季整理 AI 领域热门开源项目",
    )
    parser.add_argument("--period", default="all",
                        help="只算某个窗口（weekly/monthly/quarterly），默认三个都算")
    parser.add_argument("--top", type=int, default=config.TOP_N,
                        help="每领域每榜条数，默认 %d" % config.TOP_N)
    parser.add_argument("--out", default=config.REPORT_PATH, help="报告输出路径")
    parser.add_argument("--db", default=config.DB_PATH, help="SQLite 路径")
    parser.add_argument("--no-collect", action="store_true", help="跳过采集，只用库内数据")
    parser.add_argument("--no-render", action="store_true", help="跳过报告生成")
    parser.add_argument("--no-backfill", action="store_true", help="关闭 stargazers 回溯")
    parser.add_argument("--max-queries", type=int, default=None,
                        help="只跑前 N 个查询（调试或额度紧张时用）")
    parser.add_argument("--reclassify", action="store_true", help="全量重算领域标签")
    parser.add_argument("--resummarize", action="store_true",
                        help="强制重算项目简介（默认按内容指纹缓存）")
    parser.add_argument("--no-summary", action="store_true", help="跳过简介生成")
    parser.add_argument("--notify", action="store_true", help="生成后推送邮件")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    return parser


def resolve_periods(name):
    if name in ("all", None, ""):
        return list(config.PERIODS)
    if name not in config.PERIODS:
        raise SystemExit("未知窗口：%s（可选 %s 或 all）"
                         % (name, "/".join(config.PERIODS)))
    return [name]


def collect_ranked_ids(conn, periods=None, top_n=None):
    """收集所有窗口榜单里出现过的 repo_id。

    简介只给"会展示出来的仓库"生成，避免为 900 多个仓库白花钱（尤其配了 LLM 时）。
    """
    ids = set()
    boards = compute.all_rankings(conn, periods=periods, top_n=top_n)
    for per_domain in boards.values():
        for bucket in per_domain.values():
            for key in ("hot", "trend"):
                for item in bucket.get(key) or []:
                    ids.add(item["repo_id"])
    return ids


def main(argv=None):
    args = build_parser().parse_args(argv)
    verbose = not args.quiet
    periods = resolve_periods(args.period)

    env.load_env()
    env.ensure_dirs()

    if verbose:
        print("=" * 62)
        print("GitHub AI 开源热点雷达 v1")
        print("=" * 62)
        print(env.describe_github())

    conn = db.init(args.db)
    started = time.time()

    # ---- 采集 ----
    if not args.no_collect:
        if verbose:
            print("-" * 62)
        collect.collect(
            conn,
            snap_date=db.today_iso(),
            do_backfill=(False if args.no_backfill else None),
            max_queries=args.max_queries,
            verbose=verbose,
        )
    elif verbose:
        print("[i] 已跳过采集（--no-collect），使用库内已有快照")

    # ---- 分类 ----
    if verbose:
        print("-" * 62)
    classify.classify_repos(conn, reclassify=args.reclassify, verbose=verbose)

    # ---- 指标 ----
    if verbose:
        print("-" * 62)
    compute.compute_windows(conn, periods=periods, verbose=verbose)

    # ---- 简介 ----
    # 放在指标之后、渲染之前：只有榜单会展示的仓库才需要简介，
    # 这样即使配了 LLM 也只对少量条目调用（省钱）。
    if config.SUMMARY_ENABLED and not args.no_summary:
        if verbose:
            print("-" * 62)
        try:
            import summarize
            ranked = collect_ranked_ids(conn, periods=periods, top_n=args.top)
            repos = [r for r in db.iter_repos(conn) if r["repo_id"] in ranked]
            summarize.summarize_repos(conn, repos=repos, force=args.resummarize,
                                      verbose=verbose)
        except Exception as exc:                            # noqa: BLE001
            print("[!] 简介生成失败，报告改用原始描述：%s: %s"
                  % (type(exc).__name__, exc))

    # ---- 报告 ----
    report_path = None
    if not args.no_render:
        if verbose:
            print("-" * 62)
        report_path = render.render(conn, out_path=args.out, top_n=args.top,
                                   periods=periods, verbose=verbose)

    # ---- 推送 ----
    if args.notify:
        if verbose:
            print("-" * 62)
        try:
            import digest
            import notify
            # 摘要只覆盖一个窗口（默认周榜），月/季榜在附件报告里
            period_key = periods[0] if periods else "weekly"
            data = digest.collect_digest(conn, period_key=period_key,
                                         top_n=config.MAIL_TOP_N)
            markdown = digest.to_markdown(data)
            html_body = digest.to_email_html(data)
            if verbose:
                print("邮件正文：纯文本 %d 字符 / HTML %d 字符"
                      % (len(markdown), len(html_body)))
            notify.send_digest(markdown, subject=digest.subject(period_key),
                               html_body=html_body, html_path=report_path,
                               verbose=verbose)
        except Exception as exc:                            # noqa: BLE001
            # 规格要求：推送失败不得导致整体运行失败（报告与快照已落盘）
            print("[!] 推送环节异常，但不影响本次运行结果：%s: %s"
                  % (type(exc).__name__, exc))

    if verbose:
        print("-" * 62)
        print("完成，用时 %.1f 秒。库内统计：%s" % (time.time() - started, db.stats(conn)))
        if report_path:
            print("打开报告：%s" % report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

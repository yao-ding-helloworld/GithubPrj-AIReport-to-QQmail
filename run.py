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
            text = digest.render_digest(conn, periods=periods, top_n=args.top)
            notify.send_digest(text, html_path=report_path, verbose=verbose)
        except ImportError as exc:
            print("[!] 推送模块尚未就绪：%s" % exc)

    if verbose:
        print("-" * 62)
        print("完成，用时 %.1f 秒。库内统计：%s" % (time.time() - started, db.stats(conn)))
        if report_path:
            print("打开报告：%s" % report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

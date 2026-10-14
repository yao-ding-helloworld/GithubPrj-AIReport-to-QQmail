# -*- coding: utf-8 -*-
"""摘要生成：榜单 → Markdown（阶段 7.2）。

对应 specs/scheduled-delivery/spec.md 的「摘要生成」需求：
- 内容含统计窗口、生成时间、各领域趋势 Top N 与热度 Top N
- 某领域无趋势数据时标注"趋势数据待生成"，但不省略该领域
- 超过长度上限时按领域压缩成每条目一行，并提示"完整榜单见附件报告"

同时提供一个极简 Markdown → HTML 转换器，供邮件正文的 HTML 版本使用
（只支持本模块自己产出的那几种语法，够用即可，不引入依赖）。
"""

import datetime
import re

import config
import compute
import db

MAX_DIGEST_CHARS = 8000      # 超过此长度触发压缩


def fmt_stars(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "-"
    if value >= 1_000_000:
        return "%.1fM" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%.1fk" % (value / 1_000.0)
    return str(value)


def _line_hot(index, item):
    return "%d. `%s` — ★%s%s" % (
        index, item["full_name"], fmt_stars(item["stars"]),
        ("  " + item["language"]) if item.get("language") else "",
    )


def _line_trend(index, item):
    delta = item.get("delta")
    sign = "+" if (delta or 0) > 0 else ""
    growth = ""
    if item.get("growth_pct") is not None:
        growth = "（%s%.1f%%）" % ("+" if item["growth_pct"] > 0 else "", item["growth_pct"])
    est = " [估]" if item.get("estimated") else ""
    return "%d. `%s` — ★%s，窗口净增 %s%s%s%s" % (
        index, item["full_name"], fmt_stars(item["stars"]),
        sign, fmt_stars(abs(delta) if delta else 0), growth, est,
    )


def render_digest(conn, period_key=None, top_n=None, domains=None,
                  generated_at=None, verbose=True):
    """生成 Markdown 摘要文本。"""
    period_key = period_key or "weekly"
    if period_key not in config.PERIODS:
        period_key = list(config.PERIODS)[0]
    top_n = top_n or 3
    domains = domains or config.enabled_domains()
    generated_at = generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    days, label = config.PERIODS[period_key]
    boards = compute.build_rankings(conn, period_key, top_n=top_n, domains=domains)
    stats = db.stats(conn)

    lines = []
    lines.append("# AI 开源热点雷达 · %s" % label)
    lines.append("")
    lines.append("统计窗口：%d 天 · 生成时间：%s · 累计快照：%d 天 · 候选仓库：%d 个"
                 % (days, generated_at, stats["snapshot_days"], stats["repos"]))
    lines.append("")

    for index, domain in enumerate(domains, 1):
        board = boards.get(domain["key"], {"hot": [], "trend": []})
        lines.append("## %d. %s" % (index, domain["name"]))
        lines.append("")

        lines.append("**趋势榜 Top %d**（窗口净增 star）" % top_n)
        lines.append("")
        if board["trend"]:
            for i, item in enumerate(board["trend"], 1):
                lines.append(_line_trend(i, item))
        else:
            lines.append("- 趋势数据待生成（需要至少两天的快照积累）")
        lines.append("")

        lines.append("**热度榜 Top %d**（star 总量）" % top_n)
        lines.append("")
        for i, item in enumerate(board["hot"], 1):
            lines.append(_line_hot(i, item))
        lines.append("")

    # 长度约束：超限则退化为"每领域只留榜首一条"
    text = "\n".join(lines)
    if len(text) > MAX_DIGEST_CHARS:
        lines = lines[:4]
        for index, domain in enumerate(domains, 1):
            board = boards.get(domain["key"], {"hot": [], "trend": []})
            lines.append("## %d. %s" % (index, domain["name"]))
            lines.append("")
            if board["trend"]:
                lines.append("- 趋势榜首：%s" % _line_trend(1, board["trend"][0])[3:])
            if board["hot"]:
                lines.append("- 热度榜首：%s" % _line_hot(1, board["hot"][0])[3:])
            lines.append("")
        lines.append("（摘要过长，已按领域压缩；完整榜单见附件报告）")
        text = "\n".join(lines)

    if verbose:
        print("摘要已生成：%d 字符，%d 个领域" % (len(text), len(domains)))
    return text


def subject(period_key="weekly", today=None):
    """邮件主题，形如 [AI 开源雷达] 周榜 · 2026-10-14。"""
    today = today or datetime.date.today().isoformat()
    return "[AI 开源雷达] %s · %s" % (config.PERIODS.get(period_key, ("", "榜单"))[1], today)


# ---------------------------------------------------------------- Markdown → HTML
def _inline(text):
    """行内语法：**粗体**、`代码`，并转义 HTML。"""
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def md_to_html(markdown):
    """极简转换器：只处理标题 / 列表 / 粗体 / 代码，够邮件正文用。"""
    out = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<h3 style=\"margin:18px 0 8px;font-size:15px;\">%s</h3>"
                       % _inline(line[3:]))
        elif line.startswith("# "):
            out.append("<h2 style=\"margin:0 0 10px;font-size:18px;\">%s</h2>"
                       % _inline(line[2:]))
        elif re.match(r"^\d+\.\s", line) or line.startswith("- "):
            if not in_list:
                out.append("<ul style=\"margin:6px 0 6px 18px;padding:0;\">")
                in_list = True
            body = re.sub(r"^(\d+\.\s|-\s)", "", line)
            out.append("<li style=\"margin:3px 0;\">%s</li>" % _inline(body))
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<p style=\"margin:6px 0;color:#57606a;\">%s</p>" % _inline(line))
    if in_list:
        out.append("</ul>")
    return (
        "<!DOCTYPE html><html><body style=\"font:14px/1.6 -apple-system,"
        "'Segoe UI','Microsoft YaHei',sans-serif;color:#1c1f23;\">"
        + "\n".join(out) + "</body></html>"
    )


if __name__ == "__main__":
    conn = db.init()
    print(render_digest(conn))

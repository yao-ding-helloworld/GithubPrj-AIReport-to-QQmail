# -*- coding: utf-8 -*-
"""摘要层：把榜单变成「纯文本 + 移动优先 HTML 邮件正文」。

对应 specs/scheduled-delivery/spec.md 的「摘要生成」需求。

为什么要把邮件正文做成正经的 HTML 邮件（而不是一小段 markdown）：
手机上（尤其 iOS）打开邮件时，很多人不会去点附件；而且 iOS 预览 HTML 附件时
**不执行 JavaScript**，交互式报告在手机上就是个空壳。所以正文必须自带内容：
单列、大字号、内联样式、纯 div、零 JS、零外部资源。

一份数据源 `collect_digest()`，两个渲染器：
    to_markdown()   —— 纯文本分支（老客户端/纯文本模式兜底）
    to_email_html() —— HTML 分支（手机优先，直接可读）
"""

import datetime
import html
import re

import config
import compute
import db

MAX_DIGEST_CHARS = 12000      # 纯文本分支超过此长度则压缩


# ---------------------------------------------------------------- 小工具
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


def fmt_delta(value):
    if value is None:
        return "-"
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "-"
    return ("+" if value > 0 else ("-" if value < 0 else "")) + fmt_stars(abs(value))


def _esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


# ---------------------------------------------------------------- 数据
def collect_digest(conn, period_key=None, top_n=None, domains=None,
                   generated_at=None):
    """把某个窗口的榜单收成一份结构化数据，供两种渲染器共用。"""
    period_key = period_key or "weekly"
    if period_key not in config.PERIODS:
        period_key = list(config.PERIODS)[0]
    top_n = top_n or config.MAIL_TOP_N
    domains = domains or config.enabled_domains()
    generated_at = generated_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    days, label = config.PERIODS[period_key]
    boards = compute.build_rankings(conn, period_key, top_n=top_n, domains=domains)
    stats = db.stats(conn)

    sections = []
    for domain in domains:
        board = boards.get(domain["key"], {"hot": [], "trend": []})
        sections.append({
            "name": domain["name"],
            "trend": board.get("trend") or [],
            "hot": board.get("hot") or [],
        })

    return {
        "period_key": period_key,
        "period_label": label,
        "days": days,
        "generated_at": generated_at,
        "snapshot_days": stats["snapshot_days"],
        "repo_count": stats["repos"],
        "top_n": top_n,
        "sections": sections,
    }


# ---------------------------------------------------------------- Markdown
def to_markdown(data):
    lines = ["# AI 开源热点雷达 · %s" % data["period_label"], ""]
    lines.append("统计窗口：%d 天 · 生成时间：%s · 累计快照：%d 天 · 候选仓库：%d 个"
                 % (data["days"], data["generated_at"], data["snapshot_days"],
                    data["repo_count"]))
    lines.append("")

    for index, section in enumerate(data["sections"], 1):
        lines.append("## %d. %s" % (index, section["name"]))
        lines.append("")
        lines.append("**趋势榜（窗口净增 star）**")
        lines.append("")
        if section["trend"]:
            for i, item in enumerate(section["trend"], 1):
                growth = ("（%+.1f%%）" % item["growth_pct"]
                          if item.get("growth_pct") is not None else "")
                lines.append("- %d. %s — ★%s，净增 %s%s%s%s"
                             % (i, item["full_name"], fmt_stars(item["stars"]),
                                fmt_delta(item["delta"]), growth,
                                " [估]" if item.get("estimated") else "",
                                ("　" + item["summary"]) if item.get("summary") else ""))
        else:
            lines.append("- 趋势数据待生成（需要至少两天的快照积累）")
        lines.append("")
        lines.append("**热度榜（star 总量）**")
        lines.append("")
        for i, item in enumerate(section["hot"], 1):
            lines.append("- %d. %s — ★%s%s"
                         % (i, item["full_name"], fmt_stars(item["stars"]),
                            ("　" + item["summary"]) if item.get("summary") else ""))
        lines.append("")

    text = "\n".join(lines)

    # 长度约束：超限则每榜只留榜首
    if len(text) > MAX_DIGEST_CHARS:
        lines = lines[:4]
        for index, section in enumerate(data["sections"], 1):
            lines.append("## %d. %s" % (index, section["name"]))
            lines.append("")
            if section["trend"]:
                top = section["trend"][0]
                lines.append("- 趋势榜首：%s ★%s 净增 %s"
                             % (top["full_name"], fmt_stars(top["stars"]),
                                fmt_delta(top["delta"])))
            if section["hot"]:
                top = section["hot"][0]
                lines.append("- 热度榜首：%s ★%s"
                             % (top["full_name"], fmt_stars(top["stars"])))
            lines.append("")
        lines.append("（摘要过长，已按领域压缩；完整榜单见附件报告）")
        text = "\n".join(lines)

    return text


# ---------------------------------------------------------------- HTML 邮件
FONT = ("-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei',sans-serif")


def _item_html(index, item, show_delta):
    """一条榜单记录：单列、大字号，手机上不会横向溢出。"""
    name = item.get("full_name") or ""
    parts = [
        '<span style="color:#9aa0a6;font-size:13px;">%d</span>' % index,
        '<a href="https://github.com/%s" style="color:#1a56db;text-decoration:none;'
        'font-weight:600;font-size:14px;">%s</a>' % (_esc(name), _esc(name)),
        '<span style="color:#6b7280;font-size:12.5px;">★%s</span>'
        % fmt_stars(item.get("stars")),
    ]
    if show_delta and item.get("delta") is not None:
        delta = item["delta"]
        color = "#c62828" if delta > 0 else ("#2e7d32" if delta < 0 else "#6b7280")
        txt = fmt_delta(delta)
        if item.get("growth_pct") is not None:
            txt += " (%+.1f%%)" % item["growth_pct"]
        if item.get("estimated"):
            txt += " 估"
        parts.append('<span style="color:%s;font-size:12.5px;font-weight:600;">%s</span>'
                     % (color, txt))
    if item.get("language"):
        parts.append('<span style="color:#9aa0a6;font-size:11.5px;">%s</span>'
                     % _esc(item["language"]))

    summary = item.get("summary") or item.get("description") or ""
    summary_html = ('<div style="color:#57606a;font-size:12.5px;line-height:1.55;'
                    'margin-top:3px;">%s</div>' % _esc(summary)) if summary else ""

    return ('<div style="padding:9px 0;border-bottom:1px solid #eef1f4;">'
            '<div style="line-height:1.55;">%s</div>%s</div>'
            % ("&nbsp; ".join(parts), summary_html))


def _board_html(title, icon, items, show_delta, empty_hint):
    if not items:
        body = ('<div style="color:#9aa0a6;font-size:12.5px;padding:6px 0;">%s</div>'
                % _esc(empty_hint))
    else:
        body = "".join(_item_html(i, item, show_delta)
                       for i, item in enumerate(items, 1))
    return ('<div style="margin:14px 0 4px;font-size:13px;font-weight:600;'
            'color:#374151;">%s %s</div>%s' % (icon, _esc(title), body))


def to_email_html(data):
    """移动优先的 HTML 邮件正文：单列、内联样式、零 JS、零外部资源。"""
    blocks = [
        '<div style="max-width:600px;margin:0 auto;padding:18px 16px 26px;'
        'font-family:%s;font-size:14px;line-height:1.6;color:#1c1f23;">' % FONT,
        '<div style="font-size:19px;font-weight:700;letter-spacing:.2px;">'
        'GitHub AI 开源热点雷达 · %s</div>' % _esc(data["period_label"]),
        '<div style="color:#6b7280;font-size:12.5px;margin-top:6px;">'
        '统计窗口 %d 天 · 生成时间 %s<br>累计快照 %d 天 · 候选仓库 %d 个</div>'
        % (data["days"], _esc(data["generated_at"]),
           data["snapshot_days"], data["repo_count"]),
    ]

    if data["snapshot_days"] < data["days"]:
        blocks.append(
            '<div style="background:#fff8e1;border:1px solid #f0dfa8;border-radius:6px;'
            'padding:8px 11px;margin-top:12px;font-size:12.5px;color:#7a6200;">'
            '累计快照 %d 天，短于窗口 %d 天：趋势值为窗口内已有快照的净增量，'
            '随每日采集自动变准。</div>'
            % (data["snapshot_days"], data["days"]))

    for index, section in enumerate(data["sections"], 1):
        blocks.append(
            '<div style="margin:22px 0 0;padding-top:14px;border-top:2px solid #eef1f4;">'
            '<div style="font-size:16px;font-weight:700;">%d. %s</div></div>'
            % (index, _esc(section["name"])))
        blocks.append(_board_html("趋势榜（窗口净增 star）", "▲",
                                  section["trend"], True,
                                  "趋势数据待生成（需要至少两天的快照）"))
        blocks.append(_board_html("热度榜（star 总量）", "★",
                                  section["hot"], False, "暂无数据"))

    blocks.append(
        '<div style="margin-top:24px;padding-top:14px;border-top:1px solid #eef1f4;'
        'color:#9aa0a6;font-size:11.5px;line-height:1.6;">'
        '口径：热度榜＝当前 star 总量；趋势榜＝窗口内净增量（以每日快照差值为准）。'
        '带「估」字为估算值。数据来自 GitHub 官方 API，仅供选题参考。<br>'
        '完整榜单与交互筛选见附件 report.html（桌面端打开体验最佳）。</div>')
    blocks.append("</div>")
    return ("<!DOCTYPE html><html><body style=\"margin:0;padding:0;background:#ffffff;\">"
            + "".join(blocks) + "</body></html>")


# ---------------------------------------------------------------- 兼容入口
def render_digest(conn, period_key=None, top_n=None, domains=None,
                  generated_at=None, verbose=True):
    """生成 Markdown 摘要（旧入口，保留兼容）。"""
    data = collect_digest(conn, period_key=period_key, top_n=top_n,
                          domains=domains, generated_at=generated_at)
    text = to_markdown(data)
    if verbose:
        print("摘要已生成：%d 字符，%d 个领域" % (len(text), len(data["sections"])))
    return text


def subject(period_key="weekly", today=None):
    """邮件主题，形如 [AI 开源雷达] 周榜 · 2026-10-14。"""
    today = today or datetime.date.today().isoformat()
    return "[AI 开源雷达] %s · %s" % (config.PERIODS.get(period_key, ("", "榜单"))[1],
                                     today)


# ---------------------------------------------------------------- Markdown → HTML
def _inline(text):
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def md_to_html(markdown):
    """极简转换器：只处理标题 / 列表 / 粗体 / 代码（打包邮件等简单场景用）。"""
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
    return ("<!DOCTYPE html><html><body style=\"font:14px/1.6 %s;color:#1c1f23;\">"
            % FONT + "\n".join(out) + "</body></html>")


if __name__ == "__main__":
    conn = db.init()
    print(render_digest(conn))

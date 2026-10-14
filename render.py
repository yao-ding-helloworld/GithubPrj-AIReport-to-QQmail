# -*- coding: utf-8 -*-
"""报告层：单文件交互 HTML（阶段 5）。

对应 specs/report-generation/spec.md 与 ADR-6：
- 数据以 const DATA = {...} 内联，三个窗口全量预算好 → 切换零请求
- 交互用原生 JS（无框架）：时间窗 tab × 榜单类型 × 领域多选 × 关键字搜索
- 图表手写 SVG：榜单用条形 meter，趋势用 sparkline（快照 <3 张则不渲染）
- 响应式：CSS grid + 媒体查询，窄屏自动单列
- 浅色主题；涨红跌绿（A 股口径）
"""

import datetime
import html
import json
import pathlib

import config
import compute
import db


# 每个领域一个主色：卡片左侧色条 + 卡片标题图标都用它。
# Python 静态兜底与前端 JS 用的是同一组值（前端那边的 ACCENTS 要与此保持一致）。
ACCENTS = ["#4f46e5", "#0d9488", "#d97706", "#db2777", "#7c3aed", "#0284c7"]


# ---------------------------------------------------------------- 小工具
def fmt_stars(value):
    """12345 -> 12.3k，1234567 -> 1.2M。"""
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
        return ""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return ""
    return ("+" if value > 0 else "") + fmt_stars(value) if abs(value) >= 1000 \
        else ("+" if value > 0 else "") + str(value)


_spark_seq = [0]


def sparkline(series, width=132, height=30):
    """把 [(date, stars)] 画成带渐变填充的折线；点数 <3 返回空串（规格要求整体隐藏）。"""
    if not series or len(series) < 3:
        return ""
    values = [float(v) for _, v in series]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / float(len(values) - 1)
    points = []
    for idx, value in enumerate(values):
        x = idx * step
        y = height - 3 - (value - lo) / span * (height - 6)
        points.append("%.1f,%.1f" % (x, y))
    color = "#c62828" if values[-1] >= values[0] else "#2e7d32"
    line = " ".join(points)
    area = "0,%.1f %s %.1f,%.1f" % (height, line, width, height)
    _spark_seq[0] += 1
    gid = "spk%d" % _spark_seq[0]
    return (
        '<svg class="spark" viewBox="0 0 %d %d" width="%d" height="%d" '
        'preserveAspectRatio="none" aria-hidden="true">'
        '<defs><linearGradient id="%s" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%%" stop-color="%s" stop-opacity=".30"/>'
        '<stop offset="1%%" stop-color="%s" stop-opacity="0"/>'
        '</linearGradient></defs>'
        '<polygon fill="url(#%s)" points="%s"/>'
        '<polyline fill="none" stroke="%s" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round" points="%s"/>'
        '</svg>'
        % (width, height, width, height, gid, color, color, gid, area, color, line)
    )


# ---------------------------------------------------------------- 静态预渲染
# 为什么要有这一段：
# 页面原本完全靠 JS 渲染，一旦渲染环境禁用内联脚本（预览面板、邮件客户端预览、
# 严格 CSP），页面就只剩"空架子"——结构在、榜单区一片空白。
# 所以构建时就把默认视图（周榜的 趋势榜 + 热度榜）编译成静态 HTML 塞进去；
# 脚本可用时 render() 会立即覆盖它，脚本被禁时这就是最终内容。
# 纯增强，不牺牲交互。

def _esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _fmt_delta(value):
    if value is None:
        return "-"
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return sign + fmt_stars(abs(value))


def _svg_icon(name, cls="ico"):
    return ('<svg class="%s" viewBox="0 0 24 24" aria-hidden="true">'
            '<use href="#%s"/></svg>' % (cls, name))


def _arrow_icon(delta):
    """涨用红上箭头、跌用绿下箭头（A 股配色）。"""
    if delta is None or delta == 0:
        return ""
    return _svg_icon("i-up" if delta > 0 else "i-down", "ico ico-sm")


def _rank_cell(rank):
    """前三名给奖牌徽章，其余用普通序号。"""
    if rank <= 3:
        return '<span class="medal m%d">%d</span>' % (rank, rank)
    return str(rank)


def _row_html(item, rank, max_value):
    delta = item.get("delta")
    if delta is None:
        delta_cell = '<span class="sub">-</span>'
    else:
        cls = "up" if delta > 0 else ("down" if delta < 0 else "")
        growth = item.get("growth_pct")
        pct = ""
        if growth is not None:
            pct = ' <span class="pct">(%s%.1f%%)</span>' % (
                "+" if growth > 0 else "", growth)
        delta_cell = ('<span class="delta %s">%s%s</span>%s'
                      % (cls, _arrow_icon(delta), _fmt_delta(delta), pct))
    est = ('<span class="est" title="超大仓库分页受限，回溯值可能偏低">估</span>'
           if item.get("estimated") else "")
    bar = 3
    if max_value > 0:
        bar = max(3, int(round(abs(item.get("value") or 0) * 100.0 / max_value)))
    # 优先展示中文简介；原始英文描述挂到 title 上（鼠标悬停可见）
    summary = item.get("summary") or ""
    description = item.get("description") or ""
    text_line = summary or description
    title_attr = (' title="%s"' % _esc(description)) if (summary and description) else ""
    desc = ('<div class="desc"%s>%s</div>' % (title_attr, _esc(text_line))) \
        if text_line else ""
    lang = ('<span class="lang">%s</span>' % _esc(item.get("language"))
            if item.get("language") else "")
    name = item.get("full_name") or ""
    return (
        '<tr>'
        '<td class="rk">%s</td>'
        '<td class="proj">'
        '<a href="https://github.com/%s" target="_blank" rel="noopener">%s</a>%s%s%s'
        '<div class="meter"><i style="width:%d%%"></i></div>'
        '</td>'
        '<td class="num">%s</td>'
        '<td class="num hide-sm">%s</td>'
        '<td class="hide-sm">%s</td>'
        '</tr>'
        % (_rank_cell(rank), _esc(name), _esc(name), lang, est, desc,
           bar, fmt_stars(item.get("stars")), delta_cell,
           sparkline(item.get("series")))
    )


def _card_html(title, meta, items, board_type, accent="#4f46e5"):
    if items:
        max_value = max(abs(x.get("value") or 0) for x in items)
        rows = "".join(_row_html(it, i, max_value)
                       for i, it in enumerate(items, 1))
        body = (
            '<table><thead><tr><th>#</th><th>项目</th>'
            '<th class="num">%s Star</th>'
            '<th class="num hide-sm">窗口增量</th><th class="hide-sm">走势</th></tr>'
            '</thead><tbody>%s</tbody></table>'
            % (_svg_icon("i-repo", "ico ico-sm"), rows)
        )
    else:
        body = ('<div class="none">%s该领域暂无%s数据（趋势榜需要至少两天的快照）</div>'
                % (_svg_icon("i-empty", "ico ico-sm"),
                   "趋势" if board_type == "trend" else "热度"))

    is_trend = board_type == "trend"
    head_icon = _svg_icon("i-trend" if is_trend else "i-flame", "ico")
    badge = ('<span class="badge %s">%s%s</span>'
             % ("badge-trend" if is_trend else "badge-hot",
                _svg_icon("i-trend" if is_trend else "i-flame", "ico ico-sm"),
                "趋势" if is_trend else "热度"))
    return ('<section class="card" style="--daccent:%s">'
            '<div class="card-head"><h2>%s%s</h2>'
            '<div class="meta">%s%s</div></div>%s</section>'
            % (accent, head_icon, _esc(title), badge, _esc(meta), body))


def static_boards(payload, period_key=None):
    """把默认视图编译成静态 HTML（脚本不可用时的兜底内容）。"""
    periods = list(payload["periods"])
    period_key = period_key or periods[0]
    label = payload["periods"][period_key]["label"]
    boards = payload["boards"].get(period_key, {})
    parts = []
    for di, domain in enumerate(payload["domains"]):
        board = boards.get(domain["key"]) or {"hot": [], "trend": []}
        accent = ACCENTS[di % len(ACCENTS)]
        for board_type, suffix in (("trend", "趋势榜"), ("hot", "热度榜")):
            items = board.get(board_type) or []
            parts.append(_card_html(
                "%s · %s" % (domain["name"], suffix),
                "%s · %d 条" % (label, len(items)), items, board_type, accent))
    return "".join(parts)


# ---------------------------------------------------------------- 数据准备
def build_payload(conn, periods=None, top_n=None, domains=None):
    """把三个窗口的榜单整理成前端直接可用的结构。"""
    periods = periods or list(config.PERIODS)
    domains = domains or config.enabled_domains()
    rankings = compute.all_rankings(conn, periods=periods, top_n=top_n, domains=domains)
    stats = db.stats(conn)

    period_meta = {}
    for key in periods:
        days, label = config.PERIODS[key]
        period_meta[key] = {"days": days, "label": label}

    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": db.today_iso(),
        "snapshot_days": stats["snapshot_days"],
        "repo_count": stats["repos"],
        "periods": period_meta,
        "domains": [{"key": d["key"], "name": d["name"]} for d in domains],
        "boards": rankings,
        "disclaimer": (
            "口径：热度榜 = 当前 star 总量；趋势榜 = 窗口内净增量"
            "（快照差值为准，冷启动期使用 stargazers 回溯估算）。"
            "带“估”字角标的条目为估算值，超大仓库分页受限时可能偏低。"
            "数据来自 GitHub 官方 API，仅供选题参考，不构成任何投资或技术选型建议。"
        ),
    }


# ---------------------------------------------------------------- 模板
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub AI 开源热点雷达</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --line: #e3e6ea; --line-soft: #eef1f4;
    --text: #1c1f23; --muted: #6b7280; --accent: #1a56db;
    --up: #c62828; --down: #2e7d32; --chip: #eef2ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, "Segoe UI", "Microsoft YaHei", Roboto, sans-serif;
  }
  header {
    background: var(--panel); border-bottom: 1px solid var(--line);
    padding: 20px 24px 16px;
  }
  h1 { margin: 0 0 6px; font-size: 20px; letter-spacing: .2px; }
  .sub { color: var(--muted); font-size: 12.5px; }
  .sub b { color: var(--text); font-weight: 600; }
  .controls {
    position: sticky; top: 0; z-index: 5;
    background: rgba(255,255,255,.96); backdrop-filter: blur(6px);
    border-bottom: 1px solid var(--line);
    padding: 12px 24px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
  }
  .group { display: flex; gap: 6px; align-items: center; }
  .group > span.lbl { color: var(--muted); font-size: 12px; margin-right: 2px; }
  button.tab, button.chip {
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    border-radius: 999px; padding: 5px 13px; font-size: 13px; cursor: pointer;
    transition: .15s;
  }
  button.tab:hover, button.chip:hover { border-color: #c7cdd6; }
  button.tab[aria-pressed="true"] {
    background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
  }
  button.chip[aria-pressed="true"] {
    background: var(--chip); border-color: #b9c6ff; color: #1a3fa8; font-weight: 600;
  }
  input[type="search"] {
    border: 1px solid var(--line); border-radius: 999px; padding: 6px 14px;
    font-size: 13px; min-width: 200px; outline: none; background: var(--panel);
  }
  input[type="search"]:focus { border-color: var(--accent); }
  main { padding: 18px 24px 40px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden;
  }
  .card-head {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 13px 16px 10px; border-bottom: 1px solid var(--line-soft);
  }
  .card-head h2 { margin: 0; font-size: 15px; }
  .card-head .meta { color: var(--muted); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 11.5px; font-weight: 600; color: var(--muted);
    padding: 8px 10px 6px; border-bottom: 1px solid var(--line-soft); white-space: nowrap;
  }
  th.num, td.num { text-align: right; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--line-soft); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  td.rk { color: var(--muted); font-variant-numeric: tabular-nums; width: 26px; }
  td.rk.top { color: #b8860b; font-weight: 700; }
  .proj a { color: var(--accent); text-decoration: none; font-weight: 600; word-break: break-all; }
  .proj a:hover { text-decoration: underline; }
  .lang {
    display: inline-block; margin-left: 6px; font-size: 11px; color: var(--muted);
    border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; vertical-align: 1px;
  }
  .desc { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
  .meter { height: 3px; background: var(--line-soft); border-radius: 2px; margin-top: 6px; }
  .meter i { display: block; height: 100%; background: #b9c6ff; border-radius: 2px; }
  td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .delta.up { color: var(--up); font-weight: 600; }
  .delta.down { color: var(--down); font-weight: 600; }
  .pct { font-weight: 400; font-size: 11.5px; opacity: .85; }
  .est {
    display: inline-block; margin-left: 5px; font-size: 10.5px; color: #8a6d00;
    background: #fff7dd; border: 1px solid #f0dfa8; border-radius: 4px; padding: 0 4px;
    vertical-align: 1px; font-weight: 600;
  }
  .spark { display: block; }
  .none { color: var(--muted); font-size: 12.5px; padding: 14px 16px 18px; }
  .hint {
    background: #fff9e6; border: 1px solid #f2e2b0; color: #7a6200;
    border-radius: 8px; padding: 10px 14px; font-size: 12.5px; margin-bottom: 16px;
  }
  footer {
    border-top: 1px solid var(--line); background: var(--panel);
    padding: 16px 24px 26px; color: var(--muted); font-size: 12px;
  }
  footer p { margin: 4px 0; }
  @media (max-width: 640px) {
    header, .controls, main, footer { padding-left: 14px; padding-right: 14px; }
    .grid { grid-template-columns: 1fr; }
    input[type="search"] { min-width: 130px; flex: 1; }
    .hide-sm { display: none; }
  }

/* ================================================================
   视觉升级 v2 —— 追加式覆盖，不改动上面的基础规则，降低回归风险
   ================================================================ */
:root {
  --brand-1: #4f46e5; --brand-2: #0ea5e9;
  --gold: #b8860b; --silver: #7b8794; --bronze: #a9662f;
  --shadow-sm: 0 1px 2px rgba(16,24,40,.05);
  --shadow-md: 0 4px 14px rgba(16,24,40,.08);
  --shadow-lg: 0 14px 36px rgba(16,24,40,.13);
  --d0: #4f46e5; --d1: #0d9488; --d2: #d97706; --d3: #db2777; --d4: #7c3aed;
  --ease: cubic-bezier(.22,.61,.36,1);
}
html[data-theme="dark"] {
  --bg: #0f1115; --panel: #171a21; --line: #272c36; --line-soft: #20242d;
  --text: #e9ebef; --muted: #98a1b0; --accent: #7aa2ff; --chip: #1c2333;
  --up: #ff6b6b; --down: #3ecf7f;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.45);
  --shadow-md: 0 4px 14px rgba(0,0,0,.5);
  --shadow-lg: 0 14px 36px rgba(0,0,0,.6);
}
@media (prefers-color-scheme: dark) {
  html:not([data-theme="light"]) {
    --bg: #0f1115; --panel: #171a21; --line: #272c36; --line-soft: #20242d;
    --text: #e9ebef; --muted: #98a1b0; --accent: #7aa2ff; --chip: #1c2333;
    --up: #ff6b6b; --down: #3ecf7f;
    --shadow-sm: 0 1px 2px rgba(0,0,0,.45);
    --shadow-md: 0 4px 14px rgba(0,0,0,.5);
    --shadow-lg: 0 14px 36px rgba(0,0,0,.6);
  }
}
body {
  background-image:
    radial-gradient(1200px 380px at 12% -8%, rgba(79,70,229,.10), transparent 60%),
    radial-gradient(900px 320px at 88% -12%, rgba(14,165,233,.10), transparent 60%);
  background-repeat: no-repeat;
}

/* ---- 图标 ---- */
.ico { width: 15px; height: 15px; flex: none; stroke: currentColor; fill: none;
  stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; vertical-align: -2px; }
.ico-lg { width: 22px; height: 22px; stroke-width: 1.7; }
.ico-sm { width: 12px; height: 12px; }

/* ---- 头部 ---- */
header { padding: 22px 24px 18px; border-bottom: none;
  background: linear-gradient(180deg, var(--panel), transparent); }
.brand { display: flex; align-items: center; gap: 12px; }
.brand .logo { width: 38px; height: 38px; border-radius: 11px; flex: none;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(135deg, var(--brand-1), var(--brand-2));
  box-shadow: var(--shadow-md); }
.brand h1 { margin: 0; font-size: 19px; letter-spacing: .2px; }
.brand .tagline { color: var(--muted); font-size: 12px; margin-top: 1px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; align-items: center; }
.stat { display: inline-flex; align-items: center; gap: 6px; color: var(--muted);
  background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
  padding: 5px 11px; font-size: 12px; box-shadow: var(--shadow-sm); }
.stat b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.head-tools { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.icon-btn { width: 32px; height: 32px; border-radius: 9px; border: 1px solid var(--line);
  background: var(--panel); color: var(--muted); cursor: pointer; display: grid;
  place-items: center; transition: .18s var(--ease); padding: 0; }
.icon-btn:hover { color: var(--accent); border-color: var(--accent);
  transform: translateY(-1px); box-shadow: var(--shadow-sm); }

/* ---- 控制条 ---- */
.controls { background: color-mix(in srgb, var(--panel) 88%, transparent);
  box-shadow: var(--shadow-sm); gap: 12px; }
.group { background: var(--panel); border: 1px solid var(--line);
  border-radius: 11px; padding: 4px 8px 4px 10px; }
.group > span.lbl { display: inline-flex; align-items: center; gap: 4px; }
button.tab, button.chip { border: none; background: transparent; transition: .16s var(--ease); }
button.tab:hover, button.chip:hover { background: var(--line-soft); border-color: transparent; }
button.tab[aria-pressed="true"] { background: linear-gradient(135deg, var(--brand-1), var(--brand-2));
  color: #fff; box-shadow: var(--shadow-sm); }
button.chip[aria-pressed="true"] { background: var(--chip); border: 1px solid transparent;
  color: var(--accent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent); }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block;
  margin-right: 5px; flex: none; }
.search-wrap { position: relative; display: flex; align-items: center; }
.search-wrap .ico { position: absolute; left: 11px; color: var(--muted); pointer-events: none; }
input[type="search"] { padding-left: 32px; box-shadow: var(--shadow-sm); }
.kbd { border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 5px;
  padding: 0 5px; font-size: 11px; color: var(--muted); font-family: ui-monospace, monospace; }

/* ---- 卡片 ---- */
.grid { gap: 18px; animation: fadeUp .34s var(--ease) both; }
@keyframes fadeUp { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
.card { border-radius: 14px; box-shadow: var(--shadow-md); position: relative;
  transition: box-shadow .22s var(--ease), transform .22s var(--ease); }
.card::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px;
  background: var(--daccent, var(--brand-1)); border-radius: 14px 0 0 14px; }
.card:hover { box-shadow: var(--shadow-lg); transform: translateY(-2px); }
.card-head { background: linear-gradient(180deg, var(--line-soft), transparent); align-items: center; }
.card-head h2 { display: flex; align-items: center; gap: 7px; font-size: 14.5px; }
.card-head h2 .ico { color: var(--daccent, var(--brand-1)); }
.card-head .meta { display: inline-flex; align-items: center; gap: 5px; }
.badge { display: inline-flex; align-items: center; gap: 4px; border-radius: 6px;
  padding: 1px 7px; font-size: 11px; font-weight: 600; }
.badge-hot { color: #b45309; background: #fef3c7; }
.badge-trend { color: #1d4ed8; background: #dbeafe; }
html[data-theme="dark"] .badge-hot { color: #fbbf24; background: rgba(251,191,36,.14); }
html[data-theme="dark"] .badge-trend { color: #93b4ff; background: rgba(147,180,255,.14); }

/* ---- 榜单行 ---- */
tbody tr { transition: background .15s var(--ease); }
tbody tr:hover { background: color-mix(in srgb, var(--daccent, var(--brand-1)) 5%, transparent); }
td.rk { width: 34px; }
.medal { display: inline-grid; place-items: center; width: 22px; height: 22px;
  border-radius: 50%; font-size: 11.5px; font-weight: 700; color: #fff;
  background: var(--silver); box-shadow: var(--shadow-sm); }
.medal.m1 { background: linear-gradient(135deg, #f2c14e, #b8860b); }
.medal.m2 { background: linear-gradient(135deg, #cbd5e1, #7b8794); }
.medal.m3 { background: linear-gradient(135deg, #e0a877, #a9662f); }
.delta { display: inline-flex; align-items: center; gap: 3px; border-radius: 6px;
  padding: 1px 7px; font-weight: 600; font-size: 12px; }
.delta.up { color: var(--up); background: color-mix(in srgb, var(--up) 11%, transparent); }
.delta.down { color: var(--down); background: color-mix(in srgb, var(--down) 11%, transparent); }
.lang { border-radius: 999px; padding: 0 8px; line-height: 17px;
  background: var(--line-soft); border-color: transparent; }
.meter { height: 4px; border-radius: 999px; overflow: hidden; }
.meter i { background: linear-gradient(90deg, var(--daccent, var(--brand-1)),
  color-mix(in srgb, var(--daccent, var(--brand-1)) 55%, #fff)); }
mark { background: color-mix(in srgb, #facc15 42%, transparent); color: inherit;
  border-radius: 3px; padding: 0 1px; }
.desc { line-height: 1.5; }

/* ---- 悬浮按钮 / 提示 ---- */
#toTop { position: fixed; right: 22px; bottom: 22px; width: 40px; height: 40px;
  border-radius: 50%; border: 1px solid var(--line); background: var(--panel);
  color: var(--text); box-shadow: var(--shadow-lg); cursor: pointer;
  display: grid; place-items: center; opacity: 0; pointer-events: none;
  transition: .22s var(--ease); z-index: 20; padding: 0; }
#toTop.show { opacity: 1; pointer-events: auto; }
#toTop:hover { color: var(--accent); transform: translateY(-2px); }
.toast { position: fixed; left: 50%; bottom: 26px;
  transform: translateX(-50%) translateY(10px);
  background: var(--text); color: var(--panel); padding: 8px 16px; border-radius: 999px;
  font-size: 12.5px; opacity: 0; pointer-events: none;
  transition: .22s var(--ease); z-index: 30; }
.toast.show { opacity: .96; transform: translateX(-50%) translateY(0); }
.hint { display: flex; gap: 9px; align-items: flex-start; box-shadow: var(--shadow-sm);
  border-radius: 11px; }
.hint .ico { margin-top: 2px; }
.none { display: flex; gap: 8px; align-items: center; }
footer { border-top: 1px solid var(--line); }
footer .foot-brand { display: flex; align-items: center; gap: 7px; color: var(--muted); }
@media (max-width: 640px) {
  .stats { gap: 6px; }
  .stat { font-size: 11.5px; padding: 4px 9px; }
  .brand h1 { font-size: 17px; }
  .group { padding: 3px 6px 3px 8px; }
  #toTop { right: 14px; bottom: 14px; }
}
@media print {
  .controls, #toTop, .toast { display: none !important; }
  body { background: #fff; }
  .card { box-shadow: none; border: 1px solid #ddd; break-inside: avoid; }
}
</style>
</head>
<body>
<noscript>
  <style>
    .controls { display: none !important; }
  </style>
  <div style="background:#eef2ff;border-bottom:1px solid #b9c6ff;padding:10px 24px;font-size:12.5px;color:#1a3fa8;">
    当前环境未执行脚本，下面显示的是<b>周榜的静态快照</b>（每个领域：趋势榜 + 热度榜）。
    用浏览器直接打开本文件即可获得完整交互：时间窗切换、榜单切换、领域筛选、关键字搜索。
  </div>
</noscript>
<!-- 内联 SVG 图标库：不依赖任何外部资源，单文件仍然自足 -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-radar" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.6"/><path d="M12 12l6.6-3.8"/><circle cx="12" cy="12" r="1"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.3 2"/></symbol>
<symbol id="i-layers" viewBox="0 0 24 24"><path d="M12 3.2l8.8 4.8-8.8 4.8L3.2 8z"/><path d="M3.2 13.4L12 18.2l8.8-4.8"/></symbol>
<symbol id="i-repo" viewBox="0 0 24 24"><path d="M4.2 5.6A2.6 2.6 0 016.8 3H20v18H6.8a2.6 2.6 0 01-2.6-2.6z"/><path d="M8 3v18"/></symbol>
<symbol id="i-flame" viewBox="0 0 24 24"><path d="M12 3s5.2 4.3 5.2 9.2a5.2 5.2 0 01-10.4 0c0-2.1 1-3.5 2.1-4.6.4 1.2 1.2 1.9 1.9 1.9C11.9 7.8 12 5.5 12 3z"/></symbol>
<symbol id="i-trend" viewBox="0 0 24 24"><path d="M3 17.2l6-6 4 4 7.2-7.2"/><path d="M15.2 8h5v5"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.6"/><path d="M16 16l4.6 4.6"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.4v2.2M12 19.4v2.2M2.4 12h2.2M19.4 12h2.2M5.1 5.1l1.6 1.6M17.3 17.3l1.6 1.6M18.9 5.1l-1.6 1.6M6.7 17.3l-1.6 1.6"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M20.4 14.6A8.6 8.6 0 019.4 3.6a8.6 8.6 0 1011 11z"/></symbol>
<symbol id="i-top" viewBox="0 0 24 24"><path d="M12 19.5V5"/><path d="M6 11l6-6 6 6"/></symbol>
<symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.2M12 7.9h.01"/></symbol>
<symbol id="i-tag" viewBox="0 0 24 24"><path d="M20.6 13.4l-7.2 7.2a2 2 0 01-2.8 0l-7-7A2 2 0 013 12.2V5a2 2 0 012-2h7.2a2 2 0 011.4.6l7 7a2 2 0 010 2.8z"/><path d="M7.6 7.6h.01"/></symbol>
<symbol id="i-keyboard" viewBox="0 0 24 24"><rect x="2.6" y="6" width="18.8" height="12" rx="2.6"/><path d="M6.6 10h.01M10 10h.01M13.4 10h.01M16.8 10h.01M7.4 14h9.2"/></symbol>
<symbol id="i-up" viewBox="0 0 24 24"><path d="M12 6.2l6 7.2h-4v4.4H10v-4.4H6z"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24"><path d="M12 17.8l-6-7.2h4V6.2h4v4.4h4z"/></symbol>
<symbol id="i-empty" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.6"/><path d="M16 16l4.6 4.6M8.6 8.6l4.8 4.8M13.4 8.6l-4.8 4.8"/></symbol>
</defs></svg>

<header>
  <div class="stats" style="margin:0">
    <div class="brand">
      <div class="logo"><svg class="ico ico-lg" viewBox="0 0 24 24"><use href="#i-radar"/></svg></div>
      <div>
        <h1>GitHub AI 开源热点雷达</h1>
        <div class="tagline">周 / 月 / 季 三窗口 · 趋势榜（增速）+ 热度榜（体量）</div>
      </div>
    </div>
    <div class="head-tools">
      <button class="icon-btn" id="themeBtn" title="切换深浅色（快捷键 T）" aria-label="切换主题">
        <svg class="ico" viewBox="0 0 24 24"><use href="#i-moon" id="themeUse"/></svg>
      </button>
    </div>
  </div>
  <div class="stats">
    <span class="stat"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-clock"/></svg>生成 <b>__GENERATED__</b></span>
    <span class="stat"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-layers"/></svg>累计快照 <b>__SNAPDAYS__</b> 天</span>
    <span class="stat"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-repo"/></svg>候选仓库 <b>__REPOS__</b> 个</span>
    <span class="stat" id="stat"></span>
  </div>
</header>

<div class="controls">
  <div class="group" id="periodTabs"><span class="lbl"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-clock"/></svg>时间窗</span></div>
  <div class="group" id="boardTabs"><span class="lbl"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-trend"/></svg>榜单</span></div>
  <div class="group" id="domainChips"><span class="lbl"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-tag"/></svg>领域</span></div>
  <div class="search-wrap">
    <svg class="ico" viewBox="0 0 24 24"><use href="#i-search"/></svg>
    <input type="search" id="kw" placeholder="搜索项目名 / 描述…" autocomplete="off">
  </div>
  <div class="group" title="快捷键：1/2/3 切时间窗，← → 切榜单，/ 聚焦搜索，T 切主题，Esc 清空搜索">
    <span class="lbl"><svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-keyboard"/></svg>
      <span class="kbd">1</span>–<span class="kbd">3</span> 窗口 ·
      <span class="kbd">← →</span> 榜单 ·
      <span class="kbd">/</span> 搜索 ·
      <span class="kbd">T</span> 主题</span>
  </div>
</div>

<main>
  <div id="hint"></div>
  <div class="grid" id="boards">__STATIC_BOARDS__</div>
</main>

<footer>
  <p class="foot-brand">
    <svg class="ico ico-sm" viewBox="0 0 24 24"><use href="#i-info"/></svg>
    <span id="disclaimer"></span>
  </p>
  <p>页面为单文件离线产物：数据已内联，断网可用；无任何外部依赖与网络请求。支持打印或「另存为 PDF」。</p>
</footer>

<button id="toTop" title="回到顶部" aria-label="回到顶部">
  <svg class="ico" viewBox="0 0 24 24"><use href="#i-top"/></svg>
</button>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const DATA = __DATA__;

const state = {
  period: Object.keys(DATA.periods)[0],
  board: "trend",
  domains: new Set(DATA.domains.map(d => d.key)),
  keyword: ""
};

function fmtStars(v) {
  if (v === null || v === undefined) return "-";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(v);
}

function fmtDelta(v) {
  if (v === null || v === undefined) return "-";
  const abs = Math.abs(v);
  const body = abs >= 1e3 ? fmtStars(abs) : String(abs);
  return (v > 0 ? "+" : (v < 0 ? "-" : "")) + body;
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let sparkSeq = 0;
function sparkSVG(series) {
  if (!series || series.length < 3) return '<span class="sub">-</span>';
  const w = 132, h = 30, vals = series.map(p => p[1]);
  const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  const span = (hi - lo) || 1, step = w / (vals.length - 1);
  const pts = vals.map((v, i) =>
    (i * step).toFixed(1) + "," + (h - 3 - (v - lo) / span * (h - 6)).toFixed(1)
  ).join(" ");
  const color = vals[vals.length - 1] >= vals[0] ? "#c62828" : "#2e7d32";
  const gid = "spk" + (++sparkSeq);
  const area = "0," + h + " " + pts + " " + w + "," + h;
  return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
    '" preserveAspectRatio="none" aria-hidden="true"><defs>' +
    '<linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="' + color + '" stop-opacity=".30"/>' +
    '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>' +
    '</linearGradient></defs>' +
    '<polygon fill="url(#' + gid + ')" points="' + area + '"/>' +
    '<polyline fill="none" stroke="' + color + '" stroke-width="1.6" ' +
    'stroke-linejoin="round" stroke-linecap="round" points="' + pts + '"/></svg>';
}

// 关键字命中高亮（先转义再插 mark，避免 XSS 与标签错位）
function hl(text) {
  const safe = esc(text);
  const kw = (state.keyword || "").trim();
  if (!kw) return safe;
  const idx = safe.toLowerCase().indexOf(esc(kw).toLowerCase());
  if (idx < 0) return safe;
  const len = esc(kw).length;
  return safe.slice(0, idx) + "<mark>" + safe.slice(idx, idx + len) + "</mark>" +
    safe.slice(idx + len);
}

// 图标片段
function icon(id, cls) {
  return '<svg class="ico ' + (cls || "") + '" viewBox="0 0 24 24"><use href="#' + id + '"/></svg>';
}

function rankCell(rank) {
  if (rank <= 3) return '<span class="medal m' + rank + '">' + rank + '</span>';
  return rank;
}

function rowHTML(item, rank, max) {
  const delta = item.delta;
  let deltaCell = '<span class="sub">-</span>';
  if (delta !== null && delta !== undefined) {
    const cls = delta > 0 ? "up" : (delta < 0 ? "down" : "");
    const arrow = delta > 0 ? icon("i-up", "ico-sm") : (delta < 0 ? icon("i-down", "ico-sm") : "");
    const pct = item.growth_pct === null || item.growth_pct === undefined
      ? "" : ' <span class="pct">(' + (item.growth_pct > 0 ? "+" : "") +
        item.growth_pct + '%)</span>';
    deltaCell = '<span class="delta ' + cls + '">' + arrow + fmtDelta(delta) +
      '</span>' + pct;
  }
  const est = item.estimated
    ? '<span class="est" title="超大仓库分页受限，回溯值可能偏低">估</span>' : "";
  const barW = max > 0 ? Math.max(3, Math.round(Math.abs(item.value) / max * 100)) : 3;
  const textLine = item.summary || item.description || "";
  const tip = (item.summary && item.description)
    ? ' title="' + esc(item.description) + '"' : "";
  const desc = textLine
    ? '<div class="desc"' + tip + '>' + hl(textLine) + '</div>' : "";
  const lang = item.language
    ? '<span class="lang">' + esc(item.language) + '</span>' : "";
  return '<tr>' +
    '<td class="rk">' + rankCell(rank) + '</td>' +
    '<td class="proj">' +
      '<a href="https://github.com/' + esc(item.full_name) + '" target="_blank" rel="noopener">' +
        hl(item.full_name) + '</a>' + lang + est + desc +
      '<div class="meter"><i style="width:' + barW + '%"></i></div>' +
    '</td>' +
    '<td class="num">' + fmtStars(item.stars) + '</td>' +
    '<td class="num hide-sm">' + deltaCell + '</td>' +
    '<td class="hide-sm">' + sparkSVG(item.series) + '</td>' +
  '</tr>';
}

function boardHTML(domain, items, max, accent) {
  const t = state.board;
  const rows = items.map((it, i) => rowHTML(it, i + 1, max)).join("");
  const body = rows
    ? '<table><thead><tr><th>#</th><th>项目</th><th class="num">' +
      icon("i-repo", "ico-sm") + ' Star</th>' +
      '<th class="num hide-sm">窗口增量</th><th class="hide-sm">走势</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>'
    : '<div class="none">' + icon("i-empty", "ico-sm") +
      '该领域暂无' + (t === "trend" ? "趋势" : "热度") +
      '数据（趋势榜需要至少两天的快照）</div>';
  const badge = t === "trend"
    ? '<span class="badge badge-trend">' + icon("i-trend", "ico-sm") + '趋势</span>'
    : '<span class="badge badge-hot">' + icon("i-flame", "ico-sm") + '热度</span>';
  return '<section class="card" style="--daccent:' + accent + '">' +
    '<div class="card-head">' +
      '<h2>' + icon(t === "trend" ? "i-trend" : "i-flame") + esc(domain.name) + '</h2>' +
      '<div class="meta">' + badge + esc(DATA.periods[state.period].label) +
      ' · ' + items.length + ' 条</div>' +
    '</div>' + body + '</section>';
}

function matches(item) {
  if (!state.keyword) return true;
  const kw = state.keyword.toLowerCase();
  return (item.full_name + " " + (item.description || "") + " " +
    (item.summary || "") + " " + (item.language || ""))
    .toLowerCase().indexOf(kw) >= 0;
}

// 每个领域一个主色，卡片左侧色条 + 卡片名图标都用它
const ACCENTS = ["#4f46e5", "#0d9488", "#d97706", "#db2777", "#7c3aed", "#0284c7"];

function render() {
  const boardData = DATA.boards[state.period] || {};
  let html = "";
  let total = 0;
  let hitDomains = 0;

  DATA.domains.forEach(function (domain, di) {
    if (!state.domains.has(domain.key)) return;
    const bucket = boardData[domain.key] || { hot: [], trend: [] };
    const items = (bucket[state.board] || []).filter(matches);
    total += items.length;
    if (items.length) hitDomains += 1;
    const max = items.reduce((m, it) => Math.max(m, Math.abs(it.value || 0)), 0);
    html += boardHTML(domain, items, max, ACCENTS[di % ACCENTS.length]);
  });

  const boards = document.getElementById("boards");
  boards.innerHTML = html ||
    '<div class="none">' + icon("i-empty") +
    '没有符合条件的结果，试试清空关键字或放宽领域筛选。</div>';
  // 内容重建后重放一次入场动画
  boards.style.animation = "none";
  void boards.offsetWidth;
  boards.style.animation = "";

  const longest = Math.max.apply(null, Object.keys(DATA.periods)
    .map(k => DATA.periods[k].days));
  const cov = DATA.snapshot_days;
  const hintBox = document.getElementById("hint");
  if (cov < longest) {
    hintBox.innerHTML = '<div class="hint">' + icon("i-info", "ico-sm") +
      '<div>当前累计快照 <b>' + cov + '</b> 天，而最长窗口为 <b>' + longest +
      '</b> 天：趋势榜的统计区间尚未覆盖完整窗口，增量会偏小。' +
      '保持每日采集，数值会随快照积累自动变准。</div></div>';
  } else {
    hintBox.innerHTML = "";
  }

  const boardName = state.board === "trend" ? "趋势榜" : "热度榜";
  document.getElementById("stat").innerHTML =
    icon("i-trend", "ico-sm") + '当前视图 <b>' + total + '</b> 条 · ' +
    boardName + ' · ' + DATA.periods[state.period].days + ' 天' +
    (state.keyword ? ' · 命中 <b>' + hitDomains + '</b> 个领域' : '');
}

function setPressed(container, activeBtn) {
  Array.prototype.forEach.call(container.querySelectorAll("button"), function (x) {
    x.setAttribute("aria-pressed", x === activeBtn ? "true" : "false");
  });
}

function initTabs() {
  const pt = document.getElementById("periodTabs");
  Object.keys(DATA.periods).forEach(function (key) {
    const b = document.createElement("button");
    b.className = "tab";
    b.innerHTML = esc(DATA.periods[key].label) + ' <span class="pct">' +
      DATA.periods[key].days + '天</span>';
    b.setAttribute("aria-pressed", key === state.period ? "true" : "false");
    b.onclick = function () {
      state.period = key;
      setPressed(pt, b);
      render();
    };
    pt.appendChild(b);
  });

  const bt = document.getElementById("boardTabs");
  [["hot", "i-flame", "热度榜"], ["trend", "i-trend", "趋势榜"]].forEach(function (p) {
    const b = document.createElement("button");
    b.className = "tab";
    b.innerHTML = icon(p[1], "ico-sm") + p[2];
    b.setAttribute("aria-pressed", p[0] === state.board ? "true" : "false");
    b.onclick = function () {
      state.board = p[0];
      setPressed(bt, b);
      render();
    };
    bt.appendChild(b);
  });

  const dc = document.getElementById("domainChips");
  DATA.domains.forEach(function (d, di) {
    const b = document.createElement("button");
    b.className = "chip";
    b.innerHTML = '<span class="dot" style="background:' +
      ACCENTS[di % ACCENTS.length] + '"></span>' + esc(d.name);
    b.setAttribute("aria-pressed", "true");
    b.onclick = function () {
      if (state.domains.has(d.key)) state.domains.delete(d.key);
      else state.domains.add(d.key);
      if (state.domains.size === 0) state.domains.add(d.key);
      Array.prototype.forEach.call(dc.querySelectorAll("button.chip"), function (x, i) {
        x.setAttribute("aria-pressed",
          state.domains.has(DATA.domains[i].key) ? "true" : "false");
      });
      render();
    };
    dc.appendChild(b);
  });
}

/* ---------------- 轻提示 ---------------- */
let toastTimer = null;
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.classList.remove("show"); }, 1600);
}

/* ---------------- 深浅色主题 ---------------- */
const THEME_KEY = "radar-theme";
function systemDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function isDarkNow() {
  const set = document.documentElement.getAttribute("data-theme");
  return set ? set === "dark" : systemDark();
}
function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  const dark = isDarkNow();
  const use = document.getElementById("themeUse");
  if (use) use.setAttribute("href", dark ? "#i-sun" : "#i-moon");
  const btn = document.getElementById("themeBtn");
  if (btn) btn.title = dark ? "切换到浅色（T）" : "切换到深色（T）";
}
function toggleTheme() {
  const next = isDarkNow() ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  applyTheme(next);
  toast("已切换到" + (next === "dark" ? "深色" : "浅色") + "模式");
}
document.getElementById("themeBtn").onclick = toggleTheme;

/* ---------------- 回到顶部 ---------------- */
const toTopEl = document.getElementById("toTop");
toTopEl.onclick = function () { window.scrollTo({ top: 0, behavior: "smooth" }); };
window.addEventListener("scroll", function () {
  toTopEl.classList.toggle("show", window.scrollY > 420);
}, { passive: true });

/* ---------------- 键盘快捷键 ---------------- */
document.addEventListener("keydown", function (e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea") {
    if (e.key === "Escape") {
      document.getElementById("kw").value = "";
      state.keyword = "";
      render();
      e.target.blur();
    }
    return;
  }
  const keys = Object.keys(DATA.periods);
  if (e.key >= "1" && e.key <= String(keys.length)) {
    const idx = parseInt(e.key, 10) - 1;
    state.period = keys[idx];
    setPressed(document.getElementById("periodTabs"),
      document.getElementById("periodTabs").querySelectorAll("button")[idx]);
    render();
    toast("时间窗：" + DATA.periods[state.period].label);
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    state.board = state.board === "trend" ? "hot" : "trend";
    const btns = document.getElementById("boardTabs").querySelectorAll("button");
    setPressed(document.getElementById("boardTabs"),
      state.board === "hot" ? btns[0] : btns[1]);
    render();
    toast("榜单：" + (state.board === "trend" ? "趋势榜" : "热度榜"));
  } else if (e.key === "/") {
    e.preventDefault();
    document.getElementById("kw").focus();
  } else if (e.key.toLowerCase() === "t") {
    toggleTheme();
  }
});

/* ---------------- 启动 ---------------- */
document.getElementById("disclaimer").textContent = DATA.disclaimer;
let saved = null;
try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
applyTheme(saved || null);
initTabs();

const kwEl = document.getElementById("kw");
let kwTimer = null;
kwEl.oninput = function () {
  clearTimeout(kwTimer);
  kwTimer = setTimeout(function () {
    state.keyword = kwEl.value.trim();
    render();
  }, 150);
};
render();
</script>
</body>
</html>
"""


def render(conn, out_path=None, periods=None, top_n=None, domains=None, verbose=True):
    """生成单文件报告，返回写出的路径。"""
    payload = build_payload(conn, periods=periods, top_n=top_n, domains=domains)

    if verbose:
        for key, board in payload["boards"].items():
            trend_total = sum(len(b["trend"]) for b in board.values())
            hot_total = sum(len(b["hot"]) for b in board.values())
            print("  %s：热度 %d 条 / 趋势 %d 条"
                  % (config.PERIODS[key][1], hot_total, trend_total))

    out = pathlib.Path(out_path or config.REPORT_PATH)
    if not out.is_absolute():
        out = config.ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    text = TEMPLATE
    text = text.replace("__GENERATED__", html.escape(payload["generated_at"]))
    text = text.replace("__SNAPDAYS__", str(payload["snapshot_days"]))
    text = text.replace("__REPOS__", str(payload["repo_count"]))
    # 静态预渲染的兜底内容：脚本不可用时页面依然有实际榜单
    text = text.replace("__STATIC_BOARDS__", static_boards(payload))
    # </script> 若出现在数据里会提前闭合脚本标签，做一次转义保险
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    text = text.replace("__DATA__", data_json)

    out.write_text(text, encoding="utf-8")

    # 同步一份到 docs/：GitHub Pages 直接托管这个目录。
    # 好处是链接固定、内容随每日 workflow 的提交自动更新，不需要手动重新发布。
    # `.nojekyll` 是给 Pages 看的：别拿 Jekyll 处理我们的产物。
    docs = config.ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "index.html").write_text(text, encoding="utf-8")
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    if verbose:
        print("报告已生成：%s（%.1f KB）" % (out, out.stat().st_size / 1024.0))
        print("已同步到 %s（GitHub Pages 托管目录）" % (docs / "index.html"))
    return out

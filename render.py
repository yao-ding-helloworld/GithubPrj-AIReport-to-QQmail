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


def sparkline(series, width=132, height=30):
    """把 [(date, stars)] 画成一条折线；点数 <3 返回空串（规格要求整体隐藏）。"""
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
    return (
        '<svg class="spark" viewBox="0 0 %d %d" width="%d" height="%d" '
        'preserveAspectRatio="none" aria-hidden="true">'
        '<polyline fill="none" stroke="%s" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round" points="%s"/>'
        '</svg>' % (width, height, width, height, color, " ".join(points))
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
        delta_cell = ('<span class="delta %s">%s</span>%s'
                      % (cls, _fmt_delta(delta), pct))
    est = ('<span class="est" title="超大仓库分页受限，回溯值可能偏低">估</span>'
           if item.get("estimated") else "")
    bar = 3
    if max_value > 0:
        bar = max(3, int(round(abs(item.get("value") or 0) * 100.0 / max_value)))
    desc = ('<div class="desc">%s</div>' % _esc(item.get("description"))
            if item.get("description") else "")
    lang = ('<span class="lang">%s</span>' % _esc(item.get("language"))
            if item.get("language") else "")
    name = item.get("full_name") or ""
    return (
        '<tr>'
        '<td class="rk%s">%d</td>'
        '<td class="proj">'
        '<a href="https://github.com/%s" target="_blank" rel="noopener">%s</a>%s%s%s'
        '<div class="meter"><i style="width:%d%%"></i></div>'
        '</td>'
        '<td class="num">%s</td>'
        '<td class="num hide-sm">%s</td>'
        '<td class="hide-sm">%s</td>'
        '</tr>'
        % (" top" if rank <= 3 else "", rank, _esc(name), _esc(name), lang, est, desc,
           bar, fmt_stars(item.get("stars")), delta_cell, sparkline(item.get("series")))
    )


def _card_html(title, meta, items, board_type):
    if items:
        rows = "".join(_row_html(it, i, max(abs(x.get("value") or 0) for x in items))
                       for i, it in enumerate(items, 1))
        body = (
            '<table><thead><tr><th>#</th><th>项目</th><th class="num">Star</th>'
            '<th class="num hide-sm">窗口增量</th><th class="hide-sm">走势</th></tr>'
            '</thead><tbody>%s</tbody></table>' % rows
        )
    else:
        body = ('<div class="none">该领域暂无%s数据（趋势榜需要至少两天的快照）</div>'
                % ("趋势" if board_type == "trend" else "热度"))
    return ('<section class="card"><div class="card-head"><h2>%s</h2>'
            '<div class="meta">%s</div></div>%s</section>'
            % (_esc(title), _esc(meta), body))


def static_boards(payload, period_key=None):
    """把默认视图编译成静态 HTML（脚本不可用时的兜底内容）。"""
    periods = list(payload["periods"])
    period_key = period_key or periods[0]
    label = payload["periods"][period_key]["label"]
    boards = payload["boards"].get(period_key, {})
    parts = []
    for domain in payload["domains"]:
        board = boards.get(domain["key"]) or {"hot": [], "trend": []}
        for board_type, suffix in (("trend", "趋势榜"), ("hot", "热度榜")):
            items = board.get(board_type) or []
            parts.append(_card_html(
                "%s · %s" % (domain["name"], suffix),
                "%s · %d 条" % (label, len(items)), items, board_type))
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
<header>
  <h1>GitHub AI 开源热点雷达</h1>
  <div class="sub">
    生成时间 <b>__GENERATED__</b> · 累计快照 <b>__SNAPDAYS__</b> 天 · 候选仓库 <b>__REPOS__</b> 个 ·
    数据源 GitHub 官方 API
  </div>
</header>

<div class="controls">
  <div class="group" id="periodTabs"><span class="lbl">时间窗</span></div>
  <div class="group" id="boardTabs"><span class="lbl">榜单</span></div>
  <div class="group" id="domainChips"><span class="lbl">领域</span></div>
  <div class="group">
    <input type="search" id="kw" placeholder="搜索项目名 / 描述…" autocomplete="off">
  </div>
</div>

<main>
  <div id="hint"></div>
  <div class="grid" id="boards">__STATIC_BOARDS__</div>
</main>

<footer>
  <p id="disclaimer"></p>
  <p>页面为单文件离线产物：数据已内联，断网可用；无任何外部依赖与网络请求。</p>
</footer>

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

function sparkSVG(series) {
  if (!series || series.length < 3) return '<span class="sub">-</span>';
  const w = 132, h = 30, vals = series.map(p => p[1]);
  const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  const span = (hi - lo) || 1, step = w / (vals.length - 1);
  const pts = vals.map((v, i) =>
    (i * step).toFixed(1) + "," + (h - 3 - (v - lo) / span * (h - 6)).toFixed(1)
  ).join(" ");
  const color = vals[vals.length - 1] >= vals[0] ? "#c62828" : "#2e7d32";
  return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
    '" preserveAspectRatio="none"><polyline fill="none" stroke="' + color +
    '" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" points="' + pts +
    '"/></svg>';
}

function rowHTML(item, rank, max) {
  const delta = item.delta;
  let deltaCell = '<span class="sub">-</span>';
  if (delta !== null && delta !== undefined) {
    const cls = delta > 0 ? "up" : (delta < 0 ? "down" : "");
    const pct = item.growth_pct === null || item.growth_pct === undefined
      ? "" : ' <span class="pct">(' + (item.growth_pct > 0 ? "+" : "") + item.growth_pct + '%)</span>';
    deltaCell = '<span class="delta ' + cls + '">' + fmtDelta(delta) + '</span>' + pct;
  }
  const est = item.estimated ? '<span class="est" title="超大仓库分页受限，回溯值可能偏低">估</span>' : "";
  const barW = max > 0 ? Math.max(3, Math.round(Math.abs(item.value) / max * 100)) : 3;
  const desc = item.description ? '<div class="desc">' + esc(item.description) + '</div>' : "";
  const lang = item.language ? '<span class="lang">' + esc(item.language) + '</span>' : "";
  return '<tr>' +
    '<td class="rk' + (rank <= 3 ? " top" : "") + '">' + rank + '</td>' +
    '<td class="proj">' +
      '<a href="https://github.com/' + esc(item.full_name) + '" target="_blank" rel="noopener">' +
        esc(item.full_name) + '</a>' + lang + est + desc +
      '<div class="meter"><i style="width:' + barW + '%"></i></div>' +
    '</td>' +
    '<td class="num">' + fmtStars(item.stars) + '</td>' +
    '<td class="num hide-sm">' + deltaCell + '</td>' +
    '<td class="hide-sm">' + sparkSVG(item.series) + '</td>' +
  '</tr>';
}

function boardHTML(domain, items, max) {
  const name = domain.name;
  const rows = items.map((it, i) => rowHTML(it, i + 1, max)).join("");
  const body = rows
    ? '<table><thead><tr><th>#</th><th>项目</th><th class="num">Star</th>' +
      '<th class="num hide-sm">窗口增量</th><th class="hide-sm">走势</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>'
    : '<div class="none">该领域暂无' + (state.board === "trend" ? "趋势" : "热度") +
      '数据（趋势榜需要至少两天的快照）</div>';
  return '<section class="card"><div class="card-head"><h2>' + esc(name) +
    '</h2><div class="meta">' + esc(DATA.periods[state.period].label) + ' · ' +
    items.length + ' 条</div></div>' + body + '</section>';
}

function matches(item) {
  if (!state.keyword) return true;
  const kw = state.keyword.toLowerCase();
  return (item.full_name + " " + (item.description || "")).toLowerCase().indexOf(kw) >= 0;
}

function render() {
  const boardData = DATA.boards[state.period] || {};
  const kw = state.keyword.toLowerCase();
  let html = "";
  let total = 0;

  DATA.domains.forEach(function (domain) {
    if (!state.domains.has(domain.key)) return;
    const bucket = boardData[domain.key] || { hot: [], trend: [] };
    let items = (bucket[state.board] || []).filter(matches);
    total += items.length;
    const max = items.reduce((m, it) => Math.max(m, Math.abs(it.value || 0)), 0);
    html += boardHTML(domain, items, max);
  });

  document.getElementById("boards").innerHTML = html ||
    '<div class="none">没有符合条件的结果，试试清空关键字或放宽领域筛选。</div>';

  const longest = Math.max.apply(null, Object.keys(DATA.periods)
    .map(k => DATA.periods[k].days));
  const cov = DATA.snapshot_days;
  const hintBox = document.getElementById("hint");
  if (cov < longest) {
    hintBox.innerHTML = '<div class="hint">当前累计快照 <b>' + cov +
      '</b> 天，而最长窗口为 <b>' + longest +
      '</b> 天：趋势榜的统计区间尚未覆盖完整窗口，增量会偏小。' +
      '保持每日采集，数值会随快照积累自动变准。</div>';
  } else {
    hintBox.innerHTML = "";
  }

  document.getElementById("stat").textContent =
    "当前视图 " + total + " 条 · 窗口 " + DATA.periods[state.period].days + " 天";
}

function initTabs() {
  const pt = document.getElementById("periodTabs");
  Object.keys(DATA.periods).forEach(function (key) {
    const b = document.createElement("button");
    b.className = "tab";
    b.textContent = DATA.periods[key].label + "（" + DATA.periods[key].days + "天）";
    b.setAttribute("aria-pressed", key === state.period ? "true" : "false");
    b.onclick = function () {
      state.period = key;
      Array.prototype.forEach.call(pt.querySelectorAll("button.tab"), function (x) {
        x.setAttribute("aria-pressed", x === b ? "true" : "false");
      });
      render();
    };
    pt.appendChild(b);
  });

  const bt = document.getElementById("boardTabs");
  [["hot", "热度榜"], ["trend", "趋势榜"]].forEach(function (pair) {
    const b = document.createElement("button");
    b.className = "tab";
    b.textContent = pair[1];
    b.setAttribute("aria-pressed", pair[0] === state.board ? "true" : "false");
    b.onclick = function () {
      state.board = pair[0];
      Array.prototype.forEach.call(bt.querySelectorAll("button.tab"), function (x) {
        x.setAttribute("aria-pressed", x === b ? "true" : "false");
      });
      render();
    };
    bt.appendChild(b);
  });

  const dc = document.getElementById("domainChips");
  DATA.domains.forEach(function (d) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = d.name;
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

  const kw = document.getElementById("kw");
  let timer = null;
  kw.oninput = function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      state.keyword = kw.value.trim();
      render();
    }, 150);
  };
}

document.getElementById("disclaimer").textContent = DATA.disclaimer;
const statEl = document.createElement("div");
statEl.id = "stat";
statEl.className = "sub";
statEl.style.marginBottom = "10px";
document.querySelector("main").insertBefore(statEl, document.getElementById("hint"));
initTabs();
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
    if verbose:
        print("报告已生成：%s（%.1f KB）" % (out, out.stat().st_size / 1024.0))
    return out

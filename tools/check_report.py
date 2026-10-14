# -*- coding: utf-8 -*-
"""报告自检：内联数据是否完整、脚本是否有语法错误。

HTML 报告是纯前端渲染的，一旦脚本抛异常，页面就只剩"空架子"（静态结构在、
榜单区空白），而且浏览器不会把错误写到文件里，很难查。所以：

1. 把 <script> 里的 `const DATA = {...}` 抠出来用 json 解析 —— 验证数据完整
2. 把整段脚本交给 node --check —— 验证语法（node 在本机可用）
3. 统计各窗口/各领域的榜单条数，暴露"数据在但渲染不出"的情况

跑法：python tools/check_report.py [报告路径]
"""

import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def extract_script(html):
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    return match.group(1) if match else None


def extract_data(script):
    """抠出 `const DATA = <json>;` —— 从等号后第一个 { 到脚本里第一个 `;\\n`。"""
    start = script.find("const DATA =")
    if start < 0:
        return None
    start = script.index("{", start)
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(script)):
        char = script[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start:idx + 1]
    return None


def main(argv):
    path = pathlib.Path(argv[1]) if len(argv) > 1 else (config.ROOT / config.REPORT_PATH)
    if not path.is_absolute():
        path = config.ROOT / path

    say("== 报告自检 ==")
    say("文件：%s" % path)
    if not path.exists():
        say("[FAIL] 文件不存在")
        return 1
    say("大小：%.1f KB" % (path.stat().st_size / 1024.0))
    say("")

    html = path.read_text(encoding="utf-8")
    script = extract_script(html)
    if script is None:
        say("[FAIL] 找不到 <script> 块")
        return 1
    say("[PASS] 找到 <script> 块（%d 字符）" % len(script))
    say("")

    # ---- 1. 内联数据 ----
    raw = extract_data(script)
    if raw is None:
        say("[FAIL] 抠不出 const DATA")
        return 1
    say("DATA 原始长度：%d 字符" % len(raw))
    try:
        data = json.loads(raw)
        say("[PASS] JSON 解析成功")
    except Exception as exc:                                # noqa: BLE001
        say("[FAIL] JSON 解析失败：%s" % exc)
        say("       前 200 字符：%s" % raw[:200])
        return 1
    say("")

    say("顶层键：%s" % ", ".join(data.keys()))
    say("生成时间：%s | 累计快照：%s 天 | 候选仓库：%s"
        % (data.get("generated_at"), data.get("snapshot_days"), data.get("repo_count")))
    say("领域：%s" % ", ".join(d["name"] for d in data.get("domains", [])))
    say("")

    say("--- 各窗口榜单条数 ---")
    boards = data.get("boards") or {}
    total = 0
    for period, domains in boards.items():
        hot = sum(len(b.get("hot", [])) for b in domains.values())
        trend = sum(len(b.get("trend", [])) for b in domains.values())
        total += hot + trend
        say("  %-10s 热度 %3d 条 / 趋势 %3d 条" % (period, hot, trend))
    say("  合计 %d 条" % total)
    say("")

    if total == 0:
        say("[FAIL] 榜单全空 —— 页面自然只剩空架子。检查 compute/db 是否有数据。")
    else:
        sample = None
        for domains in boards.values():
            for board in domains.values():
                if board.get("hot"):
                    sample = board["hot"][0]
                    break
            if sample:
                break
        if sample:
            say("样例条目：%s ★%s（字段：%s）"
                % (sample.get("full_name"), sample.get("stars"),
                   ", ".join(sorted(sample.keys()))))
    say("")

    # ---- 2. 脚本语法 ----
    js_path = config.ROOT / ".data" / "_report_script.js"
    js_path.write_text(script, encoding="utf-8")
    node = None
    for candidate in (
        pathlib.Path(r"C:\Users\dingyao1\.workbuddy\binaries\node\versions\22.12.0\node.exe"),
        pathlib.Path(r"C:\Users\dingyao1\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"),
        pathlib.Path(r"D:\Nodejs\node.exe"),
    ):
        if candidate.exists():
            node = candidate
            break
    say("--- 脚本语法检查 ---")
    if not node:
        say("[i] 没找到 node，跳过语法检查")
        return 0
    result = subprocess.run([str(node), "--check", str(js_path)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        say("[PASS] node --check 通过，无语法错误")
    else:
        say("[FAIL] 语法错误：")
        say(result.stderr.strip()[:2000])
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    code = main(sys.argv)
    (config.ROOT / ".data" / "checkreport.txt").write_text("\n".join(LINES),
                                                           encoding="utf-8")
    print("\n".join(LINES))
    sys.exit(code)

# -*- coding: utf-8 -*-
"""邮件结构自检：把将要发出的 MIME 结构完整打印出来（不发送）。

为什么要这个：
邮件在手机上"看不到内容"通常不是内容缺失，而是**结构/兼容性**问题——
比如附件被当成正文、HTML 分支过于简陋、charset 缺失、或正文其实只有一小段纯文本。
把 MIME 树打出来一看就知道。

跑法：python tools/inspect_mail.py [--out 预览.html]
"""

import json
import pathlib
import sys
from email.message import EmailMessage

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import db          # noqa: E402
import digest      # noqa: E402
import env         # noqa: E402
import notify      # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def walk(msg, depth=0):
    pad = "  " * depth
    ctype = msg.get_content_type()
    disp = msg.get_content_disposition() or "-"
    payload = msg.get_payload(decode=True)
    size = len(payload) if payload else 0
    extra = ""
    if msg.get_filename():
        extra += "  filename=%s" % msg.get_filename()
    if msg.get_content_charset():
        extra += "  charset=%s" % msg.get_content_charset()
    say("%s- %-24s disp=%-10s %7d B%s" % (pad, ctype, disp, size, extra))
    if msg.is_multipart():
        for part in msg.get_payload():
            walk(part, depth + 1)


def main(argv):
    env.load_env()
    conn = db.init()
    data = digest.collect_digest(conn, period_key="weekly", top_n=config.MAIL_TOP_N)
    markdown = digest.to_markdown(data)
    html_body = digest.to_email_html(data)
    report = config.ROOT / config.REPORT_PATH

    say("== 邮件结构自检 ==")
    say("")

    settings = env.smtp_settings()
    say("SMTP 配置：%s" % ("完整" if settings else "缺失"))
    say("收件人：%s" % (", ".join(settings["to"]) if settings else "-"))
    say("")

    provider = notify.EmailProvider(settings) if settings else None
    if provider:
        # 关键：这里必须传 html_body，否则走的是 markdown 兜底转换，看不出版式
        msg, note = provider.build_message(
            "[AI 开源雷达] 结构自检", markdown, html_body=html_body,
            html_path=str(report) if report.exists() else None)
        say("附件说明：%s" % note)
        say("")
        say("--- 当前 MIME 结构 ---")
        walk(msg)
        say("")
        raw = msg.as_bytes()
        say("整体大小：%.1f KB" % (len(raw) / 1024.0))
        say("")

    say("--- 纯文本分支：%d 字符 ---" % len(markdown))
    for line in markdown.splitlines()[:14]:
        say("  | " + line)
    say("")
    say("--- HTML 分支：%d 字符 ---" % len(html_body))

    # 落两份预览，方便直接用浏览器看效果（模拟手机宽度）
    (config.ROOT / ".data" / "mail_body_preview.html").write_text(html_body,
                                                                  encoding="utf-8")
    say("HTML 分支已另存：.data/mail_body_preview.html")
    say("（在浏览器里把窗口拖窄，就是手机上的实际观感）")
    return 0


if __name__ == "__main__":
    code = main(sys.argv)
    (config.ROOT / ".data" / "inspect_mail.txt").write_text("\n".join(LINES),
                                                            encoding="utf-8")
    print("\n".join(LINES))
    sys.exit(code)

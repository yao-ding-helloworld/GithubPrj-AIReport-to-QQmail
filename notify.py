# -*- coding: utf-8 -*-
"""推送层：邮件单通道（阶段 7.1 / 7.7）。

对应 specs/scheduled-delivery/spec.md 的「邮件推送（SMTP 单通道）」需求与 ADR-8：
- 只用标准库 smtplib + email.message，零第三方依赖
- 正文 = 纯文本 + 简易 HTML 双份；附件 = 完整 report.html
- 不把报告内联进正文（邮件客户端会剥离内联 JS，交互会失效）
- SMTP 配置缺失 → 自动降级为控制台输出，流程不中断
- 任何失败都不影响整体运行成功（报告与快照已落盘）

provider 接口统一为 send(subject, markdown, html_path=None)。
v1 只落地 EmailProvider / ConsoleProvider；若以后要加 IM 通道，只增实现、不改调用方。
"""

import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import config
import env

MAX_ATTACH_BYTES = 8 * 1024 * 1024      # 超过 8MB 的附件不硬塞，多数邮箱会拒收


class NotifyResult(object):
    def __init__(self, ok, channel, detail=""):
        self.ok = ok
        self.channel = channel
        self.detail = detail

    def __repr__(self):
        return "NotifyResult(%s, %s, %s)" % (self.ok, self.channel, self.detail)


# ---------------------------------------------------------------- provider
class ConsoleProvider(object):
    """兜底通道：把摘要打到标准输出，不消耗任何额度。"""

    name = "console"

    def available(self):
        return True

    def send(self, subject, markdown, html_body=None, html_path=None):
        print("-" * 62)
        print("[控制台推送] 主题：%s" % subject)
        print(markdown)
        if html_body:
            print("[控制台推送] HTML 分支：%d 字符（未打印）" % len(html_body))
        if html_path and Path(html_path).exists():
            print("[控制台推送] 附件（未发送）：%s" % html_path)
        print("-" * 62)
        return NotifyResult(True, self.name, "已打印到标准输出")


class EmailProvider(object):
    """SMTP 邮件通道。"""

    name = "email"

    def __init__(self, settings=None):
        self.settings = settings or env.smtp_settings()

    def available(self):
        return bool(self.settings and self.settings.get("to"))

    def build_message(self, subject, markdown, html_body=None, html_path=None):
        import digest                      # 局部导入，避免与 run.py 形成环

        settings = self.settings
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings["user"]
        msg["To"] = ", ".join(settings["to"])
        msg["Date"] = formatdate(localtime=True)

        msg.set_content(markdown)                                   # 纯文本分支
        # HTML 分支：正文优先用调用方给的移动版 HTML；没给则退回 markdown 转换
        msg.add_alternative(html_body or digest.md_to_html(markdown),
                            subtype="html")

        note = []
        if html_path:
            path = Path(html_path)
            if path.exists() and path.stat().st_size <= MAX_ATTACH_BYTES:
                payload = path.read_bytes()
                msg.add_attachment(
                    payload, maintype="text", subtype="html",
                    filename=path.name,
                )
                note.append("附件 %s（%.1f KB）" % (path.name, len(payload) / 1024.0))
            elif path.exists():
                note.append("报告超 %.0fMB，跳过附件"
                            % (MAX_ATTACH_BYTES / 1024.0 / 1024.0))
        if not note:
            note.append("无附件")
        return msg, "；".join(note)

    def send(self, subject, markdown, html_body=None, html_path=None, retries=2):
        if not self.available():
            return NotifyResult(False, self.name, "未配置 SMTP")

        msg, note = self.build_message(subject, markdown, html_body=html_body,
                                       html_path=html_path)
        settings = self.settings
        attempt = 0
        last_error = ""

        while attempt <= retries:
            attempt += 1
            try:
                if settings["use_ssl"]:
                    context = ssl.create_default_context()
                    with smtplib.SMTP_SSL(settings["host"], settings["port"],
                                          timeout=30, context=context) as server:
                        server.login(settings["user"], settings["password"])
                        server.send_message(msg)
                else:
                    with smtplib.SMTP(settings["host"], settings["port"],
                                      timeout=30) as server:
                        server.starttls(context=ssl.create_default_context())
                        server.login(settings["user"], settings["password"])
                        server.send_message(msg)
                return NotifyResult(True, self.name,
                                    "已发送至 %s（%s）"
                                    % (", ".join(settings["to"]), note))

            except smtplib.SMTPAuthenticationError:
                return NotifyResult(
                    False, self.name,
                    "SMTP 认证失败：QQ 邮箱需使用 16 位授权码（在邮箱设置中开启 "
                    "IMAP/SMTP 服务后生成），不是登录密码",
                )
            except Exception as exc:                            # noqa: BLE001
                last_error = "%s: %s" % (type(exc).__name__, exc)
                if attempt <= retries:
                    time.sleep(3.0 * (2 ** (attempt - 1)))      # 3s / 6s 退避
        return NotifyResult(False, self.name, "重试 %d 次仍失败：%s" % (retries, last_error))


# ---------------------------------------------------------------- 统一入口
def choose_provider():
    email = EmailProvider()
    return email if email.available() else ConsoleProvider()


def send_digest(markdown, subject=None, html_body=None, html_path=None, verbose=True):
    """把摘要推送出去。返回 NotifyResult；失败不抛异常。"""
    import digest

    subject = subject or digest.subject()
    provider = choose_provider()

    if verbose:
        print("推送通道：%s" % provider.name)

    try:
        result = provider.send(subject, markdown, html_body=html_body,
                               html_path=html_path)
    except Exception as exc:                                    # noqa: BLE001
        result = NotifyResult(False, getattr(provider, "name", "unknown"),
                              "未捕获异常：%s" % exc)

    if verbose:
        if result.ok:
            print("推送成功：%s" % result.detail)
        else:
            print("推送失败（不影响本次运行）：%s" % result.detail)
            if result.channel == "console" or "SMTP" in result.detail:
                print(env.describe_smtp())
    return result


if __name__ == "__main__":
    import db
    import digest

    conn = db.init()
    text = digest.render_digest(conn)
    send_digest(text)

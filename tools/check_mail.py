# -*- coding: utf-8 -*-
"""SMTP 连通性自检：填完 .env 后先跑这个，不要直接等云端报错。

跑法：python tools/check_mail.py
它会依次检查：变量是否齐全 → 能否连上 SMTP → 能否登录 → 能否发出测试邮件，
并把每一层的失败原因翻成人话（授权码错、端口不通、被风控拦截等）。

输出同时落一份到 .data/check_mail.txt。
"""

import pathlib
import smtplib
import ssl
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import env         # noqa: E402
import notify      # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def flush():
    say("")
    say("== 自检结束 ==")
    out = config.ROOT / ".data" / "check_mail.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LINES), encoding="utf-8")
    try:
        print("\n".join(LINES))
    except Exception:                                       # noqa: BLE001
        pass


def main():
    env.load_env()
    settings = env.smtp_settings()

    say("== SMTP 连通性自检 ==")
    say(".env 路径：%s" % env.ENV_PATH)
    say("")

    missing = env.missing(env.SMTP_REQUIRED)
    for name in env.SMTP_REQUIRED:
        value = env.get(name)
        show = env.mask(value) if name in ("SMTP_USER", "SMTP_PASS") else (value or "<空>")
        say("  %-10s %s%s" % (name, show,
                              "   ← 缺失" if name in missing else ""))
    say("")

    if missing:
        say("[FAIL] 变量不全，缺少：%s" % ", ".join(missing))
        say("")
        say(env.describe_smtp())
        flush()
        return 1

    host, port = settings["host"], settings["port"]
    say("[1/3] 变量齐全，开始连接 %s:%s（%s）"
        % (host, port, "SSL 直连" if settings["use_ssl"] else "STARTTLS"))

    try:
        if settings["use_ssl"]:
            server = smtplib.SMTP_SSL(host, port, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=ssl.create_default_context())
    except Exception as exc:                                # noqa: BLE001
        say("[FAIL] 连不上 SMTP：%s: %s" % (type(exc).__name__, exc))
        say("")
        say("排查方向：")
        say("  1. 网络是否放行出站 465/587（公司网络常拦非 443 端口）")
        say("  2. SMTP_HOST 是否写成了 smtp.qq.com（不是 mail.qq.com）")
        say("  3. 换成 SMTP_PORT=587（STARTTLS）再试")
        flush()
        return 1

    say("[PASS] TCP + TLS 已连通")
    say("[2/3] 尝试登录 %s" % env.mask(settings["user"]))

    try:
        server.login(settings["user"], settings["password"])
    except smtplib.SMTPAuthenticationError as exc:
        say("[FAIL] 登录被拒（535）：%s" % exc)
        say("")
        say("最可能的原因：SMTP_PASS 填的是登录密码，而不是 16 位授权码。")
        say("  获取方式：mail.qq.com → 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码")
        say("  其次确认 SMTP_USER 是完整邮箱地址（如 12345678@qq.com）")
        say("  另外：改过 QQ 密码后，旧授权码会失效，需要重新生成")
        try:
            server.quit()
        except Exception:                                   # noqa: BLE001
            pass
        flush()
        return 1
    except Exception as exc:                                # noqa: BLE001
        say("[FAIL] 登录异常：%s: %s" % (type(exc).__name__, exc))
        try:
            server.quit()
        except Exception:                                   # noqa: BLE001
            pass
        flush()
        return 1

    say("[PASS] 登录成功")
    say("[3/3] 发送测试邮件到 %s" % ", ".join(settings["to"]))

    provider = notify.EmailProvider(settings)
    # 附件用真实报告（存在就带上，顺便验证附件通路）
    report = config.ROOT / config.REPORT_PATH
    result = provider.send(
        "[AI 开源雷达] SMTP 连通性自检",
        "# 连通性自检\n\n如果你在收件箱看到这封邮件，说明 SMTP 链路已通。\n\n"
        "- 发件账号：%s\n- 收件地址：%s\n- 服务器：%s:%s\n"
        % (settings["user"], ", ".join(settings["to"]), host, port),
        html_path=str(report) if report.exists() else None,
    )

    if result.ok:
        say("[PASS] %s" % result.detail)
        say("")
        say("接下来：git remote add origin <你的仓库URL> && git push -u origin main")
        say("推送后到仓库 Actions 页面手动触发一次 workflow 即可。")
        flush()
        return 0

    say("[FAIL] %s" % result.detail)
    say("")
    say("若返回 550 / 554，通常是邮箱侧风控：新开启 SMTP 的账号首次发信可能被限，")
    say("去 QQ 邮箱「已发送」确认是否已发出；或稍后重试。")
    try:
        server.quit()
    except Exception:                                       # noqa: BLE001
        pass
    flush()
    return 1


if __name__ == "__main__":
    sys.exit(main())

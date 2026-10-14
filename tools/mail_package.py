# -*- coding: utf-8 -*-
"""把整个项目打成 zip 并发到指定邮箱（便于在其他设备上继续开发）。

跑法：python tools/mail_package.py
包含：全部源码、openspec 规格与 ADR、.git 完整历史、快照 CSV、已渲染的报告
排除：.env（含密钥）、*.db（可由 CSV 重建）、各类临时/自检输出

安全性：.env 永不入包；打包后会用 zipfile 校验 CRC，确保附件可用。
"""

import pathlib
import smtplib
import ssl
import subprocess
import sys
import zipfile
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import digest      # noqa: E402
import env         # noqa: E402

LINES = []
MAX_ATTACH_BYTES = 25 * 1024 * 1024      # 超过就不带 .git，保证能发出去


def say(text=""):
    LINES.append(str(text))


def flush(code=0):
    out = config.ROOT / ".data" / "mailpackage.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LINES), encoding="utf-8")
    try:
        print("\n".join(LINES))
    except Exception:                                       # noqa: BLE001
        pass
    return code


# ---------------------------------------------------------------- 打包
def is_excluded(rel_path, name, drop_git):
    """判断相对路径是否该排除。rel_path 用 / 分隔。"""
    parts = rel_path.split("/")
    if drop_git and parts[0] == ".git":
        return True
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    if rel_path == ".env":                      # 密钥绝不入包
        return True
    if name.endswith((".pyc", ".pyo")):
        return True
    if name.endswith(".db") or "-journal" in name or name.endswith("-wal"):
        return True
    if name.endswith(".zip"):
        return True
    # .data 下只留三份 CSV
    if parts[0] == ".data":
        return name not in ("repos.csv", "snapshots.csv", "windows.csv")
    # 根目录的临时/诊断文件
    if len(parts) == 1 and (
        name in ("sz.txt", "ls_tmp.txt", "smoke_out.txt", "net.txt", "out.txt")
        or name.startswith(("out_", "remote", "gitlog"))
    ):
        return True
    return False


def commit_tag():
    """用当前提交短 sha 当版本标签，便于区分多次打包。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(config.ROOT), capture_output=True, text=True,
                             timeout=20)
        return out.stdout.strip() or "nogit"
    except Exception:                                       # noqa: BLE001
        return "nogit"


def build_zip(drop_git=False, tag=None):
    stamp = date.today().isoformat()
    tag = tag or commit_tag()
    out = config.ROOT / "dist" / ("ai-trending-radar-v1_%s_%s%s.zip"
                                  % (stamp, tag, "_nogit" if drop_git else ""))
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(config.ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(config.ROOT).as_posix()
            if is_excluded(rel, path.name, drop_git):
                continue
            # 顶层套一层目录，解压后不会散落一地
            zf.write(path, arcname="ai-trending-radar-v1/" + rel)
            count += 1
    return out, count


# ---------------------------------------------------------------- 邮件正文
def body_markdown(zip_name, file_count, size_mb, has_git, tag="nogit"):
    return """# AI 开源雷达 · 完整项目代码包

附件 `{zip}`（版本 `{tag}`，{count} 个文件，{size:.1f} MB{gitnote}），解压后即可在其他设备上继续开发。

## 本版更新（相对上一版）

| 变更 | 说明 |
|---|---|
| **修复报告空架子** | 页面改为「静态预渲染 + JS 增强」。此前整页靠 JS 渲染，渲染环境一禁用内联脚本就只剩骨架；现在构建时就把周榜的**趋势榜 + 热度榜**编译进 HTML（内嵌 60 条真实榜单），脚本可用时再接管交互。 |
| **新增 `gh_actions.py`** | 走 API 触发 / 跟踪云端 workflow（本机 `git push` 不通，但 api.github.com 稳定）。`list` / `trigger` / `status` / `watch` |
| **新增 `check_report.py`** | 报告体检：抠出内联 JSON 校验 + 交给 `node --check` 查语法，专治"页面空白但文件看着没问题" |
| **新增 `git_fill.py`** | 补齐本地缺失的提交对象（API 推送产生的提交本机没有，否则 `git log` 报 `Failed to traverse parents`） |
| **新增 `mail_package.py`** | 就是发出本邮件的工具 |
| **修正 `github_push.py`** | ① 原来用 `git log --pretty=raw` 取提交信息，该格式会给消息体每行**加 4 空格缩进**，污染远端提交信息 → 改用 `git cat-file commit`；② 原来推送后把本地分支指向 API 创建的提交，会让 git 报 `bad tree object HEAD` → 现在只记录远端 sha，本地保持独立提交线；③ **默认只推代码**，不覆盖云端维护的快照 CSV |
| **修正 `run.py`** | `--notify` 调用 `digest` 时参数名笔误（`periods=` 应为 `period_key=`），此前一跑必崩 |

## 云端已验证跑通

GitHub Actions 首次运行**全绿**（271 秒）：重建快照库 → 采集 → 分类 → 指标 → 报告 → 发信 → 快照提交回仓库 → 上传 artifact。
之后每日北京时间 9:00 自动运行，无需人工干预。

## 包里有什么

| 内容 | 包含 | 说明 |
|---|---|---|
| 全部源码 | ✅ | 12 个模块 + 9 个工具脚本 + GitHub Actions 工作流 |
| openspec 设计文档 | ✅ | proposal / design（10 条 ADR）/ 5 份行为规格 / 任务清单 |
| `.git` 提交历史 | {gitflag} | 已配好 origin 远程 |
| `.data/*.csv` 采集快照 | ✅ | 931 行仓库快照 + 2676 行窗口增量，接着跑就有趋势榜 |
| `dist/report.html` | ✅ | 当前渲染出的交互报告（含静态兜底） |
| `.env` | ❌ | **故意排除**——里面有 Token 和邮箱授权码，需在新设备自行重建 |
| `*.db` | ❌ | SQLite 运行时文件，由 CSV 重建，无需随包携带 |

> ⚠️ 关于 git 历史：本机出网对 `github.com:443` 是间歇性可达的，`git push` 走不通，
> 所以本机的提交是通过 GitHub API 推送的。**本机 git 历史与远端是两条平行线**（内容一致、历史不同）。
> 换设备后建议直接 `git clone` 远端仓库，拿到的是云端的线性历史。

## 在新设备上跑起来

1. 解压，进入 `ai-trending-radar-v1/`
2. 复制 `.env.example` 为 `.env`，填入下面 5 行
3. `python run.py` —— **零第三方依赖**，不需要 pip install，Python 3.10+ 即可

`.env` 内容：

```
GITHUB_TOKEN=<你的 GitHub PAT，需 repo + workflow scope>
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=1211995677@qq.com
SMTP_PASS=<QQ 邮箱 16 位授权码>
MAIL_TO=13077936420@163.com
```

## 常用命令

```bash
python run.py                      # 全流程：采集 → 分类 → 指标 → 报告
python run.py --no-collect         # 只用库内快照重算并重渲染（几秒）
python run.py --notify             # 生成后推送邮件
python run.py --reclassify         # 改了领域规则后重算标签
python persist.py import           # CSV → SQLite（云端流程的第一步）
python persist.py export           # SQLite → CSV
python tools/check_mail.py         # SMTP 连通性自检（会发测试邮件）
python tools/check_remote.py       # 检查 Token 权限与仓库状态
python tools/check_report.py       # 报告体检（数据 + 脚本语法）
python tools/gh_actions.py watch   # 触发云端 workflow 并跟踪结果
python tools/smoke_stage1.py       # 骨架自检
```

## 设计要点（改之前建议先看）

- 想调领域 / 关键词 / 阈值 → 只改 `config.py`，不碰管道代码
- 报告是**单文件 HTML**（数据内联 + 静态预渲染），改样式改 `render.py` 里的 `TEMPLATE`
- 云端调度：`.github/workflows/daily-radar.yml`，cron 用 UTC（北京 9:00 = `0 1 * * *`）
- 完整设计文档见 `openspec/changes/add-ai-trending-radar-v1/design.md`

## 在线仓库

https://github.com/yao-ding-helloworld/GithubPrj-AIReport-to-QQmail

也可以直接 `git clone` 上面这个地址。

---
由本地流水线自动生成，仅作开发交接用。
""".format(zip=zip_name, count=file_count, size=size_mb, tag=tag,
           gitflag="✅" if has_git else "❌（附件体积考虑已剔除）",
           gitnote="，含完整 git 历史" if has_git else "，不含 git 历史")


def send(zip_path, markdown, tag="nogit"):
    settings = env.smtp_settings()
    if not settings:
        say("[FAIL] SMTP 未配置：%s" % ", ".join(env.missing(env.SMTP_REQUIRED)))
        return False

    msg = EmailMessage()
    msg["Subject"] = "[AI 开源雷达] 完整项目代码包 · %s · %s" % (
        date.today().isoformat(), tag)
    msg["From"] = settings["user"]
    msg["To"] = ", ".join(settings["to"])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(markdown)
    msg.add_alternative(digest.md_to_html(markdown), subtype="html")

    payload = zip_path.read_bytes()
    msg.add_attachment(payload, maintype="application", subtype="zip",
                       filename=zip_path.name)

    try:
        if settings["use_ssl"]:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=60,
                                  context=ssl.create_default_context()) as server:
                server.login(settings["user"], settings["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=60) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(settings["user"], settings["password"])
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        say("[FAIL] SMTP 认证失败——SMTP_PASS 应为 16 位授权码，不是登录密码")
        return False
    except Exception as exc:                                # noqa: BLE001
        say("[FAIL] 发送失败：%s: %s" % (type(exc).__name__, exc))
        return False

    say("[PASS] 已发送至 %s" % ", ".join(settings["to"]))
    say("       附件：%s（%.2f MB，%d 字节）"
        % (zip_path.name, len(payload) / 1024.0 / 1024.0, len(payload)))
    return True


def main():
    env.load_env()
    say("== 打包并邮寄项目 ==")
    say("根目录：%s" % config.ROOT)
    tag = commit_tag()
    say("版本标签（当前提交）：%s" % tag)
    say("")

    # 先带 .git 打包；超限则去掉 .git 重打
    zip_path, count = build_zip(drop_git=False, tag=tag)
    size = zip_path.stat().st_size
    has_git = True
    say("首次打包（含 .git）：%d 个文件，%.2f MB" % (count, size / 1024.0 / 1024.0))
    if size > MAX_ATTACH_BYTES:
        say("体积超过 %.0f MB，去掉 .git 重新打包…" % (MAX_ATTACH_BYTES / 1024.0 / 1024.0))
        zip_path, count = build_zip(drop_git=True, tag=tag)
        size = zip_path.stat().st_size
        has_git = False
        say("重新打包：%d 个文件，%.2f MB" % (count, size / 1024.0 / 1024.0))
    say("")

    # 校验：能打开、CRC 全通过、且确认 .env 没混进去
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
        say("[PASS] zip 完整性校验通过（CRC 全部正常）" if bad is None
            else "[FAIL] 损坏条目：%s" % bad)
        leaked = [n for n in names if n.endswith("/.env")]
        say("[PASS] 确认 .env 未入包" if not leaked else "[FAIL] 密钥泄露：%s" % leaked)
        say("       包内条目数：%d" % len(names))
        # 顺手确认这版的关键修复真的在包里
        for probe in ("render.py", "tools/gh_actions.py", "tools/check_report.py"):
            hit = [n for n in names if n.endswith("/" + probe)]
            say("       %s %s" % ("[PASS]" if hit else "[!]   ", probe))
    except Exception as exc:                                # noqa: BLE001
        say("[FAIL] zip 校验失败：%s" % exc)
        return flush(1)
    say("")

    markdown = body_markdown(zip_path.name, count, size / 1024.0 / 1024.0,
                             has_git, tag=tag)
    ok = send(zip_path, markdown, tag=tag)
    say("")
    say("包位置：%s" % zip_path)
    return flush(0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())

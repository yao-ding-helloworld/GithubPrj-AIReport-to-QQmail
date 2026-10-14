# -*- coding: utf-8 -*-
"""零依赖 .env 加载与配置读取。

约定（见 design.md ADR-4）：
- 真实环境变量优先于 .env 文件——这样 GitHub Actions 的 Secrets 天然生效，
  本地 .env 只作补充，不需要两套代码。
- 缺失关键变量时**主动提示**并降级，绝不静默失败、也不中断流程。
"""

import os
import pathlib

import config

ENV_PATH = config.ROOT / ".env"

# 进程内缓存：第一次读取后不再重复解析文件
_loaded = False


def _parse_line(line):
    """把一行 'KEY=VALUE' 解析成 (key, value)；不是有效行则返回 (None, None)。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None, None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    # 去掉成对的引号（'...' 或 "..."）
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return (key, value) if key else (None, None)


def load_env(path=None, override=False):
    """解析 .env 并写入 os.environ。

    override=False（默认）时，已存在的真实环境变量不被覆盖。
    """
    global _loaded
    path = pathlib.Path(path) if path else ENV_PATH
    if not path.exists():
        _loaded = True
        return {}
    parsed = {}
    with open(path, "r", encoding="utf-8-sig") as fh:   # utf-8-sig 兼容 Windows 记事本 BOM
        for raw in fh:
            key, value = _parse_line(raw)
            if key is None:
                continue
            parsed[key] = value
            if override or key not in os.environ:
                os.environ[key] = value
    _loaded = True
    return parsed


def _ensure_loaded():
    if not _loaded:
        load_env()


def get(name, default=None):
    """读一个配置项，空字符串视为未设置。"""
    _ensure_loaded()
    value = os.environ.get(name, "")
    return value if value.strip() else default


def has(name):
    return get(name) is not None


def missing(names):
    """返回 names 里未配置的变量名列表。"""
    return [n for n in names if not has(n)]


def mask(value):
    """日志用的脱敏展示：只露前 3 位与长度，不泄露明文。"""
    if not value:
        return "<未配置>"
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + "(%d 位)" % len(value)


# ---------------------------------------------------------------- GitHub
def github_token():
    """GitHub Token；缺失返回 None（调用方降级为匿名模式）。"""
    return get("GITHUB_TOKEN") or get("GH_API_TOKEN")


def github_mode():
    """返回 'token' 或 'anonymous'。"""
    return "token" if github_token() else "anonymous"


def describe_github():
    """给日志用的一句话说明。"""
    if github_token():
        return "GitHub 认证：Token 模式（5000 req/h，启用趋势回溯）"
    return (
        "GitHub 认证：匿名模式（60 req/h，已跳过趋势回溯）\n"
        "  想要完整的趋势榜，请在 .env 中补充 GITHUB_TOKEN\n"
        "  获取方式：GitHub → Settings → Developer settings → Personal access tokens\n"
        "  Fine-grained token 只需勾选 Public Repositories (read-only)，无需任何 scope"
    )


# ---------------------------------------------------------------- 邮件推送
SMTP_REQUIRED = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]


def smtp_settings():
    """读 SMTP 配置；返回 dict 或 None（None 表示未配置完整）。"""
    miss = missing(SMTP_REQUIRED)
    if miss:
        return None
    port = get("SMTP_PORT", "465")
    try:
        port = int(port)
    except ValueError:
        port = 465
    return {
        "host": get("SMTP_HOST"),
        "port": port,
        "user": get("SMTP_USER"),
        "password": get("SMTP_PASS"),
        "to": [a.strip() for a in (get("MAIL_TO") or "").split(",") if a.strip()],
        "use_ssl": port != 587,     # 465 = SSL 直连；587 = STARTTLS
    }


def describe_smtp():
    miss = missing(SMTP_REQUIRED)
    if miss:
        return (
            "邮件推送：未配置（缺少 %s）→ 降级为控制台输出\n"
            "  QQ 邮箱配置：设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成 16 位授权码\n"
            "  然后在 .env 写入：SMTP_HOST=smtp.qq.com / SMTP_PORT=465 / "
            "SMTP_USER=你的邮箱 / SMTP_PASS=授权码 / MAIL_TO=收件邮箱"
        ) % ", ".join(miss)
    return "邮件推送：已配置（%s:%s → %s）" % (
        get("SMTP_HOST"), get("SMTP_PORT", "465"), mask(get("SMTP_USER"))
    )


# ---------------------------------------------------------------- LLM（可选）
LLM_REQUIRED = ["LLM_API_KEY"]


def llm_settings():
    """读 LLM 配置；未配置返回 None（调用方自动降级为规则版简介）。

    用 OpenAI 兼容的 /chat/completions 协议，所以 OpenAI / DeepSeek / 通义 /
    智谱 / 本地 vLLM 都能直接接——只要改 base_url + model。
    """
    key = get("LLM_API_KEY")
    if not key:
        return None
    base = (get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    return {
        "base_url": base,
        "key": key,
        "model": get("LLM_MODEL", "deepseek-chat"),
        "timeout": int(get("LLM_TIMEOUT", "60")),
    }


def describe_llm():
    settings = llm_settings()
    if not settings:
        return ("项目简介：规则版（未配置 LLM_API_KEY）\n"
                "  想要真正的中文简介，在 .env 补 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
    return "项目简介：LLM 版（%s @ %s → %s）" % (
        settings["model"], settings["base_url"], mask(settings["key"]))


# ---------------------------------------------------------------- 输出目录
def ensure_dirs():
    """确保数据目录与产物目录存在。"""
    for rel in (config.DB_PATH, config.REPORT_PATH, config.SNAPSHOT_CSV):
        (config.ROOT / rel).parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    load_env()
    print(describe_github())
    print(describe_smtp())
    print(describe_llm())

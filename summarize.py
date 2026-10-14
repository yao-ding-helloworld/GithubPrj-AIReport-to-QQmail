# -*- coding: utf-8 -*-
"""项目简介层：给每个仓库生成一句话介绍。

两种实现，按是否配置 LLM 自动选择：

1. `RuleSummarizer`（默认，零依赖）
   用 语言 + 领域 + topics + 描述 拼出一句结构化中文骨架。
   优点：免费、离线、绝不会瞎编。缺点：GitHub 描述多为英文，翻译不了。

2. `LlmSummarizer`（可选，需 LLM_API_KEY）
   走 OpenAI 兼容的 /chat/completions，把项目信息压成一句 40 字内的中文简介。
   带**内容指纹缓存**：只有描述/主题变了才重算，所以日常运行几乎没有额外花费，
   缓存随 CSV 一起持久化（见 persist.py），云端重建库也不会重复调用。

设计取舍：把"简介"当作**可降级能力**——没有 key 也照样出报告，只是简介偏朴素。
"""

import hashlib
import json
import re
import urllib.error
import urllib.request

import config
import db
import env

# LLM 调用失败的原因（写进日志，便于用户判断要不要配 key）
LAST_LLM_ERROR = [None]


# ---------------------------------------------------------------- 指纹
def content_hash(repo):
    """简介依据的**内容**指纹：描述 / 主题 / 语言。

    注意：这里**不把生成模式算进去**。模式单独放在 `summary_mode` 列里存，
    这样"内容有没有变"和"生成方式有没有变"是两件可以分别判断的事——
    混在一起会出现在云端没配 LLM 密钥时，把已有的中文简介覆盖成规则版的惨剧。
    """
    basis = "|".join([
        (repo.get("description") or "")[:400],
        ",".join(sorted(repo.get("topics") or [])),
        repo.get("language") or "",
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def mode_signature(settings):
    """当前生成模式的签名：rule 或 llm:<模型名>。"""
    return ("llm:" + settings["model"]) if settings else "rule"


def is_downgrade(from_mode, to_mode):
    """从 LLM 版退回规则版属于**降级**。

    云端如果没配 LLM 密钥（或临时调用失败），不该把已经生成好的中文简介
    覆盖成"中文骨架 + 英文原文"。宁可让它略微过期，也不破坏已有内容。
    """
    return to_mode == "rule" and (from_mode or "").startswith("llm")


def should_regenerate(repo, mode_sig, force=False):
    """决定某个仓库的简介要不要重算，返回 (是否重算, 原因)。

    把这段判断单独抽出来是为了能被单测覆盖 —— 这段逻辑同时管着"省钱"
    （命中缓存不重复调 LLM）和"别搞坏数据"（缺密钥时不降级覆盖），
    藏在一堆副作用里很难验证。
    """
    if force:
        return True, "force"
    if not repo.get("summary"):
        return True, "empty"
    same_content = (repo.get("summary_hash") or "") == content_hash(repo)
    stored_mode = repo.get("summary_mode")
    if same_content and stored_mode == mode_sig:
        return False, "cached"
    if is_downgrade(stored_mode, mode_sig):
        return False, "keep-no-downgrade"
    if same_content and stored_mode is None and mode_sig == "rule":
        return False, "keep-legacy"
    return True, "changed"


# ---------------------------------------------------------------- 规则版
def rule_summary(repo, domain_name=None):
    """零依赖的结构化简介（中文骨架 + 原始信息）。"""
    language = (repo.get("language") or "").strip()
    description = (repo.get("description") or "").strip()
    topics = [t for t in (repo.get("topics") or []) if t][:3]
    domain = domain_name or (config.domain_by_key(repo.get("domain")) or {}).get("name")

    head = ""
    if domain:
        head += "%s方向" % domain
    if language:
        head = ("%s · %s" % (language, head)) if head else language
    if not head:
        head = "开源项目"

    body = description
    if body:
        # 描述首句就够，避免长描述挤占手机屏幕
        body = re.split(r"(?<=[.。!！?？])\s+", body)[0]
    elif topics:
        body = "关键词 " + " / ".join(topics)

    text = "%s。%s" % (head, body) if body else head
    return truncate(text, config.SUMMARY_MAX_CHARS)


def truncate(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip(" ,;，；。.") + "…"


# ---------------------------------------------------------------- LLM 版
SYSTEM_PROMPT = (
    "你是技术编辑，为中文读者介绍 GitHub 开源项目。"
    "只输出一句话简介，不超过 40 个汉字，说清三件事：它是什么、解决什么问题、谁适合用。"
    "要求：不要客套话，不要重复项目名，不要罗列技术栈，不要用「这是一个」开头。"
    "直接输出简介正文，不要引号、不要 markdown、不要解释。"
)


def _post(settings, payload, max_tokens):
    """发一次请求，返回 (是否成功, content 或 None, 失败原因)。"""
    payload = dict(payload, max_tokens=max_tokens)
    req = urllib.request.Request(
        settings["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + settings["key"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        return False, None, "HTTP %s %s" % (exc.code, body)
    except Exception as exc:                                # noqa: BLE001
        return False, None, "%s: %s" % (type(exc).__name__, exc)

    try:
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, AttributeError):
        return False, None, ("响应结构异常：%s"
                             % json.dumps(data, ensure_ascii=False)[:200])

    # 推理被截断 → 失败。宁可降级成规则版，也不要写一条空简介
    if choice.get("finish_reason") == "length":
        reasoning = choice["message"].get("reasoning_content") or ""
        return False, None, ("被 max_tokens 截断（当前 %d，推理已占 %d 字符）"
                            % (max_tokens, len(reasoning)))

    text = re.sub(r"^[\s\"'“”‘’]+|[\s\"'“”‘’]+$", "", text)
    text = text.replace("\n", " ").strip()
    if not text:
        return False, None, ("模型返回空内容（finish_reason=%s）"
                            % choice.get("finish_reason"))
    return True, truncate(text, config.SUMMARY_MAX_CHARS), None


def llm_summary(repo, settings):
    """调用 OpenAI 兼容接口生成中文简介；失败返回 None（由调用方降级）。

    推理型模型的思考长度不可预测，所以**截断时自动加倍重试一次**，
    而不是让用户去猜 SUMMARY_MAX_TOKENS 该填多少。
    """
    topics = "、".join((repo.get("topics") or [])[:6])
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "项目名：%s\n主要语言：%s\n主题标签：%s\n英文描述：%s"
                % (repo.get("full_name") or "", repo.get("language") or "-",
                   topics or "-", (repo.get("description") or "-")[:400]))},
        ],
        "temperature": 0.3,
    }

    budget = config.SUMMARY_MAX_TOKENS
    for attempt in (1, 2):
        ok, text, reason = _post(settings, payload, budget)
        if ok:
            LAST_LLM_ERROR[0] = None      # 重试成功后要清掉上一次的失败原因，
            return text                   # 否则日志里会残留一条误导性的 [!]
        LAST_LLM_ERROR[0] = reason
        if "截断" in (reason or "") and attempt == 1:
            budget = min(budget * 2, 4096)
            continue
        return None
    return None


# ---------------------------------------------------------------- 主入口
def summarize_repos(conn, repos=None, force=False, limit=None, verbose=True):
    """给仓库补简介。返回统计 dict。

    repos 为 None 时处理库内全部仓库；可传入榜单用到的子集以省成本。
    limit 限制本次最多调用 LLM 的次数。
    """
    settings = env.llm_settings()
    mode = "llm" if settings else "rule"
    mode_sig = mode_signature(settings)
    limit = config.SUMMARY_LLM_LIMIT if limit is None else limit
    candidates = repos if repos is not None else list(db.iter_repos(conn))

    updates = []
    reused = 0
    generated = 0
    llm_calls = 0
    fallbacks = 0
    kept = 0
    domain_names = {d["key"]: d["name"] for d in config.DOMAINS}

    for repo in candidates:
        need, reason = should_regenerate(repo, mode_sig, force=force)
        if not need:
            if reason == "cached":
                reused += 1
            else:
                kept += 1
            continue

        text = None
        actual_mode = mode_sig
        if settings and llm_calls < limit:
            text = llm_summary(repo, settings)
            if text:
                llm_calls += 1
            else:
                # 降级项用 rule 模式标记：下次运行会因模式不同而重试 LLM，
                # 否则失败一次就被永久缓存成规则版了。
                fallbacks += 1
                actual_mode = "rule"
        if not text:
            text = rule_summary(repo, domain_names.get(repo.get("domain")))
        updates.append((repo["repo_id"], text,
                        content_hash(repo), actual_mode))
        generated += 1

    applied = db.set_summaries(conn, updates) if updates else 0
    total = db.count_summaries(conn)

    if verbose:
        print("项目简介：新增/更新 %d 条，命中缓存 %d 条，保留旧版 %d 条（模式：%s）"
              % (generated, reused, kept, mode))
        print("  写入校验：实际生效 %d 行，库内有简介 %d 行" % (applied, total))
        if updates and applied == 0:
            print("  [FAIL] 有 %d 条待写却一行都没生效 —— repo_id 对不上或连接异常"
                  % len(updates))
        if kept:
            print("  保留旧版说明：这些仓库已有 LLM 版简介，本次以规则模式运行，不覆盖")
        if mode == "llm":
            print("  LLM 调用成功 %d 次（上限 %d），降级 %d 条"
                  % (llm_calls, limit, fallbacks))
            if LAST_LLM_ERROR[0]:
                print("  [!] 有调用失败，已降级为规则版：%s" % LAST_LLM_ERROR[0])
        else:
            print("  " + env.describe_llm().replace("\n", "\n  "))

    return {"mode": mode, "generated": generated, "reused": reused, "kept": kept,
            "applied": applied, "total": total, "fallbacks": fallbacks,
            "llm_calls": llm_calls, "error": LAST_LLM_ERROR[0]}


if __name__ == "__main__":
    conn = db.init()
    print(summarize_repos(conn, force=True))

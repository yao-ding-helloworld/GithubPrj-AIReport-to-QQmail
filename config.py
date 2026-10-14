# -*- coding: utf-8 -*-
"""全局配置：时间窗口、阈值、领域与归类规则。

设计约定（见 openspec/.../design.md 的 ADR-5）：
- 调优只改本文件，不触碰采集 / 指标 / 渲染代码
- 新增领域 = 在 DOMAINS 里追加一个 dict，不改任何逻辑
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent   # 项目根目录（本文件所在目录）

# ---------------------------------------------------------------- 路径
DB_PATH = ".data/radar.db"
REPORT_PATH = "dist/report.html"
SNAPSHOT_CSV = ".data/snapshots.csv"       # 云端持久化载体（CSV ↔ SQLite）
REPOS_CSV = ".data/repos.csv"
WINDOWS_CSV = ".data/windows.csv"

# ---------------------------------------------------------------- 时间窗口
# period -> (天数, 中文名, 邮件的榜单标签)
PERIODS = {
    "weekly": (7, "周榜"),
    "monthly": (30, "月榜"),
    "quarterly": (91, "季榜"),
}

# ---------------------------------------------------------------- 榜单口径
TOP_N = 10                 # 每领域每榜默认条数
STAR_MIN = 300             # 进入候选池的 star 下限（新晋查询另有阈值）

# ---------------------------------------------------------------- 新晋项目（冷启动趋势来源）
# 注意：GitHub 的 created: 限定符只接受绝对日期（created:>2026-04-17），
# 写成相对时长 created:>{created_after} 会直接 422。所以这里用占位符，采集时替换成真实日期。
NEWCOMER_DAYS = 180        # created:>{created_after}
NEWCOMER_STAR_MIN = 300

# ---------------------------------------------------------------- 采集节流与重试
SEARCH_SLEEP = 2.5         # token 模式：相邻 search 请求最小间隔（秒）
SEARCH_SLEEP_ANON = 6.5    # 匿名模式：10 req/min，留足余量
REQUEST_TIMEOUT = 20
MAX_RETRY = 3              # 失败重试次数（指数退避）
BACKOFF_BASE = 3.0         # 退避基数：3s / 6s / 12s
MAX_SEARCH_PAGES = 1       # 每查询取几页（100 条/页）；sort=stars 下首页即 Top100

# ---------------------------------------------------------------- 回溯（ADR-2）
BACKFILL_ENABLED = True    # 仅 token 模式生效
BACKFILL_MAX_REQUESTS = 600  # 单次运行回溯请求上限，防失控烧额度
BACKFILL_STAR_CEILING = 40000  # 超过此 star 数视为分页受限，直接标 estimated

# ---------------------------------------------------------------- HTTP
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "ai-trending-radar/0.1 (+https://github.com)"

# ---------------------------------------------------------------- 领域配置
# 字段说明：
#   search_queries  发现渠道——把仓库拉进候选池
#   newcomer_query  新晋渠道——拉入"总量小但可能正在爆发"的仓库
#   topic_rules     GitHub topics 精确命中（全小写，去 - 后比较）
#   keyword_rules   名称/描述关键词兜底（全小写子串匹配）
DOMAINS = [
    {
        "key": "agent-frameworks",
        "name": "智能体构造",
        "search_queries": [
            "topic:ai-agent stars:>800",
            "agent framework in:name,description stars:>500",
            "topic:llm-agent stars:>500",
            "topic:multi-agent stars:>500",
        ],
        "newcomer_query": "topic:ai-agent created:>{created_after} stars:>300",
        "topic_rules": [
            "ai-agent", "ai-agents", "aiagent", "agent", "agents",
            "agent-framework", "agentic", "agentic-ai", "llm-agent",
            "multi-agent", "multiagent", "autonomous-agents", "ai-agents-framework",
            "langchain", "langgraph", "autogen", "crewai", "agno", "phidata",
            "openai-agents", "llamaindex", "semantic-kernel", "haystack",
            "model-context-protocol", "mcp",
        ],
        "keyword_rules": [
            "agent framework", "agentic", "multi-agent", "autonomous agent",
            "ai agent", "llm agent", "react agent", "tool calling",
            "workflow orchestration", "智能体",
        ],
    },
    {
        "key": "agent-optimization",
        "name": "智能体优化",
        "search_queries": [
            "topic:prompt-engineering stars:>500",
            "topic:llm-evaluation stars:>300",
            "dspy OR \"prompt optimization\" in:name,description stars:>300",
            "topic:agent-memory stars:>300",
        ],
        "newcomer_query": "topic:prompt-engineering created:>{created_after} stars:>300",
        "topic_rules": [
            "prompt-engineering", "prompt-optimization", "prompt-tuning",
            "dspy", "llm-evaluation", "llm-eval", "evaluation", "eval",
            "agent-memory", "long-term-memory", "memory", "context-engineering",
            "chain-of-thought", "reasoning", "self-refine", "reflexion",
            "guardrails", "observability", "tracing", "langfuse",
        ],
        "keyword_rules": [
            "prompt engineering", "prompt optimization", "prompt tuning",
            "llm evaluation", "agent memory", "context engineering",
            "reasoning trace", "self-refine", "reflexion", "guardrail",
            "提示工程", "提示词", "评测",
        ],
    },
    {
        "key": "llm-theory",
        "name": "大模型理论",
        "search_queries": [
            "topic:llm topic:training stars:>500",
            "transformer implementation in:name,description stars:>400",
            "topic:quantization stars:>300",
            "topic:mixture-of-experts stars:>300",
        ],
        "newcomer_query": "topic:transformer created:>{created_after} stars:>300",
        "topic_rules": [
            "transformer", "transformers", "attention", "attention-mechanism",
            "llm-training", "pretraining", "pre-training", "training",
            "fine-tuning", "finetuning", "lora", "qlora", "peft",
            "quantization", "quantization-aware-training", "moe",
            "mixture-of-experts", "distillation", "rlhf", "dpo", "grpo",
            "tokenizer", "tokenization", "kv-cache", "flash-attention",
            "cuda", "cuda-kernels", "triton", "kernels", "deep-learning",
            "neural-network", "scaling-law", "inference-engine",
        ],
        "keyword_rules": [
            "transformer", "attention", "pretrain", "pre-train", "fine-tun",
            "finetun", "lora", "quantiz", "mixture of experts", "distillat",
            "rlhf", "tokenizer", "kv cache", "flash attention", "cuda kernel",
            "scaling law", "大模型", "量化", "微调",
        ],
    },
    # ---- 以下为 v1 预留位，enabled=False 时不参与采集与榜单 ----
    {
        "key": "rag",
        "name": "检索增强",
        "enabled": False,
        "search_queries": ["topic:rag stars:>500", "topic:vector-database stars:>500"],
        "newcomer_query": "topic:rag created:>{created_after} stars:>300",
        "topic_rules": ["rag", "retrieval-augmented-generation", "vector-database", "embedding"],
        "keyword_rules": ["retrieval augmented", "vector database", "rerank"],
    },
    {
        "key": "tooling-ecosystem",
        "name": "工具与协议生态",
        "enabled": False,
        "search_queries": ["topic:model-context-protocol stars:>300"],
        "newcomer_query": "topic:model-context-protocol created:>{created_after} stars:>200",
        "topic_rules": ["model-context-protocol", "mcp-server", "function-calling"],
        "keyword_rules": ["mcp server", "tool protocol", "function calling"],
    },
    {
        "key": "multimodal-eval",
        "name": "多模态与评测",
        "enabled": False,
        "search_queries": ["topic:multimodal stars:>500", "topic:llm-benchmark stars:>300"],
        "newcomer_query": "topic:multimodal created:>{created_after} stars:>300",
        "topic_rules": ["multimodal", "vision-language", "llm-benchmark", "benchmark"],
        "keyword_rules": ["multimodal", "vision language", "benchmark suite"],
    },
]


def enabled_domains():
    """返回参与本次运行的领域列表（enabled 缺省视为 True）。"""
    return [d for d in DOMAINS if d.get("enabled", True)]


def domain_by_key(key):
    """按 key 取领域配置；不存在返回 None。"""
    for d in DOMAINS:
        if d["key"] == key:
            return d
    return None


# ---------------------------------------------------------------- 内容卫生
# 描述入库截断长度。真实世界里存在"超长描述 + 关键词堆砌"的仓库
# （实测有一个 64773 字符的描述），既撑大报告体积，又会造成关键词误分类。
DESC_MAX = 300

# 内容黑名单：命中即不进候选池。用途是过滤非技术内容（政治宣传、违规信息），
# 避免污染技术榜单；规则命中即整体跳过，不做局部替换。
BLOCKED_TERMS = [
    "反共", "反华", "辱华", "法轮功", "六四", "台独", "藏独", "疆独", "港独",
    "邪教", "政治宣传", "政治敏感",
]


def is_blocked(text):
    """text 命中黑名单返回 True。"""
    if not text:
        return False
    lowered = str(text).lower()
    return any(term.lower() in lowered for term in BLOCKED_TERMS)


def clean_text(text, limit=None):
    """截断 + 折叠空白，防止超长文本污染存储与报告。"""
    if not text:
        return ""
    text = " ".join(str(text).split())
    limit = DESC_MAX if limit is None else limit
    return text[:limit]


# ---------------------------------------------------------------- 邮件
MAIL_TOP_N = 3             # 邮件正文里每个榜单放几条（手机上一屏能看完）
MAIL_MAX_ITEMS = 3         # 每个榜单最多几条（超长时压缩用）

# ---------------------------------------------------------------- 项目简介
SUMMARY_ENABLED = True
SUMMARY_MAX_CHARS = 78     # 简介最大长度；手机端一行半左右
SUMMARY_LLM_LIMIT = 300    # 单次运行最多调用 LLM 几次（控制花费）

# 兜底领域：规则全不命中时的归属（不参与榜单展示，只看占比）
FALLBACK_DOMAIN = "other"

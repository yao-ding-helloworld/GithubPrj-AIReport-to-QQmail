# -*- coding: utf-8 -*-
"""简介决策逻辑单测。

这段逻辑同时管着两件事，所以必须可验证：
  ① 省钱 —— 内容没变就别重复调用 LLM
  ② 别搞坏数据 —— 云端缺 LLM 密钥时，不能把已有的中文简介覆盖成规则版

跑法：python tools/test_summarize.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import summarize   # noqa: E402

CONTENT = {"description": "An agent framework for x", "topics": ["agents", "llm"],
           "language": "Python"}
HASH = summarize.content_hash(CONTENT)
LLM = "llm:deepseek-flash"
OTHER_HASH = "0" * 40
out = []
results = []


def repo(**over):
    """在固定内容上进补丁，避免漏字段导致指纹算错（第一版测试就栽在这上面）。"""
    base = dict(CONTENT)
    base.update(over)
    return base


def case(label, payload, mode_sig, expect_need, expect_reason, force=False):
    need, reason = summarize.should_regenerate(payload, mode_sig, force=force)
    ok = (need == expect_need) and (reason == expect_reason)
    results.append(ok)
    out.append("  %s %-40s need=%-5s reason=%s"
               % ("[PASS]" if ok else "[FAIL]", label, need, reason))


out.append("== 简介决策逻辑单测 ==")
out.append("")

# ① 没有简介 → 必须生成
case("没有简介", repo(), LLM, True, "empty")

# ② 内容与模式都一致 → 命中缓存（省钱的关键）
case("内容+模式均一致", repo(summary="s", summary_hash=HASH, summary_mode=LLM),
     LLM, False, "cached")

# ③ 云端缺密钥（llm → rule）→ 保留，绝不降级覆盖
case("降级保护：LLM 版遇到规则模式",
     repo(summary="s", summary_hash=HASH, summary_mode=LLM), "rule",
     False, "keep-no-downgrade")

# ④ 内容变了 + 只是缺密钥 → 仍保留（宁可略旧也不降级）
case("降级保护：内容变了但属降级",
     repo(summary="s", summary_hash=OTHER_HASH, summary_mode=LLM), "rule",
     False, "keep-no-downgrade")

# ⑤ 规则版升级到 LLM 版 → 重算
case("规则版升级到 LLM 版",
     repo(summary="s", summary_hash=HASH, summary_mode="rule"), LLM,
     True, "changed")

# ⑥ 换模型 → 重算
case("换 LLM 模型",
     repo(summary="s", summary_hash=HASH, summary_mode="llm:old-model"), LLM,
     True, "changed")

# ⑦ 内容变了 + 同模式 → 重算
case("内容变了（同模式）",
     repo(summary="s", summary_hash=OTHER_HASH, summary_mode=LLM), LLM,
     True, "changed")

# ⑧ 老数据（模式未知）+ 规则模式 → 不动
case("老数据（模式未知）+ 规则模式",
     repo(summary="s", summary_hash=HASH), "rule", False, "keep-legacy")

# ⑨ 老数据 + LLM 模式 → 重算，顺带把模式元数据补上
case("老数据（模式未知）+ LLM 模式",
     repo(summary="s", summary_hash=HASH), LLM, True, "changed")

# ⑩ --resummarize 强制重算
case("force 强制重算",
     repo(summary="s", summary_hash=HASH, summary_mode=LLM), LLM,
     True, "force", force=True)

out.append("")
out.append("结果：%d/%d 通过" % (sum(results), len(results)))
lines = "\n".join(out)
print(lines)
sys.exit(0 if all(results) else 1)

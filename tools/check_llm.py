# -*- coding: utf-8 -*-
"""LLM 连通性自检：先花一次调用确认配置对不对，再拿去批量生成简介。

失败时把原因翻成人话（key 无效 / 模型不存在 / 端点不对 / 余额不足 / 超时），
避免"配了个错的模型名，跑完整轮 46 次才在日志里发现"。

跑法：python tools/check_llm.py
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import db          # noqa: E402
import env         # noqa: E402
import summarize   # noqa: E402

LINES = []


def say(text=""):
    LINES.append(str(text))


def raw_call(settings, messages, max_tokens=120):
    """直接发一次请求，把 HTTP 状态与响应体原样带回来（便于诊断）。"""
    payload = {
        "model": settings["model"],
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
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
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"raw": body[:500]}
    except Exception as exc:                                # noqa: BLE001
        return None, {"error": "%s: %s" % (type(exc).__name__, exc)}


def diagnose(status, body):
    if status is None:
        say("  网络层失败：%s" % body.get("error"))
        say("  → 检查 base_url 是否写对、本机能否出网（代理/防火墙）")
        return
    message = json.dumps(body, ensure_ascii=False)[:400]
    say("  HTTP %s：%s" % (status, message))
    if status == 401:
        say("  → API key 无效或已失效")
    elif status == 402:
        say("  → 余额不足，去控制台充值")
    elif status == 404:
        say("  → 端点或模型名不存在。先确认 base_url 末尾是不是 /v1，"
            "再确认 model 名字在服务商那边确实存在")
    elif status == 400 and "model" in message.lower():
        say("  → 模型名不被该端点接受，换一个 model 名字再试")
    elif status == 429:
        say("  → 触发限流，稍后重试")


def main():
    env.load_env()
    settings = env.llm_settings()

    say("== LLM 连通性自检 ==")
    say("")
    if not settings:
        say("[FAIL] 没读到 LLM 配置：%s" % ", ".join(env.missing(env.LLM_REQUIRED)))
        say("")
        say(env.describe_llm())
        return 1

    say("base_url  %s" % settings["base_url"])
    say("model     %s" % settings["model"])
    say("api_key   %s" % env.mask(settings["key"]))
    say("请求地址  %s/chat/completions" % settings["base_url"])
    say("")

    say("--- 第 1 步：最小请求（探活） ---")
    # 探活的 max_tokens 也要给足：推理型模型会先"思考"，额度太小答案会被挤空，
    # 看起来像调用失败，其实只是被截断了。
    status, body = raw_call(settings, [{"role": "user", "content": "回复两个字：可用"}],
                            max_tokens=512)
    if status != 200:
        diagnose(status, body)
        return 1
    try:
        reply = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        say("  返回体结构异常：%s" % json.dumps(body, ensure_ascii=False)[:300])
        return 1
    say("[PASS] 调用成功，模型回复：%r" % reply)
    usage = body.get("usage") or {}
    say("  token 用量：prompt=%s completion=%s" % (usage.get("prompt_tokens"),
                                                  usage.get("completion_tokens")))
    say("")

    say("--- 第 2 步：拿真实仓库试一句简介 ---")
    conn = db.init()
    repos = db.iter_repos(conn)
    if not repos:
        say("  库里没有仓库，跳过")
        return 0
    target = repos[0]
    say("  目标：%s（%s）" % (target["full_name"], target.get("language") or "-"))
    say("  英文描述：%s" % (target.get("description") or "-")[:120])
    text = summarize.llm_summary(target, settings)
    if not text:
        say("[FAIL] 失败：%s" % summarize.LAST_LLM_ERROR[0])
        return 1
    say("[PASS] 中文简介：%s" % text)
    say("  长度：%d 字（上限 %d）" % (len(text), config.SUMMARY_MAX_CHARS))
    say("")

    # 目标耗时参考：批量生成时用来估算总时长
    say("--- 结论文 ---")
    say("[PASS] LLM 配置可用，可以直接批量生成中文简介")
    say("       预期消耗：每个仓库约 70 prompt + 600 completion token")
    say("       （推理型模型会把大头花在思考上，所以单条并不省，但有内容指纹缓存，")
    say("         日常只有新上榜的仓库才会调用）")
    return 0


if __name__ == "__main__":
    code = main()
    (config.ROOT / ".data" / "check_llm.txt").write_text("\n".join(LINES),
                                                        encoding="utf-8")
    print("\n".join(LINES))
    sys.exit(code)

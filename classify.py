# -*- coding: utf-8 -*-
"""分类层：把仓库归入领域（阶段 3）。

对应 specs/domain-classification/spec.md：
- 三级判定：topics 精确命中 → 名称/描述关键词 → other 兜底
- 多领域同时命中：DOMAINS 顺序里第一个作主领域，其余记 secondary（不参与榜单）
- 配置即扩展：新增领域只改 config.DOMAINS
- 支持重算：--reclassify 对库内已有数据重新打标
"""

import json

import config
import db


def normalize_topic(topic):
    """topics 归一：小写、下划线转连字符，便于规则书写。"""
    return str(topic).strip().lower().replace("_", "-")


def _topic_hits(topics, rules):
    return [rule for rule in rules if normalize_topic(rule) in topics]


def _keyword_hits(text, rules):
    return [rule for rule in rules if rule in text]


def classify_one(repo, domains=None):
    """判定单个仓库的 (主领域, 次要领域列表)。

    repo 为 dict，需含 topics（list）与 full_name / description。
    规则全不命中时主领域为 config.FALLBACK_DOMAIN。
    """
    domains = domains or config.enabled_domains()
    topics = {normalize_topic(t) for t in (repo.get("topics") or [])}
    # 描述截断后再匹配：防止"超长描述堆砌关键词"造成误分类
    text = ("%s %s" % (repo.get("full_name") or "",
                       config.clean_text(repo.get("description")))).lower()

    matched = []
    for domain in domains:
        if _topic_hits(topics, domain.get("topic_rules", [])):
            matched.append(domain["key"])
            continue
        if _keyword_hits(text, domain.get("keyword_rules", [])):
            matched.append(domain["key"])

    if not matched:
        return config.FALLBACK_DOMAIN, []
    return matched[0], matched[1:]


def classify_repos(conn, domains=None, reclassify=False, verbose=True):
    """给库内仓库打领域标签，返回统计。

    reclassify=False 时，标签无变化的仓库会被跳过（省掉无意义的写盘）；
    规则改动后用 True 全量重算。
    """
    domains = domains or config.enabled_domains()
    updated = 0
    skipped = 0
    buckets = {}

    # 先清掉内容黑名单命中项：保证报告里不会出现非技术内容
    purged = db.purge_blocked(conn, config.BLOCKED_TERMS)

    for repo in db.iter_repos(conn):
        primary, secondary = classify_one(repo, domains)
        changed = (repo.get("domain") != primary) or \
                  ((repo.get("secondary") or []) != secondary)
        if changed or reclassify:
            conn.execute(
                "UPDATE repos SET domain=?, secondary=? WHERE repo_id=?",
                (primary, json.dumps(secondary, ensure_ascii=False), repo["repo_id"]),
            )
            updated += 1
        else:
            skipped += 1
        buckets[primary] = buckets.get(primary, 0) + 1

    conn.commit()

    total = sum(buckets.values()) or 1
    other_ratio = buckets.get(config.FALLBACK_DOMAIN, 0) / float(total)

    if verbose:
        if purged:
            print("内容过滤：清除 %d 个命中黑名单的仓库" % purged)
        print("分类完成：更新 %d 条，跳过 %d 条" % (updated, skipped))
        for key in sorted(buckets, key=lambda k: -buckets[k]):
            domain = config.domain_by_key(key) or {}
            print("  %-22s %-12s %d" % (key, domain.get("name", ""), buckets[key]))
        if other_ratio > 0.2:
            print("  [!] other 占比 %.1f%%（>20%%），建议补充 keyword_rules" % (other_ratio * 100))

    return {"updated": updated, "skipped": skipped, "purged": purged,
            "buckets": buckets, "other_ratio": other_ratio}

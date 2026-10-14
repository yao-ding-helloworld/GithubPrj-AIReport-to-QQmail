# -*- coding: utf-8 -*-
"""SQLite 存储层（见 design.md ADR-3）。

三张表：
  repos     仓库主档（含领域标签）
  snapshots 每日 star 快照——趋势计算的原料
  windows   按时间窗算好的净增量（缓存，可随时重算）

单文件、零部署；同日快照用 upsert 保证采集幂等（重跑不产生重复行）。
"""

import datetime
import json
import sqlite3
import pathlib

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    repo_id     INTEGER PRIMARY KEY,
    full_name   TEXT UNIQUE NOT NULL,
    description TEXT,
    language    TEXT,
    topics      TEXT,              -- JSON 数组
    domain      TEXT,              -- 主领域 key；'other' 表示未命中任何规则
    secondary   TEXT,              -- JSON 数组，次要领域（不参与榜单）
    stars       INTEGER,           -- 最近一次采集到的 star 总量
    first_seen  TEXT,              -- ISO 日期，首次进入候选池
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    repo_id   INTEGER NOT NULL,
    snap_date TEXT    NOT NULL,    -- ISO 日期 YYYY-MM-DD
    stars     INTEGER NOT NULL,
    PRIMARY KEY (repo_id, snap_date)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(snap_date);

CREATE TABLE IF NOT EXISTS windows (
    period      TEXT    NOT NULL,  -- weekly | monthly | quarterly
    repo_id     INTEGER NOT NULL,
    stars_delta INTEGER NOT NULL,
    source      TEXT    NOT NULL,  -- snapshot（准）| backfill（估算）
    estimated   INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT,
    PRIMARY KEY (period, repo_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def today_iso():
    """返回本地日期 YYYY-MM-DD。"""
    return datetime.date.today().isoformat()


def _resolve(path):
    if path is None:
        path = config.DB_PATH
    path = pathlib.Path(path)
    return path if path.is_absolute() else (config.ROOT / path)


def connect(path=None):
    """打开（必要时创建）数据库连接。"""
    db_path = _resolve(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path=None):
    """建表 + 建索引，可重复调用。返回连接对象。"""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 写入
REPO_FIELDS = (
    "repo_id", "full_name", "description", "language",
    "topics", "domain", "secondary", "stars", "first_seen", "updated_at",
)


def upsert_repo(conn, repo):
    """写入/更新一个仓库主档。repo 为 dict；topics/secondary 可为 list。"""
    row = dict(repo)
    for key in ("topics", "secondary"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            row[key] = json.dumps(list(value), ensure_ascii=False)
    row.setdefault("first_seen", today_iso())
    row["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    values = [row.get(f) for f in REPO_FIELDS]
    placeholders = ",".join("?" * len(REPO_FIELDS))
    # 冲突时保留已有的 first_seen（首次进入日期不因重跑而后移）
    conn.execute(
        "INSERT INTO repos (%s) VALUES (%s) "
        "ON CONFLICT(repo_id) DO UPDATE SET "
        "  full_name=excluded.full_name, description=excluded.description, "
        "  language=excluded.language, topics=excluded.topics, "
        "  domain=excluded.domain, secondary=excluded.secondary, "
        "  stars=excluded.stars, updated_at=excluded.updated_at"
        % (",".join(REPO_FIELDS), placeholders),
        values,
    )


def upsert_repos(conn, repos):
    """批量写入仓库主档。返回写入条数。"""
    count = 0
    for repo in repos:
        upsert_repo(conn, repo)
        count += 1
    conn.commit()
    return count


def upsert_snapshots(conn, rows, snap_date=None):
    """批量写快照。rows 为 (repo_id, stars) 或 (repo_id, snap_date, stars)。"""
    snap_date = snap_date or today_iso()
    payload = []
    for row in rows:
        if len(row) == 2:
            payload.append((row[0], snap_date, int(row[1])))
        else:
            payload.append((row[0], str(row[1]), int(row[2])))
    conn.executemany(
        "INSERT INTO snapshots (repo_id, snap_date, stars) VALUES (?,?,?) "
        "ON CONFLICT(repo_id, snap_date) DO UPDATE SET stars=excluded.stars",
        payload,
    )
    conn.commit()
    return len(payload)


def upsert_windows(conn, rows):
    """批量写窗口增量。rows 为 dict：period/repo_id/stars_delta/source/estimated。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    payload = [
        (
            r["period"], r["repo_id"], int(r["stars_delta"]),
            r.get("source", "snapshot"), int(r.get("estimated", 0)), now,
        )
        for r in rows
    ]
    conn.executemany(
        "INSERT INTO windows (period, repo_id, stars_delta, source, estimated, computed_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(period, repo_id) DO UPDATE SET "
        "  stars_delta=excluded.stars_delta, source=excluded.source, "
        "  estimated=excluded.estimated, computed_at=excluded.computed_at",
        payload,
    )
    conn.commit()
    return len(payload)


# ---------------------------------------------------------------- 读取
def get_meta(conn, key, default=None):
    cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def iter_repos(conn):
    """遍历全部仓库主档（dict 形式，topics/secondary 已反序列化）。"""
    for row in conn.execute("SELECT * FROM repos ORDER BY stars DESC"):
        item = dict(row)
        for key in ("topics", "secondary"):
            try:
                item[key] = json.loads(item[key]) if item[key] else []
            except (TypeError, ValueError):
                item[key] = []
        yield item


def snapshot_on(conn, repo_id, snap_date):
    cur = conn.execute(
        "SELECT stars FROM snapshots WHERE repo_id=? AND snap_date=?",
        (repo_id, snap_date),
    )
    row = cur.fetchone()
    return row["stars"] if row else None


def earliest_snapshot_since(conn, repo_id, since_date):
    """窗口内最早的一张快照 → (date, stars)；没有则 (None, None)。"""
    cur = conn.execute(
        "SELECT snap_date, stars FROM snapshots "
        "WHERE repo_id=? AND snap_date>=? ORDER BY snap_date ASC LIMIT 1",
        (repo_id, since_date),
    )
    row = cur.fetchone()
    return (row["snap_date"], row["stars"]) if row else (None, None)


def latest_snapshot(conn, repo_id):
    cur = conn.execute(
        "SELECT snap_date, stars FROM snapshots "
        "WHERE repo_id=? ORDER BY snap_date DESC LIMIT 1",
        (repo_id,),
    )
    row = cur.fetchone()
    return (row["snap_date"], row["stars"]) if row else (None, None)


def snapshot_series(conn, repo_id, limit=30):
    """倒序取最近 limit 个快照，返回正序列表 [(date, stars), ...]（画 sparkline 用）。"""
    rows = conn.execute(
        "SELECT snap_date, stars FROM snapshots WHERE repo_id=? "
        "ORDER BY snap_date DESC LIMIT ?",
        (repo_id, limit),
    ).fetchall()
    return [(r["snap_date"], r["stars"]) for r in reversed(rows)]


def window_rows(conn, period):
    """取某窗口的全部增量记录，按降序返回。"""
    rows = conn.execute(
        "SELECT w.*, r.full_name, r.domain, r.language, r.stars, r.description "
        "FROM windows w JOIN repos r ON r.repo_id = w.repo_id "
        "WHERE w.period=? ORDER BY w.stars_delta DESC",
        (period,),
    ).fetchall()
    return [dict(r) for r in rows]


def stats(conn):
    """库内计数，供日志与冒烟检查使用。"""
    def count(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "repos": count("SELECT COUNT(*) FROM repos"),
        "snapshots": count("SELECT COUNT(*) FROM snapshots"),
        "snapshot_days": count("SELECT COUNT(DISTINCT snap_date) FROM snapshots"),
        "windows": count("SELECT COUNT(*) FROM windows"),
        "latest_date": conn.execute(
            "SELECT MAX(snap_date) FROM snapshots"
        ).fetchone()[0],
    }


def purge_blocked(conn, blocklist):
    """把命中内容黑名单的仓库连同其快照/窗口记录一并清掉。

    用于兜底：即使早期版本已把不该收的仓库写进库，重新分类时也会被清走。
    返回清除的仓库数。
    """
    if not blocklist:
        return 0
    clauses = " OR ".join(["full_name LIKE ? OR description LIKE ?"] * len(blocklist))
    params = []
    for term in blocklist:
        params.extend(["%" + term + "%", "%" + term + "%"])
    ids = [row[0] for row in conn.execute(
        "SELECT repo_id FROM repos WHERE " + clauses, params)]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    for table in ("snapshots", "windows", "repos"):
        conn.execute("DELETE FROM %s WHERE repo_id IN (%s)" % (table, marks), ids)
    conn.commit()
    return len(ids)


if __name__ == "__main__":
    conn = init()
    print("数据库已就绪：%s" % _resolve(None))
    print(stats(conn))

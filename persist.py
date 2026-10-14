# -*- coding: utf-8 -*-
"""快照持久化：CSV ↔ SQLite（阶段 7.3，design.md ADR-9）。

为什么不用 SQLite 直接提交回仓库：
  SQLite 是二进制，每日全量提交会让仓库体积线性膨胀到 GB 级；
  CSV 是文本，git 增量压缩友好（年增量约数 MB），而且可读、可 diff、可人工修正。
所以云端流程是：检出 CSV → 重建库 → 采集 → 导出 CSV 提交。

命令行：
  python persist.py import     # CSV → SQLite（先清空三表再灌）
  python persist.py export     # SQLite → CSV
  python persist.py status     # 看两边各有多少行
"""

import csv
import pathlib
import sys

import config
import db

TABLES = {
    "repos": {
        "file": config.REPOS_CSV,
        "columns": ["repo_id", "full_name", "description", "language", "topics",
                    "domain", "secondary", "stars", "first_seen", "updated_at",
                    # 简介必须随 CSV 走：否则云端每次重建库都会丢掉，
                    # 于是每天重复调用 LLM（白花钱）
                    "summary", "summary_hash", "summary_mode"],
    },
    "snapshots": {
        "file": config.SNAPSHOT_CSV,
        "columns": ["repo_id", "snap_date", "stars"],
    },
    "windows": {
        "file": config.WINDOWS_CSV,
        "columns": ["period", "repo_id", "stars_delta", "source",
                    "estimated", "computed_at"],
    },
}


def _path(rel):
    path = pathlib.Path(rel)
    return path if path.is_absolute() else (config.ROOT / path)


def export_csv(conn, table, path=None, verbose=True):
    """把一张表导出成 CSV（UTF-8 BOM，方便 Excel 直接打开）。"""
    spec = TABLES[table]
    path = _path(path or spec["file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = spec["columns"]
    rows = conn.execute("SELECT %s FROM %s" % (",".join(columns), table)).fetchall()
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if row[c] is None else row[c] for c in columns])
    if verbose:
        print("  导出 %-10s %6d 行 → %s" % (table, len(rows), path.name))
    return len(rows)


def import_csv(conn, table, path=None, clear=True, verbose=True):
    """把 CSV 灌回表。clear=True 时先清空该表（云端重建库的语义）。"""
    spec = TABLES[table]
    path = _path(path or spec["file"])
    if not path.exists():
        if verbose:
            print("  跳过 %-10s（%s 不存在）" % (table, path.name))
        return 0
    columns = spec["columns"]
    if clear:
        conn.execute("DELETE FROM %s" % table)
    count = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        marks = ",".join("?" * len(columns))
        payload = []
        for row in reader:
            payload.append([
                (row.get(c) if row.get(c) not in (None, "") else None)
                for c in columns
            ])
        conn.executemany(
            "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
            % (table, ",".join(columns), marks),
            payload,
        )
        count = len(payload)
    conn.commit()
    if verbose:
        print("  导入 %-10s %6d 行 ← %s" % (table, count, path.name))
    return count


def export_all(conn, verbose=True):
    if verbose:
        print("导出快照载体：")
    return {t: export_csv(conn, t, verbose=verbose) for t in TABLES}


def import_all(conn, clear=True, verbose=True):
    if verbose:
        print("导入快照载体：")
    return {t: import_csv(conn, t, clear=clear, verbose=verbose) for t in TABLES}


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    conn = db.init()
    if cmd == "import":
        import_all(conn)
    elif cmd == "export":
        export_all(conn)
    elif cmd == "status":
        print("库内：%s" % db.stats(conn))
        for table, spec in TABLES.items():
            path = _path(spec["file"])
            rows = "-"
            if path.exists():
                with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                    rows = max(0, sum(1 for _ in fh) - 1)
            print("  %-10s 库内 %6d 行 | CSV %s 行 | %s"
                  % (table,
                     conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0],
                     rows, spec["file"]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

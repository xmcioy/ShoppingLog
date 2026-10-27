#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ShoppingLog 0.2 统一数据库层

支持 SQLite（默认）与 MySQL 双后端。后端选择优先级:
  1. config.local.json 的 db.engine（默认 sqlite）
  2. 环境变量 SHOPLOG_DB_ENGINE（sqlite / mysql）

MySQL 连接参数来自 config.local.json（db.host / db.port / db.user / db.password /
db.database / db.charset），也可用 SHOPLOG_DB_HOST 等环境变量覆盖。

对外只暴露 connect()，返回统一 Conn 对象，接口兼容 sqlite3.Connection 的常用面:
  - execute(sql, params) -> Cursor(含 fetchone/fetchall/lastrowid/rowcount)
  - executescript(sql)   -> 逐条执行（MySQL 无原生 executescript）
  - commit() / close()
行对象兼容 sqlite3.Row：支持 row["col"] / row[0] / row.keys() / iter(row) / dict(row)。

MySQL 方言自动转换（占位符 ? -> %s 等），见 _to_mysql。
"""

import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(os.environ.get("SHOPLOG_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_FILE = ROOT / "config.local.json"


# --------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------
def load_db_config():
    """返回 (engine, cfg_dict)。engine 为 'sqlite' 或 'mysql'。"""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    db_cfg = cfg.get("db") or {}
    engine = (os.environ.get("SHOPLOG_DB_ENGINE") or db_cfg.get("engine") or "sqlite").strip().lower()
    if engine not in ("sqlite", "mysql"):
        engine = "sqlite"
    return engine, db_cfg


def mysql_kwargs(db_cfg):
    """从配置/环境变量组装 pymysql.connect 关键字（缺省不填，让 pymysql 默认）。"""
    def env_or(key, default=None):
        env_map = {
            "host": "SHOPLOG_DB_HOST",
            "port": "SHOPLOG_DB_PORT",
            "user": "SHOPLOG_DB_USER",
            "password": "SHOPLOG_DB_PASSWORD",
            "database": "SHOPLOG_DB_DATABASE",
            "charset": "SHOPLOG_DB_CHARSET",
        }
        v = os.environ.get(env_map.get(key, ""))
        if v is not None:
            return v
        return db_cfg.get(key, default)

    kwargs = {
        "host": env_or("host", "127.0.0.1"),
        "user": env_or("user", "root"),
        "password": env_or("password", ""),
        "database": env_or("database", "shoppinglog"),
        "charset": env_or("charset", "utf8mb4"),
        "autocommit": False,
    }
    port = env_or("port", 3306)
    try:
        kwargs["port"] = int(port)
    except (TypeError, ValueError):
        kwargs["port"] = 3306
    return {k: v for k, v in kwargs.items() if v is not None}


# --------------------------------------------------------------------------
# 行对象（兼容 sqlite3.Row）
# --------------------------------------------------------------------------
class CompatRow:
    """同时支持按列名/下标访问与 keys()/迭代，行为对齐 sqlite3.Row。"""

    def __init__(self, keys, values):
        self._keys = tuple(keys)
        self._vals = tuple(values)

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        try:
            return self._vals[self._keys.index(k)]
        except ValueError:
            raise KeyError(k)

    def __contains__(self, k):
        return k in self._keys

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default

    def __eq__(self, other):
        if isinstance(other, CompatRow):
            return self._keys == other._keys and self._vals == other._vals
        return NotImplemented


# --------------------------------------------------------------------------
# MySQL 方言转换
# --------------------------------------------------------------------------
_DATE_NOW_RE = re.compile(r"date\('now'\s*,\s*\?\)", re.I)
_DATETIME_NOW_RE = re.compile(r"datetime\('now'\s*,\s*'localtime'\)", re.I)
_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.I)
_CONFLICT_RE = re.compile(r"ON\s+CONFLICT\s*\(\s*(\w+)\s*\)\s*DO\s+UPDATE\s+SET\s+(\w+)\s*=\s*excluded\.(\w+)", re.I)
_AUTOINCR_RE = re.compile(r"\bAUTOINCREMENT\b", re.I)
_SQLITE_MASTER_RE = re.compile(r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'(\w+)'", re.I)
_SQLITE_SEQ_RE = re.compile(r"DELETE\s+FROM\s+sqlite_sequence\s+WHERE\s+name\s*=\s*'(\w+)'", re.I)
_CREATE_INDEX_RE = re.compile(r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\(([^)]+)\)", re.I)


def _qmark_to_pct(sql):
    """把 SQL 中未出现在字符串字面量内的 ? 占位符转为 %s；同时处理 date('now', ?) 参数形态。"""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        m = _DATE_NOW_RE.match(sql, i)
        if m:
            out.append("DATE_SUB(CURDATE(), INTERVAL %s DAY)")
            i = m.end()
            continue
        c = sql[i]
        if c == "?":
            out.append("%s")
            i += 1
            continue
        if c == "'":
            out.append(c)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _translate_mysql(sql, params):
    """SQLite SQL -> MySQL SQL；返回 (sql, params)。"""
    sql = _OR_IGNORE_RE.sub("INSERT IGNORE INTO", sql)
    sql = _DATETIME_NOW_RE.sub("NOW()", sql)
    sql = _AUTOINCR_RE.sub("AUTO_INCREMENT", sql)
    sql = _SQLITE_MASTER_RE.sub(
        r"SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = '\1'", sql)
    sql = _SQLITE_SEQ_RE.sub(r"ALTER TABLE \1 AUTO_INCREMENT = 1", sql)

    def fix_conflict(m):
        return f"ON DUPLICATE KEY UPDATE {m.group(2)} = VALUES({m.group(3)})"

    sql = _CONFLICT_RE.sub(fix_conflict, sql)

    # date('now', ?) 的参数形如 '-7 days'，需转为纯数字以匹配 INTERVAL %s DAY
    sql = _qmark_to_pct(sql)
    new_params = list(params or ())

    def fix_interval_param(p):
        if isinstance(p, str):
            m = re.match(r"^-?(\d+)\s+days?$", p.strip())
            if m:
                return int(m.group(1))
        return p

    new_params = [fix_interval_param(p) for p in new_params]
    return sql, new_params


# --------------------------------------------------------------------------
# 游标包装
# --------------------------------------------------------------------------
class Cursor:
    def __init__(self, raw_cur, keys=None):
        self._cur = raw_cur
        self._keys = keys or ([d[0] for d in (raw_cur.description or [])] if raw_cur.description else [])

    def fetchone(self):
        r = self._cur.fetchone()
        if r is None:
            return None
        if isinstance(r, dict):
            return CompatRow(r.keys(), r.values())
        return CompatRow(self._keys, r)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return [CompatRow(r.keys(), r.values()) for r in rows]
        return [CompatRow(self._keys, r) for r in rows]

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", -1)

    @property
    def description(self):
        return self._cur.description

    def __iter__(self):
        return iter(self.fetchall())


# --------------------------------------------------------------------------
# 统一连接
# --------------------------------------------------------------------------
class Conn:
    """统一连接对象：SQLite 直连，MySQL 经 pymysql。"""

    def __init__(self):
        self.engine, self.db_cfg = load_db_config()
        if self.engine == "mysql":
            import pymysql
            self._conn = pymysql.connect(**mysql_kwargs(self.db_cfg))
            self._conn._shoppinglog_engine = "mysql"  # 内部标记
        else:
            db_path = self.db_cfg.get("path") or (ROOT / "data" / "shopping.db")
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=None):
        if self.engine == "mysql":
            # CREATE INDEX IF NOT EXISTS: MySQL 不支持，先查存在性
            m = _CREATE_INDEX_RE.match(sql.strip())
            if m:
                unique, idx_name, table, cols = m.groups()
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
                    (table, idx_name),
                )
                exists = cur.fetchone()[0] > 0
                cur.close()
                if exists:
                    return Cursor(self._noop_cursor())
                stmt = f"CREATE {unique or ''}INDEX {idx_name} ON {table} ({cols})"
                cur = self._conn.cursor()
                cur.execute(stmt)
                self._conn.commit()
                return Cursor(cur)
            # PRAGMA table_info(T) -> information_schema
            pm = re.fullmatch(r"\s*PRAGMA\s+table_info\((\w+)\)\s*", sql)
            if pm:
                table = pm.group(1)
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT ordinal_position - 1 AS cid, column_name AS name, "
                    "data_type AS type, CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
                    "column_default AS dflt_value, 0 AS pk "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                c = Cursor(cur)
                self._conn.commit()
                return c
            sql2, params2 = _translate_mysql(sql, params)
            cur = self._conn.cursor()
            cur.execute(sql2, params2)
            return Cursor(cur)
        cur = self._conn.execute(sql, params or ())
        return Cursor(cur)

    def _noop_cursor(self):
        cur = self._conn.cursor()
        cur.description = [("_noop",)]
        cur.fetchone = lambda: None
        cur.fetchall = lambda: []
        cur.lastrowid = None
        cur.rowcount = 0
        return cur

    def executescript(self, sql):
        if self.engine == "mysql":
            for stmt in re.split(r";", sql):
                stmt = stmt.strip()
                if stmt:
                    self.execute(stmt)
            self.commit()
            return
        self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def connect():
    return Conn()


if __name__ == "__main__":
    engine, cfg = load_db_config()
    print(f"engine={engine}")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))

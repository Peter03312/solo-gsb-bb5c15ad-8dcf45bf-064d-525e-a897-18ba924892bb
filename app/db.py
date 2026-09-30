"""SQLite 访问层。

设计要点（对应"并发更正与定版不得拼接不同提交状态"）：
- 全进程共享一个连接，所有请求处理都在同一把 RLock 下执行，
  写事务使用 BEGIN IMMEDIATE，读事务使用 BEGIN（ deferred ）。
- 因此"读取当前版本 -> 校验 -> 写入"永远是一个完整事务，
  定版的"计算声明 -> 落库"与原料更正的"校验期望版本 -> 追加新版本"
  互相串行，不会把两个不同提交状态拼接在一起。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "app.db"))

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingredient_versions (
    ingredient_id TEXT NOT NULL,
    version       INTEGER NOT NULL,
    allergens     TEXT NOT NULL,          -- JSON 数组
    created_at    TEXT NOT NULL,
    PRIMARY KEY (ingredient_id, version)
);

CREATE TABLE IF NOT EXISTS batches (
    id         TEXT PRIMARY KEY,
    product    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',   -- draft | finalized
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_sources (
    batch_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    source_type TEXT NOT NULL,              -- ingredient | batch
    source_id   TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS finalizations (
    batch_id        TEXT PRIMARY KEY,
    allergens       TEXT NOT NULL,          -- JSON 数组，定版时的声明集合
    source_versions TEXT NOT NULL,          -- JSON 对象 {原料: 版本}
    paths           TEXT NOT NULL,          -- JSON 对象 {过敏原: [路径]}
    finalized_at    TEXT NOT NULL
);
"""


def get_conn():
    """惰性创建并初始化共享连接（含建表，即服务启动即完成初始化）。"""
    global _conn
    if _conn is None:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx(immediate: bool = True):
    """在全局锁内开启一个事务；immediate=True 用于写（BEGIN IMMEDIATE）。"""
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

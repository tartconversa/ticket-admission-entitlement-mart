"""SQLite 事实仓存储。

两类表：
1. raw_*：原始交接记录，只追加，永不更新。同目录重复 ingest 时按
   (source, line) 幂等跳过。
2. dim_* / fact_*：每接收一批资料后整体重建的派生事实，因此晚到、更正、
   批次补正都会被重新判定，而不是覆盖旧结论。
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    source      TEXT NOT NULL,
    line        INTEGER NOT NULL,
    arrival     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (source, line)
);

CREATE TABLE IF NOT EXISTS ingest_errors (
    source      TEXT NOT NULL,
    line        INTEGER NOT NULL,
    reason      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_ticket (
    ticket              TEXT PRIMARY KEY,
    ticket_group        TEXT NOT NULL,
    role                TEXT NOT NULL,
    site                TEXT NOT NULL,
    slot                TEXT NOT NULL,
    coupled             INTEGER NOT NULL,
    purchaser           TEXT NOT NULL,
    arrival             INTEGER NOT NULL,
    lineage_root        TEXT NOT NULL,
    root_arrival        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_ticket_issue (
    previous            TEXT NOT NULL,
    reissued            TEXT NOT NULL,
    transport_reference TEXT NOT NULL,
    arrival             INTEGER NOT NULL
);

-- 同一核验凭据以最晚到达的签字声明为准，原件全部保留在 raw 层。
CREATE TABLE IF NOT EXISTS fact_receipt (
    receipt         TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    stall           TEXT NOT NULL,
    occurrence      TEXT NOT NULL,
    ticket          TEXT NOT NULL,
    site            TEXT NOT NULL,
    slot            TEXT NOT NULL,
    link            TEXT,
    arrival         INTEGER NOT NULL
);

-- 凭据声明流水：中心/亭子双发、拒绝尝试、作废声明全部保留，可回放全过程。
CREATE TABLE IF NOT EXISTS fact_receipt_statement (
    receipt         TEXT NOT NULL,
    state           TEXT NOT NULL,
    stall           TEXT NOT NULL,
    occurrence      TEXT NOT NULL,
    ticket          TEXT NOT NULL,
    site            TEXT NOT NULL,
    slot            TEXT NOT NULL,
    link            TEXT,
    arrival         INTEGER NOT NULL,
    superseded      INTEGER NOT NULL
);

-- 到场经历：一次凭据一条；reentry 是独立经历但不产生消费。
CREATE TABLE IF NOT EXISTS fact_attendance (
    receipt         TEXT PRIMARY KEY,
    ticket          TEXT NOT NULL,
    lineage_root    TEXT NOT NULL,
    site            TEXT NOT NULL,
    slot            TEXT NOT NULL,
    occurrence      TEXT NOT NULL,
    state           TEXT NOT NULL,
    linked_receipt  TEXT,
    consumes        INTEGER NOT NULL
);

-- 消费资格：一个原始券号资格最多一条，reentry/补发都接回同一消费。
CREATE TABLE IF NOT EXISTS fact_consumption (
    lineage_root    TEXT NOT NULL,
    site            TEXT NOT NULL,
    slot            TEXT NOT NULL,
    role            TEXT NOT NULL,
    ticket_group    TEXT NOT NULL,
    entry_receipt   TEXT,
    entry_time      TEXT,
    consumed        INTEGER NOT NULL,
    voided_receipt  TEXT,
    PRIMARY KEY (lineage_root, site, slot)
);

-- 每个 (stall, site, slot) 只承认最高流水号声明；场次是否齐备要看全部亭。
CREATE TABLE IF NOT EXISTS fact_batch_boundary (
    stall       TEXT NOT NULL,
    site        TEXT NOT NULL,
    slot        TEXT NOT NULL,
    serial      INTEGER NOT NULL,
    state       TEXT NOT NULL,
    arrival     INTEGER NOT NULL,
    PRIMARY KEY (stall, site, slot)
);

CREATE TABLE IF NOT EXISTS fact_slot_audit (
    site        TEXT NOT NULL,
    slot        TEXT NOT NULL,
    complete    INTEGER NOT NULL,
    stalls      TEXT NOT NULL,
    PRIMARY KEY (site, slot)
);

-- 名额池：同键晚到声明覆盖旧声明（units 是快照值）。
CREATE TABLE IF NOT EXISTS fact_quota (
    site        TEXT NOT NULL,
    slot        TEXT NOT NULL,
    role        TEXT NOT NULL,
    units       INTEGER NOT NULL,
    arrival     INTEGER NOT NULL,
    PRIMARY KEY (site, slot, role)
);

CREATE TABLE IF NOT EXISTS fact_exchange (
    exchange            TEXT PRIMARY KEY,
    ticket_group        TEXT NOT NULL,
    old_slot            TEXT NOT NULL,
    new_slot            TEXT NOT NULL,
    tickets             TEXT NOT NULL,
    requested           TEXT NOT NULL,
    last_event          TEXT NOT NULL,
    last_arrival        INTEGER NOT NULL,
    companion_approval  TEXT,
    decision            TEXT NOT NULL,
    held_units          TEXT NOT NULL,
    affected_members    TEXT NOT NULL,
    quota_to_release    TEXT NOT NULL,
    taken_over          INTEGER NOT NULL,
    note                TEXT NOT NULL
);

-- 场馆按确认日送来的结算声明，原样保留用于对账。
CREATE TABLE IF NOT EXISTS fact_venue_statement (
    settlement  TEXT NOT NULL,
    arrival     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    site        TEXT NOT NULL,
    receipt     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    cents       INTEGER NOT NULL
);

-- 事实仓自己计算的应付结算（含更正差额），按开放期登记。
CREATE TABLE IF NOT EXISTS fact_settlement_line (
    period_id       TEXT NOT NULL,
    site            TEXT NOT NULL,
    receipt         TEXT NOT NULL,
    lineage_root    TEXT NOT NULL,
    role            TEXT NOT NULL,
    ticket_group    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    cents           INTEGER NOT NULL,
    prior_settlement TEXT,
    prior_receipt   TEXT,
    event_time      TEXT NOT NULL,
    confirmed_day   TEXT
);

CREATE TABLE IF NOT EXISTS fact_reconciliation (
    period_id       TEXT NOT NULL,
    site            TEXT NOT NULL,
    receipt         TEXT NOT NULL,
    expected_cents  INTEGER NOT NULL,
    venue_cents     INTEGER,
    status          TEXT NOT NULL,
    note            TEXT NOT NULL
);
"""

DERIVED_TABLES = [
    "dim_ticket",
    "dim_ticket_issue",
    "fact_receipt",
    "fact_receipt_statement",
    "fact_attendance",
    "fact_consumption",
    "fact_batch_boundary",
    "fact_slot_audit",
    "fact_quota",
    "fact_exchange",
    "fact_venue_statement",
    "fact_settlement_line",
    "fact_reconciliation",
]


def connect(db_path):
    """打开（或新建）事实仓数据库。":memory:" 用于测试。"""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def reset_derived(conn):
    """清空派生表，准备按当前掌握的全部资料重建。"""
    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")


def append_raw(conn, source, line, arrival, payload_json):
    """追加原始记录；同 (source,line) 重复导入时幂等跳过。"""
    conn.execute(
        "INSERT OR IGNORE INTO raw_records(source, line, arrival, payload) VALUES (?,?,?,?)",
        (source, line, arrival, payload_json),
    )


def replace_errors(conn, errors):
    conn.execute("DELETE FROM ingest_errors")
    conn.executemany(
        "INSERT INTO ingest_errors(source, line, reason) VALUES (:source, :line, :reason)",
        errors,
    )


def max_arrival(conn):
    row = conn.execute("SELECT COALESCE(MAX(arrival), 0) FROM raw_records").fetchone()
    return row[0]

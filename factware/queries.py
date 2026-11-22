"""对外查询视图：客服核实换票、场馆核对结算、资料齐备性检查。"""
from collections import defaultdict


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def ticket_status(conn, ticket=None):
    """单张券或全部券的资格与到场情况（按券号+角色核对，不串用同伴记录）。"""
    sql = (
        "SELECT t.ticket, t.ticket_group, t.role, t.site, t.slot, t.coupled, t.purchaser,"
        " t.lineage_root, c.consumed, c.entry_receipt, c.entry_time,"
        " (SELECT GROUP_CONCAT(a.receipt, ';') FROM fact_attendance a"
        "  WHERE a.lineage_root = t.lineage_root) AS attendance_receipts"
        " FROM dim_ticket t LEFT JOIN fact_consumption c"
        " ON c.lineage_root = t.lineage_root AND c.site = t.site AND c.slot = t.slot"
    )
    params = ()
    if ticket:
        sql += " WHERE t.ticket = ?"
        params = (ticket,)
    sql += " ORDER BY t.ticket_group, t.ticket"
    rows = _rows(conn, sql, params)
    for row in rows:
        row["coupled"] = "yes" if row["coupled"] else "no"
        row["consumed"] = "yes" if row["consumed"] else "no"
    return rows


def attendance_experiences(conn):
    """全部到场经历：再入场保留为独立经历并标明是否消耗资格。"""
    return _rows(
        conn,
        "SELECT receipt, ticket, lineage_root, site, slot, occurrence, state,"
        " linked_receipt, consumes FROM fact_attendance ORDER BY occurrence",
    )


def receipt_statements(conn, receipt=None):
    """凭据声明流水：双发、拒绝、作废与最新状态均可回放。"""
    sql = (
        "SELECT receipt, state, stall, occurrence, ticket, site, slot, link, arrival,"
        " superseded FROM fact_receipt_statement"
    )
    params = ()
    if receipt:
        sql += " WHERE receipt = ?"
        params = (receipt,)
    return _rows(conn, sql + " ORDER BY arrival, receipt", params)


def slot_audit_status(conn):
    """场次核验齐备性；未齐场次下的未入场只能按待查处理。"""
    return _rows(
        conn,
        "SELECT site, slot, complete, stalls FROM fact_slot_audit ORDER BY site, slot",
    )


def quota_snapshot(conn):
    """名额池快照及当前被换票占住、冻结的数量。"""
    quotas = _rows(conn, "SELECT site, slot, role, units, arrival FROM fact_quota")
    held = defaultdict(int)
    frozen = defaultdict(int)
    for ex in _rows(conn, "SELECT decision, held_units, new_slot FROM fact_exchange"):
        import json
        for role, amount in json.loads(ex["held_units"]).items():
            if ex["decision"] == "frozen":
                frozen[(ex["new_slot"], role)] += amount
            else:
                held[(ex["new_slot"], role)] += amount
    for row in quotas:
        key = (row["slot"], row["role"])
        row["held"] = held[key]
        row["frozen"] = frozen[key]
        row["available_to_promise"] = row["units"] - row["held"] - row["frozen"]
    return quotas


def exchange_decisions(conn, exchange=None):
    """换票决定全量视图，含受影响同行成员、待释放名额与接管情况。"""
    sql = "SELECT * FROM fact_exchange"
    params = ()
    if exchange:
        sql += " WHERE exchange = ?"
        params = (exchange,)
    return _rows(conn, sql + " ORDER BY exchange", params)


def settlement_lines(conn):
    """事实仓结算行：计费、反向差额及其指回的旧票/原结算行。"""
    return _rows(
        conn,
        "SELECT period_id, confirmed_day, site, receipt, lineage_root, role, ticket_group,"
        " kind, cents, prior_settlement, prior_receipt, event_time"
        " FROM fact_settlement_line ORDER BY event_time, receipt",
    )


def reconciliation(conn):
    """场馆对账：matched / difference / 待场馆确认。"""
    return _rows(
        conn,
        "SELECT site, receipt, expected_cents, venue_cents, status, note"
        " FROM fact_reconciliation ORDER BY site, receipt",
    )


def venue_statements(conn):
    """场馆已出具的凭证（更正声明保留，不删除）。"""
    return _rows(
        conn,
        "SELECT settlement, arrival, day, site, receipt, kind, cents"
        " FROM fact_venue_statement ORDER BY arrival, settlement",
    )


def ingest_issues(conn):
    return _rows(conn, "SELECT source, line, reason FROM ingest_errors ORDER BY source, line")

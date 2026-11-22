"""测试辅助：用记录构造器按到达批次喂给内存事实仓。"""
import unittest

from factware.store import connect
from factware.engine import ingest

FILES = [
    "entitlements.csv", "scans.csv", "batches.csv", "availability.csv",
    "exchanges.csv", "rates.csv", "settlement.csv", "ticket_issues.csv",
]


def entitlement(arrival, ticket, group, role, site, slot, coupled="no", purchaser=None):
    return {
        "arrival": str(arrival), "ticket": ticket, "group": group, "role": role,
        "site": site, "slot": slot, "coupled": coupled, "purchaser": purchaser or f"H-{group}",
    }


def scan(arrival, stall, receipt, occurrence, ticket, site, slot, state, link=""):
    return {
        "arrival": str(arrival), "stall": stall, "receipt": receipt,
        "occurrence": occurrence, "ticket": ticket, "site": site, "slot": slot,
        "state": state, "link": link,
    }


def batch(arrival, stall, site, slot, serial, state):
    return {
        "arrival": str(arrival), "stall": stall, "site": site, "slot": slot,
        "serial": str(serial), "state": state,
    }


def availability(arrival, site, slot, role, units):
    return {
        "arrival": str(arrival), "site": site, "slot": slot, "role": role,
        "units": str(units),
    }


def exchange(arrival, exchange_id, requested, group, old_slot, new_slot, tickets,
             state, approval=""):
    return {
        "arrival": str(arrival), "exchange": exchange_id, "requested": requested,
        "group": group, "old_slot": old_slot, "new_slot": new_slot,
        "tickets": ";".join(tickets), "state": state, "companion_approval": approval,
    }


def rate(site, role, cents):
    return {"site": site, "role": role, "cents": str(cents)}


def statement(arrival, settlement, day, site, receipt, kind, cents):
    return {
        "arrival": str(arrival), "settlement": settlement, "day": day,
        "site": site, "receipt": receipt, "kind": kind, "cents": str(cents),
    }


def issue(arrival, previous, reissued, ref):
    return {
        "arrival": str(arrival), "previous": previous, "reissued": reissued,
        "transport_reference": ref,
    }


class WarehouseCase(unittest.TestCase):
    """每个用例从零开始，按批次喂资料，每批都可以断言中间状态。"""

    def setUp(self):
        self.conn = connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def feed(self, **rows_by_file):
        sources = {name: list(rows_by_file.get(name, [])) for name in FILES}
        errors = ingest(self.conn, sources)
        return errors

    def feed_batches(self, *batches_of_sources):
        """分批喂入：每个参数是 {文件名: [记录]}，模拟陆续到达。

        真实交接目录每批都携带截至当时的全部已知记录，因此这里自动把前序
        批次合并进后续批次（raw 层按 source+line 幂等，不会重复计数）。
        """
        all_errors = []
        cumulative = {name: [] for name in FILES}
        for part in batches_of_sources:
            for name in FILES:
                cumulative[name] = cumulative[name] + list(part.get(name, []))
            sources = {name: list(rows) for name, rows in cumulative.items()}
            all_errors = ingest(self.conn, sources)
        return all_errors

    def fact(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.fact(sql, params)
        self.assertEqual(len(rows), 1, f"期望单行，实际 {rows}")
        return rows[0]

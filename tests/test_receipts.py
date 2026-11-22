"""凭据与到场事实：双发去重、拒绝/作废区分、再入场不重复消耗、票号谱系。"""
from factware_case import (
    WarehouseCase, entitlement, scan, issue,
)


class ReceiptLifecycleTest(WarehouseCase):
    def _base_entitlements(self):
        return [
            entitlement(1, "T1", "G1", "adult", "PARK", "S1", coupled="yes"),
            entitlement(1, "T2", "G1", "child", "PARK", "S1", coupled="yes"),
            entitlement(1, "T3", "G2", "solo", "AQUA", "S1"),
        ]

    def test_duplicate_forwarding_keeps_one_receipt_but_full_trail(self):
        self.feed(**{
            "entitlements.csv": self._base_entitlements(),
            "scans.csv": [
                scan(1, "ENTRY-A", "X11", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
                scan(1, "HUB", "X11", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
            ],
        })
        # 最新凭据只有一条，声明流水保留双发两条。
        self.assertEqual(len(self.fact("SELECT * FROM fact_receipt")), 1)
        statements = self.fact("SELECT * FROM fact_receipt_statement WHERE receipt='X11'")
        self.assertEqual(len(statements), 2)
        self.assertEqual(sum(s["superseded"] for s in statements), 1)
        # 只产生一次消费，绝不按扫描次数结算。
        lines = self.fact("SELECT * FROM fact_consumption WHERE lineage_root='T1'")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["entry_receipt"], "X11")

    def test_denied_attempt_is_not_attendance_or_consumption(self):
        self.feed(**{
            "entitlements.csv": self._base_entitlements(),
            "scans.csv": [
                scan(2, "ENTRY-B", "X21", "2026-10-25T09:23:00Z", "T2", "PARK", "S1", "denied"),
            ],
        })
        self.assertEqual(self.fact("SELECT * FROM fact_attendance"), [])
        self.assertEqual(
            self.one("SELECT state FROM fact_receipt WHERE receipt='X21'")["state"], "denied"
        )
        # denied 不建立任何消费资格（没有入场痕迹，也没有作废痕迹）。
        rows = self.fact("SELECT * FROM fact_consumption WHERE lineage_root='T2'")
        self.assertEqual(rows, [])

    def test_void_supersedes_entered_and_restores_qualification(self):
        self.feed(**{
            "entitlements.csv": self._base_entitlements(),
            "scans.csv": [
                scan(4, "ENTRY-B", "X14", "2026-10-25T09:10:00Z", "T2", "PARK", "S1", "entered"),
                scan(6, "ENTRY-B", "X14", "2026-10-25T09:10:00Z", "T2", "PARK", "S1", "void"),
            ],
        })
        receipt = self.one("SELECT * FROM fact_receipt WHERE receipt='X14'")
        self.assertEqual(receipt["state"], "void")
        self.assertEqual(self.fact("SELECT * FROM fact_attendance"), [])
        # 资格恢复为未使用，但作废痕迹保留，供结算冲回原计费。
        consumption = self.one(
            "SELECT * FROM fact_consumption WHERE lineage_root='T2'"
        )
        self.assertEqual(consumption["consumed"], 0)
        self.assertEqual(consumption["voided_receipt"], "X14")
        self.assertEqual(consumption["entry_receipt"], "X14")

    def test_reentry_is_separate_attendance_but_does_not_consume_again(self):
        self.feed(**{
            "entitlements.csv": self._base_entitlements() + [
                entitlement(3, "T1R", "G1", "adult", "PARK", "S1", coupled="yes"),
            ],
            "ticket_issues.csv": [issue(3, "T1", "T1R", "KEY-T1")],
            "scans.csv": [
                scan(1, "ENTRY-A", "X11", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
                scan(3, "ENTRY-A", "X15", "2026-10-25T09:31:00Z", "T1R", "PARK", "S1",
                     "reentry", link="X11"),
            ],
        })
        attendance = self.fact("SELECT * FROM fact_attendance ORDER BY occurrence")
        self.assertEqual([a["receipt"] for a in attendance], ["X11", "X15"])
        self.assertEqual([a["consumes"] for a in attendance], [1, 0])
        # 两条经历接回同一消费资格。
        self.assertEqual({a["lineage_root"] for a in attendance}, {"T1"})
        consumption = self.one(
            "SELECT * FROM fact_consumption WHERE lineage_root='T1' AND site='PARK' AND slot='S1'"
        )
        self.assertEqual(consumption["entry_receipt"], "X11")
        self.assertEqual(consumption["consumed"], 1)

    def test_unknown_ticket_scan_is_quarantined_not_attached_to_companion(self):
        # 借同伴的记录不能补自己的限制：陌生券号扫描进隔离表，不产生到场。
        errors = self.feed(**{
            "entitlements.csv": self._base_entitlements(),
            "scans.csv": [
                scan(1, "ENTRY-A", "X99", "2026-10-25T09:05:00Z", "T999", "PARK", "S1", "entered"),
            ],
        })
        self.assertTrue(errors)
        self.assertEqual(self.fact("SELECT * FROM fact_attendance"), [])
        issues = self.fact("SELECT * FROM ingest_errors")
        self.assertTrue(any("T999" in row["reason"] for row in issues))

    def test_reissue_attaches_later_entry_to_original_qualification(self):
        # 网关明确换发：补发券的入园接回原消费资格，两个不同入场仍然分开。
        self.feed(**{
            "entitlements.csv": [
                entitlement(1, "T1", "G1", "adult", "PARK", "S1"),
                entitlement(3, "T1R", "G1", "adult", "PARK", "S1"),
            ],
            "ticket_issues.csv": [issue(3, "T1", "T1R", "KEY-T1")],
            "scans.csv": [
                scan(1, "ENTRY-A", "X11", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
                scan(5, "ENTRY-A", "X19", "2026-10-25T10:00:00Z", "T1R", "PARK", "S1", "entered"),
            ],
        })
        roots = {
            row["ticket"]: row["lineage_root"]
            for row in self.fact("SELECT ticket, lineage_root FROM dim_ticket")
        }
        self.assertEqual(roots, {"T1": "T1", "T1R": "T1"})
        # 第二次 entered 接回同一资格，不产生第二条消费行。
        consumption = self.fact("SELECT * FROM fact_consumption")
        self.assertEqual(len(consumption), 1)
        self.assertEqual(
            len(self.fact("SELECT * FROM fact_attendance WHERE state='entered'")), 2
        )

"""场馆结算：按资格而非扫描次数、儿童同行条件、跨期差额与对账。"""
from factware_case import (
    WarehouseCase, entitlement, scan, batch, rate, statement, issue,
)


def rates():
    return [rate("PARK", "adult", 2500), rate("PARK", "child", 1200)]


class SettlementTest(WarehouseCase):
    def test_one_qualification_settled_once_despite_reentry(self):
        self.feed(**{
            "entitlements.csv": [
                entitlement(1, "T1", "G1", "adult", "PARK", "S1"),
                entitlement(3, "T1R", "G1", "adult", "PARK", "S1"),
            ],
            "ticket_issues.csv": [issue(3, "T1", "T1R", "KEY-T1")],
            "scans.csv": [
                scan(1, "ENTRY-A", "X11", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
                scan(3, "ENTRY-A", "X15", "2026-10-25T09:31:00Z", "T1R", "PARK", "S1",
                     "reentry", link="X11"),
            ],
            "rates.csv": rates(),
            "settlement.csv": [
                statement(3, "S900", "2026-10-25", "PARK", "X11", "accepted", 2500),
            ],
        })
        lines = self.fact("SELECT * FROM fact_settlement_line")
        # 只有一条计费行，reentry 不额外付钱。
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["receipt"], "X11")
        self.assertEqual(lines[0]["cents"], 2500)
        recon = self.one("SELECT * FROM fact_reconciliation WHERE receipt='X11'")
        self.assertEqual(recon["status"], "matched")

    def test_child_not_payable_until_companion_fact_proven(self):
        # 儿童先到、成人晚到且超出 30 分钟窗口：同行事实不成立，不可结算。
        self.feed(**{
            "entitlements.csv": [
                entitlement(1, "T1", "G1", "adult", "PARK", "S1", coupled="yes"),
                entitlement(1, "T2", "G1", "child", "PARK", "S1", coupled="yes"),
            ],
            "scans.csv": [
                scan(1, "ENTRY-A", "X20", "2026-10-25T09:00:00Z", "T2", "PARK", "S1", "entered"),
                scan(1, "ENTRY-A", "X21", "2026-10-25T09:45:00Z", "T1", "PARK", "S1", "entered"),
            ],
            "rates.csv": rates(),
        })
        child_lines = self.fact("SELECT * FROM fact_settlement_line WHERE lineage_root='T2'")
        self.assertEqual(child_lines, [])
        # 成人本身仍可结算。
        adult_line = self.one("SELECT * FROM fact_settlement_line WHERE lineage_root='T1'")
        self.assertEqual(adult_line["cents"], 2500)

    def test_child_payable_when_adult_within_window(self):
        self.feed(**{
            "entitlements.csv": [
                entitlement(1, "T1", "G1", "adult", "PARK", "S1", coupled="yes"),
                entitlement(1, "T2", "G1", "child", "PARK", "S1", coupled="yes"),
            ],
            "scans.csv": [
                scan(1, "ENTRY-A", "X21", "2026-10-25T09:20:00Z", "T1", "PARK", "S1", "entered"),
                scan(1, "ENTRY-B", "X22", "2026-10-25T09:26:00Z", "T2", "PARK", "S1", "entered"),
            ],
            "rates.csv": rates(),
        })
        child_line = self.one("SELECT * FROM fact_settlement_line WHERE lineage_root='T2'")
        self.assertEqual(child_line["cents"], 1200)

    def test_void_creates_reversal_in_next_open_period_pointing_back(self):
        # 跨结算日的作废：反向差额进入下一开放期，并指回旧票与原结算行；
        # 场馆原有凭证继续保留。
        self.feed_batches(
            {
                "entitlements.csv": [
                    entitlement(1, "T1", "G1", "adult", "PARK", "S1", coupled="yes"),
                    entitlement(1, "T2", "G1", "child", "PARK", "S1", coupled="yes"),
                ],
                "scans.csv": [
                    scan(4, "ENTRY-A", "X10", "2026-10-25T09:05:00Z", "T1", "PARK", "S1", "entered"),
                    scan(4, "ENTRY-B", "X14", "2026-10-25T09:10:00Z", "T2", "PARK", "S1", "entered"),
                ],
                "rates.csv": rates(),
            },
            {
                "settlement.csv": [
                    statement(5, "S901", "2026-10-26", "PARK", "X14",
                              "correction_received", 1200),
                ],
            },
            {
                "scans.csv": [
                    scan(6, "ENTRY-B", "X14", "2026-10-25T09:10:00Z", "T2", "PARK", "S1", "void"),
                ],
                "settlement.csv": [
                    statement(7, "S902", "2026-10-26", "PARK", "X14",
                              "correction_received", -1200),
                ],
            },
        )
        reversal = self.one("SELECT * FROM fact_settlement_line WHERE kind='reversal'")
        self.assertEqual(reversal["cents"], -1200)
        self.assertEqual(reversal["period_id"], "S902")  # 下一开放期，不回写 S901
        self.assertEqual(reversal["prior_settlement"], "S901")
        self.assertEqual(reversal["prior_receipt"], "X14")
        self.assertEqual(reversal["lineage_root"], "T2")
        # 原计费行与反向差额成对，净额为 0。
        payable = self.one("SELECT * FROM fact_settlement_line WHERE kind='payable' AND receipt='X14'")
        self.assertEqual(payable["cents"], 1200)
        # 场馆两张凭证原样保留。
        venue = self.fact("SELECT * FROM fact_venue_statement WHERE receipt='X14'")
        self.assertEqual([v["cents"] for v in venue], [1200, -1200])
        recon = self.one("SELECT * FROM fact_reconciliation WHERE receipt='X14'")
        self.assertEqual(recon["status"], "matched")
        self.assertEqual(recon["venue_cents"], 0)
        self.assertEqual(recon["expected_cents"], 0)

    def test_payable_after_last_confirmation_waits_for_venue(self):
        self.feed_batches(
            {
                "entitlements.csv": [entitlement(1, "T4", "G3", "adult", "PARK", "S2")],
                "scans.csv": [
                    scan(2, "ENTRY-A", "X13", "2026-10-25T09:20:00Z", "T4", "PARK", "S2", "entered"),
                ],
                "rates.csv": rates(),
                "settlement.csv": [
                    statement(3, "S900", "2026-10-25", "PARK", "X13", "accepted", 2500),
                ],
            },
            {
                # 最后一个确认期之后才出现的新消费。
                "entitlements.csv": [entitlement(1, "T5", "G3", "child", "PARK", "S2", coupled="yes")],
                "scans.csv": [
                    scan(5, "ENTRY-B", "X17", "2026-10-25T09:26:00Z", "T5", "PARK", "S2", "entered"),
                ],
            },
        )
        line = self.one("SELECT * FROM fact_settlement_line WHERE receipt='X17'")
        self.assertTrue(line["period_id"].startswith("PENDING-"))
        self.assertIsNone(line["confirmed_day"])
        recon = self.one("SELECT * FROM fact_reconciliation WHERE receipt='X17'")
        self.assertEqual(recon["status"], "missing_venue_confirmation")

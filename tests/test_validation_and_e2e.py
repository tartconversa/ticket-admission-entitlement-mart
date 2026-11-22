"""输入校验：坏记录进隔离表，不污染事实；幂等重复入库。"""
from factware_case import WarehouseCase, entitlement, scan
from factware.validation import validate
from factware.sources import load_directory
from factware import queries
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1] / "examples"


class ValidationTest(WarehouseCase):
    def test_bad_state_and_bad_time_are_rejected(self):
        normalized, errors = validate({
            "entitlements.csv": [],
            "scans.csv": [
                {
                    "arrival": "1", "stall": "A", "receipt": "X1",
                    "occurrence": "not-a-time", "ticket": "T1", "site": "PARK",
                    "slot": "S1", "state": "entered", "link": "",
                },
                {
                    "arrival": "1", "stall": "A", "receipt": "X2",
                    "occurrence": "2026-10-25T09:00:00Z", "ticket": "T1",
                    "site": "PARK", "slot": "S1", "state": "teleported", "link": "",
                },
            ],
        })
        self.assertEqual(normalized["scans.csv"], [])
        self.assertEqual(len(errors), 2)

    def test_reentry_without_link_is_rejected(self):
        normalized, errors = validate({
            "scans.csv": [
                {
                    "arrival": "1", "stall": "A", "receipt": "X1",
                    "occurrence": "2026-10-25T09:00:00Z", "ticket": "T1",
                    "site": "PARK", "slot": "S1", "state": "reentry", "link": "",
                },
            ],
        })
        self.assertEqual(normalized["scans.csv"], [])
        self.assertTrue(any("link" in e["reason"] for e in errors))

    def test_negative_quota_rejected(self):
        normalized, errors = validate({
            "availability.csv": [
                {"arrival": "1", "site": "PARK", "slot": "S3", "role": "adult", "units": "-1"},
            ],
        })
        self.assertEqual(normalized["availability.csv"], [])
        self.assertTrue(errors)

    def test_ingest_is_idempotent_on_same_directory(self):
        sources, _ = load_directory(ROOT)
        self.feed(**sources)
        first = self.one("SELECT COUNT(*) AS n FROM raw_records")["n"]
        # 再次接收同一目录：raw 层不重复，派生事实重建后结论不变。
        self.feed(**sources)
        second = self.one("SELECT COUNT(*) AS n FROM raw_records")["n"]
        self.assertEqual(first, second)
        exchanges = queries.exchange_decisions(self.conn)
        self.assertEqual({e["exchange"] for e in exchanges}, {"U11", "U12", "U13"})


class EndToEndSampleTest(WarehouseCase):
    """用随项目交付的完整交接资料跑一遍，锁定关键业务结论。"""

    def test_full_handoff_story(self):
        sources, read_errors = load_directory(ROOT)
        self.assertEqual(read_errors, [])
        errors = self.feed(**sources)
        self.assertEqual(errors, [])

        # 双发 X11 只承认一条凭据；X14 被 void；X15 是再入场独立经历。
        receipts = {r["receipt"]: r for r in self.fact("SELECT * FROM fact_receipt")}
        self.assertEqual(receipts["X11"]["state"], "entered")
        self.assertEqual(receipts["X14"]["state"], "void")
        self.assertEqual(receipts["X15"]["state"], "reentry")
        self.assertEqual(
            len(self.fact("SELECT * FROM fact_receipt_statement WHERE receipt='X11'")), 2
        )

        # 补发券 T1R 与 T1 共享消费资格。
        roots = {r["ticket"]: r["lineage_root"] for r in self.fact("SELECT * FROM dim_ticket")}
        self.assertEqual(roots["T1R"], "T1")

        # 换票三笔各自的终态。
        exchanges = {e["exchange"]: e for e in self.fact("SELECT * FROM fact_exchange")}
        self.assertEqual(exchanges["U11"]["decision"], "released")
        self.assertEqual(exchanges["U12"]["decision"], "pending")
        self.assertEqual(exchanges["U13"]["decision"], "blocked")

        # 结算：X11/X13 对齐 S900；X17 到达时 S901 已开放故归入 S901；
        # X14 作废后的反向差额只能进入下一开放期 S902。
        lines = {(l["receipt"], l["kind"]): l for l in queries.settlement_lines(self.conn)}
        self.assertEqual(lines[("X11", "payable")]["period_id"], "S900")
        self.assertEqual(lines[("X13", "payable")]["period_id"], "S900")
        self.assertEqual(lines[("X17", "payable")]["period_id"], "S901")
        self.assertEqual(lines[("X14", "reversal")]["period_id"], "S902")

        recon = {r["receipt"]: r for r in queries.reconciliation(self.conn)}
        self.assertEqual(recon["X11"]["status"], "matched")
        self.assertEqual(recon["X14"]["status"], "matched")
        self.assertEqual(recon["X17"]["status"], "missing_venue_confirmation")

        # 场次审计：补正后两个场次都齐备。
        audit = {(a["site"], a["slot"]): a["complete"]
                 for a in queries.slot_audit_status(self.conn)}
        self.assertEqual(audit, {("PARK", "S1"): 1, ("PARK", "S2"): 1})

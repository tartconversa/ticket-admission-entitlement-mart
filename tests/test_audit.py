"""亭审计边界：缺批待查、齐备才能判未入场、错误 complete 可被补正推翻。"""
from factware_case import WarehouseCase, entitlement, scan, batch


class BatchAuditTest(WarehouseCase):
    def _entitlements(self):
        return [
            entitlement(1, "T1", "G1", "adult", "PARK", "S1", coupled="yes"),
            entitlement(1, "T2", "G1", "child", "PARK", "S1", coupled="yes"),
        ]

    def test_missing_batch_keeps_slot_pending(self):
        # ENTRY-B 是该场馆已知核验亭（在别的场次回传过），但 S1 缺它的边界，
        # 因此 S1 资料未齐，按待查处理。
        self.feed(**{
            "entitlements.csv": self._entitlements(),
            "batches.csv": [
                batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                batch(1, "ENTRY-B", "PARK", "S2", 90, "complete"),
            ],
        })
        audit = self.one("SELECT * FROM fact_slot_audit WHERE site='PARK' AND slot='S1'")
        self.assertEqual(audit["complete"], 0)

    def test_only_complete_audit_allows_not_entered_judgement(self):
        # 两亭都齐备且没有任何有效入场：此时才能判未入场。
        self.feed(**{
            "entitlements.csv": self._entitlements(),
            "batches.csv": [
                batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
            ],
        })
        audit = self.one("SELECT * FROM fact_slot_audit WHERE site='PARK' AND slot='S1'")
        self.assertEqual(audit["complete"], 1)
        self.assertEqual(
            self.fact("SELECT * FROM fact_consumption WHERE lineage_root='T2'"), []
        )

    def test_later_higher_serial_corrects_wrong_complete_to_partial(self):
        # 离线亭先错误声明 complete，随后用更高流水号补正为 partial；
        # 场次齐备性必须被推翻回待查。
        self.feed_batches(
            {
                "entitlements.csv": self._entitlements(),
                "batches.csv": [
                    batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                    batch(1, "ENTRY-B", "PARK", "S1", 92, "complete"),
                ],
            },
            {
                "batches.csv": [batch(5, "ENTRY-B", "PARK", "S1", 93, "partial")],
            },
        )
        boundary = self.one(
            "SELECT * FROM fact_batch_boundary WHERE stall='ENTRY-B' AND site='PARK' AND slot='S1'"
        )
        self.assertEqual(boundary["serial"], 93)
        self.assertEqual(boundary["state"], "partial")
        audit = self.one("SELECT * FROM fact_slot_audit WHERE site='PARK' AND slot='S1'")
        self.assertEqual(audit["complete"], 0)

    def test_late_valid_scan_before_exchange_time_forces_rejudgement(self):
        # 离线亭补到的有效记录早于换票时间：原票已用、新券能否继续要重判。
        # 这里只验证消费事实随晚到记录重建（换票侧在 exchange 测试中覆盖）。
        self.feed_batches(
            {
                "entitlements.csv": self._entitlements(),
                "batches.csv": [batch(1, "ENTRY-A", "PARK", "S1", 91, "complete")],
            },
            {
                "scans.csv": [
                    scan(4, "ENTRY-B", "X14", "2026-10-25T09:08:00Z", "T2",
                         "PARK", "S1", "entered"),
                ],
                "batches.csv": [batch(4, "ENTRY-B", "PARK", "S1", 91, "complete")],
            },
        )
        consumption = self.one(
            "SELECT * FROM fact_consumption WHERE lineage_root='T2'"
        )
        self.assertEqual(consumption["entry_receipt"], "X14")
        self.assertEqual(consumption["consumed"], 1)

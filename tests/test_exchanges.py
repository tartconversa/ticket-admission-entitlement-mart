"""换票决定：整组兑现、陪同批准、名额占用、晚到核验冻结、拒绝待单。"""
from factware_case import (
    WarehouseCase, entitlement, scan, batch, availability, exchange, issue,
)


def coupled_pair(arrival=1, group="G1", slot="S1"):
    return [
        entitlement(arrival, "T1", group, "adult", "PARK", slot, coupled="yes"),
        entitlement(arrival, "T2", group, "child", "PARK", slot, coupled="yes"),
    ]


class ExchangeEligibilityTest(WarehouseCase):
    def test_pending_when_old_slot_audit_incomplete(self):
        self.feed(**{
            "entitlements.csv": [entitlement(1, "T3", "G2", "solo", "AQUA", "S1")],
            "availability.csv": [availability(2, "AQUA", "S3", "solo", 1)],
            "exchanges.csv": [
                exchange(2, "U12", "2026-10-25T09:18:00Z", "G2", "S1", "S3", ["T3"], "requested"),
            ],
        })
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U12'")
        self.assertEqual(row["decision"], "pending")
        # 待查不占名额。
        self.assertEqual(row["held_units"], "{}")

    def test_promised_when_group_intact_and_quota_covers_all_roles(self):
        self.feed(**{
            "entitlements.csv": coupled_pair(),
            "batches.csv": [
                batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
            ],
            "availability.csv": [
                availability(2, "PARK", "S3", "adult", 1),
                availability(2, "PARK", "S3", "child", 1),
            ],
            "exchanges.csv": [
                exchange(2, "U20", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                         ["T1", "T2"], "requested"),
            ],
        })
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U20'")
        self.assertEqual(row["decision"], "promised")
        self.assertIn('"adult": 1', row["held_units"])
        self.assertIn('"child": 1', row["held_units"])

    def test_missing_any_required_unit_blocks_promise(self):
        self.feed(**{
            "entitlements.csv": coupled_pair(),
            "batches.csv": [
                batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
            ],
            "availability.csv": [
                availability(2, "PARK", "S3", "adult", 1),
                availability(2, "PARK", "S3", "child", 0),  # 儿童名额为 0
            ],
            "exchanges.csv": [
                exchange(2, "U20", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                         ["T1", "T2"], "requested"),
            ],
        })
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U20'")
        self.assertEqual(row["decision"], "rejected")
        self.assertIn("名额不足", row["note"])

    def test_partial_ticket_list_is_rejected(self):
        self.feed(**{
            "entitlements.csv": coupled_pair(),
            "availability.csv": [availability(2, "PARK", "S3", "adult", 1)],
            "exchanges.csv": [
                exchange(2, "U20", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                         ["T1"], "requested"),
            ],
        })
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U20'")
        self.assertEqual(row["decision"], "rejected")
        self.assertIn("不能只换组内部分券", row["note"])

    def test_entered_adult_without_approval_is_blocked(self):
        # 成人已核验、儿童未使用：没有场馆陪同批准时不能承诺换场。
        self.feed(**{
            "entitlements.csv": coupled_pair(),
            "scans.csv": [
                scan(2, "ENTRY-A", "X13", "2026-10-25T09:20:00Z", "T1", "PARK", "S1", "entered"),
            ],
            "batches.csv": [
                batch(2, "ENTRY-A", "PARK", "S1", 92, "complete"),
                batch(3, "ENTRY-B", "PARK", "S1", 92, "complete"),
            ],
            "availability.csv": [
                availability(4, "PARK", "S3", "adult", 1),
                availability(4, "PARK", "S3", "child", 1),
            ],
            "exchanges.csv": [
                exchange(4, "U30", "2026-10-25T09:22:00Z", "G1", "S1", "S3",
                         ["T1", "T2"], "requested"),
            ],
        })
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U30'")
        self.assertEqual(row["decision"], "blocked")
        self.assertIn("陪同批准", row["note"])

    def test_entered_adult_with_approval_promises_and_keeps_consumption(self):
        # 场馆陪同批准随换票事件到达后可承诺：成人名额照占，已有消费不抹掉。
        self.feed_batches(
            {
                "entitlements.csv": coupled_pair(),
                "scans.csv": [
                    scan(2, "ENTRY-A", "X13", "2026-10-25T09:20:00Z", "T1", "PARK", "S1", "entered"),
                ],
                "batches.csv": [
                    batch(2, "ENTRY-A", "PARK", "S1", 92, "complete"),
                    batch(3, "ENTRY-B", "PARK", "S1", 92, "complete"),
                ],
                "availability.csv": [
                    availability(4, "PARK", "S3", "adult", 1),
                    availability(4, "PARK", "S3", "child", 1),
                ],
                "exchanges.csv": [
                    exchange(4, "U30", "2026-10-25T09:22:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "requested"),
                ],
            },
            {
                # 新到达批次带来场馆批准号（新事件行，不是改写旧声明）。
                "exchanges.csv": [
                    exchange(5, "U30", "2026-10-25T09:22:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "requested", approval="P29"),
                ],
            },
        )
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U30'")
        self.assertEqual(row["decision"], "promised")
        self.assertEqual(row["companion_approval"], "P29")
        self.assertIn('"adult": 1', row["held_units"])
        consumption = self.one("SELECT * FROM fact_consumption WHERE lineage_root='T1'")
        self.assertEqual(consumption["consumed"], 1)

    def test_late_verification_freezes_claimed_exchange_and_keeps_quota(self):
        # claim 之后晚到的儿童有效入园改变资格：冻结、列影响成员、名额不释放。
        self.feed_batches(
            {
                "entitlements.csv": coupled_pair(),
                "batches.csv": [
                    batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                    batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
                ],
                "availability.csv": [
                    availability(2, "PARK", "S3", "adult", 1),
                    availability(2, "PARK", "S3", "child", 1),
                ],
                "exchanges.csv": [
                    exchange(2, "U11", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "requested", approval="P29"),
                ],
            },
            {
                "exchanges.csv": [
                    exchange(3, "U11", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "claimed", approval="P29"),
                ],
            },
            {
                "scans.csv": [
                    scan(4, "ENTRY-B", "X14", "2026-10-25T09:10:00Z", "T2",
                         "PARK", "S1", "entered"),
                ],
                # 离线亭补齐该场次边界后，晚到的儿童入园才成为可判定事实。
                "batches.csv": [
                    batch(4, "ENTRY-B", "PARK", "S1", 93, "complete"),
                ],
            },
        )
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U11'")
        self.assertEqual(row["decision"], "frozen")
        self.assertIn("T2:child:consumed", row["affected_members"])
        self.assertIn("child:1", row["quota_to_release"])
        # 冻结名额仍被占住，客服不能立刻卖给别人。
        self.assertIn('"child": 1', row["held_units"])
        self.assertEqual(row["taken_over"], 0)

    def test_release_ack_returns_quota_and_records_members(self):
        self.feed_batches(
            {
                "entitlements.csv": coupled_pair() + [
                    entitlement(3, "T1R", "G1", "adult", "PARK", "S1", coupled="yes"),
                ],
                "ticket_issues.csv": [issue(3, "T1", "T1R", "KEY-T1")],
                "batches.csv": [
                    batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                    batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
                ],
                "availability.csv": [
                    availability(2, "PARK", "S3", "adult", 1),
                    availability(2, "PARK", "S3", "child", 1),
                ],
                "exchanges.csv": [
                    exchange(2, "U11", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "requested", approval="P29"),
                ],
            },
            {
                "exchanges.csv": [
                    exchange(6, "U11", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                             ["T1", "T2"], "release_ack", approval="P29"),
                ],
            },
        )
        row = self.one("SELECT * FROM fact_exchange WHERE exchange='U11'")
        self.assertEqual(row["decision"], "released")
        self.assertEqual(row["held_units"], "{}")
        # 补发券不重复占名额：待释放名额是成人 1、儿童 1。
        self.assertEqual(row["quota_to_release"], "adult:1;child:1")
        self.assertIn("T1:adult", row["affected_members"])
        self.assertIn("T2:child", row["affected_members"])

    def test_unpromised_other_family_remains_normal_when_one_rejected(self):
        # 一家被拒不影响另一家的正常承诺。
        self.feed(**{
            "entitlements.csv": coupled_pair("G1") + [
                entitlement(1, "T8", "G8", "adult", "PARK", "S1", coupled="yes"),
                entitlement(1, "T9", "G8", "child", "PARK", "S1", coupled="yes"),
            ],
            "batches.csv": [
                batch(1, "ENTRY-A", "PARK", "S1", 91, "complete"),
                batch(1, "ENTRY-B", "PARK", "S1", 90, "complete"),
            ],
            "availability.csv": [
                availability(2, "PARK", "S3", "adult", 2),
                availability(2, "PARK", "S3", "child", 2),
            ],
            "exchanges.csv": [
                exchange(2, "U40", "2026-10-25T09:12:00Z", "G1", "S1", "S3",
                         ["T1"], "requested"),  # 部分券 -> 拒绝
                exchange(2, "U41", "2026-10-25T09:13:00Z", "G8", "S1", "S3",
                         ["T8", "T9"], "requested"),
            ],
        })
        rejected = self.one("SELECT * FROM fact_exchange WHERE exchange='U40'")
        other = self.one("SELECT * FROM fact_exchange WHERE exchange='U41'")
        self.assertEqual(rejected["decision"], "rejected")
        self.assertEqual(other["decision"], "promised")

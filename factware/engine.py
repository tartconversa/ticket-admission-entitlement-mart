"""事实仓核心引擎。

每处理一个到达批次：原始记录追加进 raw 表，然后基于当前掌握的全部资料
重建派生事实。这样晚到记录、补发关系、批次补正都会触发重新判定，旧结论
以结算差额和换票决定留痕，而不是被悄悄覆盖。
"""
import json
from collections import defaultdict

from .store import reset_derived, append_raw, replace_errors
from .model import build_lineage, coupled_condition_holds
from .timeutil import parse_dt, parse_day, iso
from . import validation

# JSON 落库后需要恢复的日期时间列。
_DT_COLUMNS = {"scans.csv": ["occurrence_dt"], "exchanges.csv": ["requested_dt"]}
_DATE_COLUMNS = {"settlement.csv": ["day_date"]}


# ---------------------------------------------------------------------------
# 入库：原始记录只追加
# ---------------------------------------------------------------------------

def ingest(conn, sources, read_errors=()):
    """校验并追加一个交接目录的原始记录，然后重建全部事实。

    返回当前事实仓里全部待处理问题（读取/校验隔离 + 重建期引用隔离）。
    """
    normalized, errors = validation.validate(sources)
    errors = list(read_errors) + errors
    for filename, rows in normalized.items():
        for row in rows:
            line = row.pop("_line")
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            append_raw(conn, filename, line, int(row["arrival"]), payload)
    # 先落校验期问题；rebuild 会清空并在重建后重新登记，最后把重建期产生的
    # 引用隔离问题一并返回。
    replace_errors(conn, errors)
    rebuild(conn)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT source, line, reason FROM ingest_errors ORDER BY source, line"
        ).fetchall()
    ]


def _load_raw(conn):
    """从只追加的原始层读出当前全部资料（校验过的记录）。"""
    grouped = defaultdict(list)
    rows = conn.execute(
        "SELECT source, payload FROM raw_records ORDER BY arrival, line"
    ).fetchall()
    for row in rows:
        record = json.loads(row["payload"])
        for column in _DT_COLUMNS.get(row["source"], []):
            if column in record:
                record[column] = parse_dt(record[column])
        for column in _DATE_COLUMNS.get(row["source"], []):
            if column in record:
                record[column] = parse_day(record[column])
        grouped[row["source"]].append(record)
    return grouped


# ---------------------------------------------------------------------------
# 重建：票号谱系、凭据、到场、消费
# ---------------------------------------------------------------------------

def rebuild(conn):
    reset_derived(conn)
    data = _load_raw(conn)

    entitlements = data["entitlements.csv"]
    scans = data["scans.csv"]
    batches = data["batches.csv"]
    availability = data["availability.csv"]
    exchanges = data["exchanges.csv"]
    rates_rows = data["rates.csv"]
    statements = data["settlement.csv"]
    issues = data["ticket_issues.csv"]

    lineage, issue_map = build_lineage(entitlements, issues)
    ent_by_ticket = {row["ticket"]: row for row in entitlements}

    _build_ticket_dimensions(conn, entitlements, issues, lineage)
    receipt_by_id = _build_receipts_and_attendance(conn, scans, ent_by_ticket, lineage)
    consumption = _build_consumption(conn, ent_by_ticket, lineage, receipt_by_id)
    slot_audit = _build_batch_audit(conn, batches)
    quota = _build_quota(conn, availability)
    exchange_facts = _build_exchanges(
        conn, exchanges, ent_by_ticket, lineage, receipt_by_id, consumption, slot_audit, quota
    )
    _build_settlement(conn, rates_rows, statements, receipt_by_id, consumption, ent_by_ticket, lineage)


def _build_ticket_dimensions(conn, entitlements, issues, lineage):
    for issue in issues:
        conn.execute(
            "INSERT INTO dim_ticket_issue(previous, reissued, transport_reference, arrival)"
            " VALUES (?,?,?,?)",
            (issue["previous"], issue["reissued"], issue["transport_reference"], issue["arrival"]),
        )
    for ent in entitlements:
        root = lineage[ent["ticket"]]
        root_ent = next((e for e in entitlements if e["ticket"] == root), ent)
        conn.execute(
            "INSERT INTO dim_ticket(ticket, ticket_group, role, site, slot, coupled,"
            " purchaser, arrival, lineage_root, root_arrival) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ent["ticket"], ent["group"], ent["role"], ent["site"], ent["slot"],
                1 if ent["coupled"] == "yes" else 0, ent["purchaser"], ent["arrival"],
                root, root_ent["arrival"],
            ),
        )


def _build_receipts_and_attendance(conn, scans, ent_by_ticket, lineage):
    """同一凭据可能被中心与亭子双发，只承认最晚到达的签字声明。"""
    latest = {}
    for scan in scans:
        receipt = scan["receipt"]
        if receipt not in latest or scan["arrival"] >= latest[receipt]["arrival"]:
            latest[receipt] = scan

    # 声明流水：全部转发记录留痕（含未知票号的隔离记录），标注是否被取代。
    for scan in scans:
        is_latest = latest[scan["receipt"]] is scan
        conn.execute(
            "INSERT INTO fact_receipt_statement(receipt, state, stall, occurrence, ticket,"
            " site, slot, link, arrival, superseded) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (scan["receipt"], scan["state"], scan["stall"], iso(scan["occurrence_dt"]),
             scan["ticket"], scan["site"], scan["slot"], scan.get("link") or None,
             scan["arrival"], 0 if is_latest else 1),
        )

    receipt_by_id = {}
    for receipt, scan in latest.items():
        # 券号资格按券号核对：扫描记录上的票号必须存在对应票权，
        # 借同伴的记录无法补到自己券号下（未知票号进入隔离错误）。
        ent = ent_by_ticket.get(scan["ticket"])
        if ent is None:
            conn.execute(
                "INSERT INTO ingest_errors(source, line, reason) VALUES (?,?,?)",
                ("scans.csv", 0, f"凭据 {receipt} 的券号 {scan['ticket']} 无对应票权，已隔离"),
            )
            continue
        state = scan["state"]
        conn.execute(
            "INSERT INTO fact_receipt(receipt, state, stall, occurrence, ticket, site, slot, link, arrival)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (receipt, state, scan["stall"], iso(scan["occurrence_dt"]), scan["ticket"],
             scan["site"], scan["slot"], scan.get("link") or None, scan["arrival"]),
        )
        receipt_by_id[receipt] = scan

        # 有效到场经历只包括 entered 与 reentry；void 是对旧声明的作废，
        # denied 是拒绝尝试，二者都不是到场。
        if state not in {"entered", "reentry"}:
            continue
        root = lineage[scan["ticket"]]
        conn.execute(
            "INSERT INTO fact_attendance(receipt, ticket, lineage_root, site, slot, occurrence,"
            " state, linked_receipt, consumes) VALUES (?,?,?,?,?,?,?,?,?)",
            (receipt, scan["ticket"], root, scan["site"], scan["slot"],
             iso(scan["occurrence_dt"]), state, scan.get("link") or None,
             1 if state == "entered" else 0),
        )
    return receipt_by_id


def _build_consumption(conn, ent_by_ticket, lineage, receipt_by_id):
    """每个谱系根在一场的消费资格最多消耗一次。

    entered 产生/接回消费；reentry 通过 link 或补发关系接回原资格，保留为
    独立到场但不新增消费；void 后旧凭据不再支撑当前消费（资格恢复未使用），
    但原始入场痕迹保留在 voided_receipt 上，供结算生成“原计费+反向差额”。
    """
    # 最新声明为 void 的凭据，其原始 entered 记录只留在声明流水里，单独取回。
    original_entered = {}
    for row in conn.execute(
        "SELECT * FROM fact_receipt_statement WHERE state='entered' ORDER BY arrival"
    ).fetchall():
        record = dict(row)
        record["occurrence_dt"] = parse_dt(record["occurrence"])
        original_entered.setdefault(row["receipt"], record)

    consumption = {}

    def key(root, site, slot):
        return root, site, slot

    # 先按时间顺序处理原始 entered（含后来被 void 的凭据），建立入场痕迹。
    entered_scans = list(original_entered.values()) + [
        scan for scan in receipt_by_id.values() if scan["state"] == "entered"
    ]
    seen_entered = set()
    for scan in sorted(entered_scans, key=lambda s: (s["occurrence"], s["receipt"])):
        receipt = scan["receipt"]
        if receipt in seen_entered:
            continue
        # 无票权的扫描已在凭据阶段隔离，不能进入任何消费资格。
        if scan["ticket"] not in lineage:
            continue
        seen_entered.add(receipt)
        root = lineage[scan["ticket"]]
        k = key(root, scan["site"], scan["slot"])
        item = consumption.setdefault(
            k, {"entry_receipt": None, "entry_time": None, "consumed": False,
                "reentry_receipts": [], "voided_receipt": None}
        )
        if item["entry_receipt"] is None:
            item["entry_receipt"] = receipt
            item["entry_time"] = scan["occurrence_dt"]
            item["consumed"] = True

    for receipt, scan in receipt_by_id.items():
        state = scan["state"]
        if scan["ticket"] not in lineage:
            continue
        if state not in {"entered", "reentry", "void"}:
            # denied 等拒绝尝试不建立任何消费资格条目。
            continue
        root = lineage[scan["ticket"]]
        k = key(root, scan["site"], scan["slot"])
        item = consumption.setdefault(
            k, {"entry_receipt": None, "entry_time": None, "consumed": False,
                "reentry_receipts": [], "voided_receipt": None}
        )
        if state == "void":
            # 凭据被作废：若它曾是入场凭据，保留痕迹并把资格恢复为未使用。
            if item["entry_receipt"] == receipt:
                item["voided_receipt"] = receipt
                item["consumed"] = False
            continue
        if state == "reentry":
            # 关联凭据（link 指向原入场，或补发券指向原券）把再入接回原消费。
            item["reentry_receipts"].append(receipt)
            continue
        if state != "entered":
            continue
        # 作废后又有新的有效入场：新凭据接管消费资格。
        if item["voided_receipt"] is not None:
            item["entry_receipt"] = receipt
            item["entry_time"] = scan["occurrence_dt"]
            item["consumed"] = True
            item["voided_receipt"] = None

    for (root, site, slot), item in consumption.items():
        root_ent = next(
            (e for ticket, e in ent_by_ticket.items() if lineage[ticket] == root), None
        )
        if root_ent is None:
            continue
        conn.execute(
            "INSERT INTO fact_consumption(lineage_root, site, slot, role, ticket_group,"
            " entry_receipt, entry_time, consumed, voided_receipt)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (root, site, slot, root_ent["role"], root_ent["group"],
             item["entry_receipt"], iso(item["entry_time"]) if item["entry_time"] else None,
             1 if item["consumed"] else 0, item["voided_receipt"]),
        )
    return consumption


# ---------------------------------------------------------------------------
# 重建：亭审计边界与场次齐备性
# ---------------------------------------------------------------------------

def _build_batch_audit(conn, batches):
    # 每个 (亭, 场馆, 场次) 只承认最高流水号声明，晚到的更正可以推翻
    # 早先错误的 complete。
    boundaries = {}
    for row in batches:
        k = (row["stall"], row["site"], row["slot"])
        if k not in boundaries or row["serial"] > boundaries[k]["serial"]:
            boundaries[k] = row
    for row in boundaries.values():
        conn.execute(
            "INSERT INTO fact_batch_boundary(stall, site, slot, serial, state, arrival)"
            " VALUES (?,?,?,?,?,?)",
            (row["stall"], row["site"], row["slot"], row["serial"], row["state"], row["arrival"]),
        )

    slot_audit = {}
    grouped = defaultdict(list)
    for (stall, site, slot), row in boundaries.items():
        grouped[(site, slot)].append(row)
    # 场馆的亭全集：只要某亭向该场馆回传过任意场次边界，它就是该场馆的
    # 已知核验亭。一场次必须被全部已知亭以 complete 覆盖才算资料齐备；
    # 已知亭缺该场次边界本身就是缺批，按待查处理，不能据现有亭判未入场。
    site_stalls = defaultdict(set)
    for (stall, site, _slot) in boundaries:
        site_stalls[site].add(stall)
    for (site, slot), rows in grouped.items():
        declaring = {r["stall"] for r in rows}
        all_stalls = site_stalls[site]
        complete = (
            declaring == all_stalls
            and all(r["state"] == "complete" for r in rows)
        )
        stalls = ",".join(sorted(all_stalls))
        slot_audit[(site, slot)] = {"complete": complete, "stalls": stalls}
        conn.execute(
            "INSERT INTO fact_slot_audit(site, slot, complete, stalls) VALUES (?,?,?,?)",
            (site, slot, 1 if complete else 0, stalls),
        )
    return slot_audit


# ---------------------------------------------------------------------------
# 重建：名额池
# ---------------------------------------------------------------------------

def _build_quota(conn, availability):
    # 同 (场馆,场次,角色) 的晚到声明覆盖旧声明；units 是快照名额。
    quota = {}
    for row in availability:
        quota[(row["site"], row["slot"], row["role"])] = row
    for row in quota.values():
        conn.execute(
            "INSERT INTO fact_quota(site, slot, role, units, arrival) VALUES (?,?,?,?,?)",
            (row["site"], row["slot"], row["role"], row["units"], row["arrival"]),
        )
    return quota


# ---------------------------------------------------------------------------
# 重建：换票决定
# ---------------------------------------------------------------------------

def _group_tickets(ent_by_ticket, group):
    """联票组合的全部券号（票权按券号+角色核对的基础）。"""
    return [ent for ent in ent_by_ticket.values() if ent["group"] == group]


def _member_consumption(ent, lineage, consumption):
    """成员在原场次的消费状态：consumed / none / pending。"""
    root = lineage[ent["ticket"]]
    item = consumption.get((root, ent["site"], ent["slot"]))
    if item and item["consumed"]:
        return "consumed", item
    return "none", item


def _slot_is_complete(slot_audit, site, slot):
    info = slot_audit.get((site, slot))
    return bool(info and info["complete"])


def _evaluate_request(row, ent_by_ticket, lineage, consumption, slot_audit, quota,
                      held_by_others, approval=None):
    """判断一个换票请求在当前资料下能否承诺。

    返回 (decision, note, needs 列表[(role, 占用人数)])。只有整组仍能作为
    一组兑现时才允许承诺：已入场成人必须有场馆陪同批准且仍占新场次名额，
    未入场成员的原场次缺批时按待查处理，不能直接判未入场而承诺。
    approval 取该换票任一事件上携带的场馆批准号（多事件归并后）。
    """
    group = row["group"]
    members = _group_tickets(ent_by_ticket, group)
    listed = row["ticket_list"]

    # needs 是这笔换票本身的名额需求（由联票成员角色决定）。补发券与原券
    # 共享同一消费资格，不重复占名额，因此按谱系根去重。即使当前资料不齐、
    # 请求挂起，需求仍然存在，以便释放/冻结时留痕待释放名额。
    distinct_roots = {}
    for ent in members:
        distinct_roots.setdefault(lineage[ent["ticket"]], ent)
    needs_counter = defaultdict(int)
    for ent in distinct_roots.values():
        needs_counter[ent["role"]] += 1
    needs = sorted(needs_counter.items())

    if not members:
        return "rejected", f"联票组 {group} 无票权记录", needs
    member_tickets = {ent["ticket"] for ent in members}
    unknown = [t for t in listed if t not in ent_by_ticket]
    if unknown:
        return "rejected", f"券号无票权：{','.join(unknown)}", needs
    if set(listed) != member_tickets:
        return "rejected", "换票券清单与联票组成员不一致，不能只换组内部分券", needs

    for ent in members:
        status, _item = _member_consumption(ent, lineage, consumption)
        if status == "consumed":
            # 已消费成员（典型：已核验成人）只能以陪同资格换场，需要场馆
            # 明确批准；批准号在该换票的任一事件行上出现即视为已批准。
            if not approval:
                return "blocked", f"成员 {ent['ticket']}（{ent['role']}）已入场，缺少场馆陪同批准", needs
        elif not _slot_is_complete(slot_audit, ent["site"], ent["slot"]):
            # 原场次审计未齐：不能据此判定未入场，按待查挂起。
            return "pending", f"成员 {ent['ticket']} 原场次 {ent['slot']} 核验资料未齐，按待查处理", needs

    # 名额：陪同成人同样占用新场次人数；先扣除其他换票已经占住的名额。
    for (role, amount) in needs:
        pool = quota.get((ent_by_ticket[listed[0]]["site"], row["new_slot"], role))
        total = pool["units"] if pool else 0
        held = held_by_others.get((row["new_slot"], role), 0)
        if total - held < amount:
            return "rejected", (
                f"新场次 {row['new_slot']} 的 {role} 名额不足：需要 {amount}，"
                f"池内 {total}，已占用 {held}"
            ), needs
    return "promised", "整组可兑现，名额已预留", needs


def _build_exchanges(conn, exchanges, ent_by_ticket, lineage, receipt_by_id,
                     consumption, slot_audit, quota):
    # 同一 exchange 的多份事件按到达顺序归并，companion_approval 随行携带。
    grouped = defaultdict(list)
    for row in exchanges:
        grouped[row["exchange"]].append(row)

    facts = []
    # 先算所有“占名额”的换票，供名额核对时互相扣减。
    def effective_approval(events):
        for event in events:
            if event.get("companion_approval"):
                return event["companion_approval"]
        return None

    for exchange_id, events in grouped.items():
        events.sort(key=lambda r: r["arrival"])
        first = events[0]
        last = events[-1]
        members = _group_tickets(ent_by_ticket, first["group"])
        approval = effective_approval(events)

        # 已被 release_ack 明确释放、或被拒/挂起的换票不再占用名额。
        prelim_decision, _note, needs = _evaluate_request(
            first, ent_by_ticket, lineage, consumption, slot_audit, quota,
            held_by_others={}, approval=approval,
        )
        facts.append({
            "exchange": exchange_id, "events": events, "first": first, "last": last,
            "members": members, "needs": needs, "prelim": prelim_decision,
            "approval": approval,
        })

    def held_for(exclude_id):
        held = defaultdict(int)
        for fact in facts:
            if fact["exchange"] == exclude_id:
                continue
            last_state = fact["last"]["state"]
            if last_state == "release_ack":
                continue
            if fact["prelim"] in {"rejected", "blocked", "pending"}:
                continue
            for role, amount in fact["needs"]:
                held[(fact["first"]["new_slot"], role)] += amount
        return held

    for fact in facts:
        row = fact["first"]
        events = fact["events"]
        last = fact["last"]
        members = fact["members"]
        held = held_for(fact["exchange"])
        decision, note, needs = _evaluate_request(
            row, ent_by_ticket, lineage, consumption, slot_audit, quota, held,
            approval=fact["approval"],
        )

        # 该换票若成立会占住的名额，无论后续释放或冻结都需要留痕。
        required_units = dict(fact["needs"])
        held_units = dict(needs) if decision == "promised" else {}
        affected, quota_to_release, taken_over = [], [], 0
        final_state = last["state"]

        if last["state"] == "release_ack":
            decision = "released"
            note = "换票承诺已释放，预留名额回流可售池"
            held_units = {}
            # 留痕当初占住、现在待释放回流的是哪些名额。
            quota_to_release = [f"{role}:{amount}" for role, amount in fact["needs"]]
            # 受影响同行成员按独立消费资格（谱系根）列示，补发券不重复列。
            seen_roots = set()
            for ent in members:
                root = lineage[ent["ticket"]]
                if root in seen_roots:
                    continue
                seen_roots.add(root)
                status, _ = _member_consumption(ent, lineage, consumption)
                affected.append(f"{root}:{ent['role']}:{status}")
        elif last["state"] == "claimed":
            # 已承诺换票在 claim 之后遇到晚到核验：只要成员资格状态相对承诺
            # 时点发生变化（典型：儿童从“未使用”变成“已核验”），承诺即受
            # 影响，需要冻结等待处置，名额不能被客服立即卖给别人。
            current_decision, current_note, _ = _evaluate_request(
                row, ent_by_ticket, lineage, consumption, slot_audit, quota, held,
                approval=fact["approval"],
            )
            # 承诺时点的成员状态：用 claim 事件到达之前已掌握的消费事实重建。
            claim_arrival = next(
                (event["arrival"] for event in events if event["state"] == "claimed"),
                last["arrival"],
            )
            changed_members = []
            for ent in members:
                root = lineage[ent["ticket"]]
                now_consumed, _ = _member_consumption(ent, lineage, consumption)
                # claim 之前是否已有该资格的有效入场（按声明到达批次判断）。
                consumed_before_claim = any(
                    scan["state"] in {"entered", "reentry"}
                    and lineage.get(scan["ticket"]) == root
                    and scan["arrival"] <= claim_arrival
                    for scan in receipt_by_id.values()
                )
                if now_consumed == "consumed" and not consumed_before_claim:
                    changed_members.append(ent)

            if changed_members or current_decision != "promised":
                decision = "frozen"
                note = (
                    f"晚到核验改变资格，换票承诺受影响：{current_note}"
                    if current_decision != "promised"
                    else "晚到核验改变成员资格，换票承诺需重新处置"
                )
                held_units = required_units
                seen_roots = set()
                for ent in members:
                    root = lineage[ent["ticket"]]
                    if root in seen_roots:
                        continue
                    seen_roots.add(root)
                    status, _ = _member_consumption(ent, lineage, consumption)
                    if status == "consumed":
                        affected.append(f"{root}:{ent['role']}:{status}")
                quota_to_release = [f"{role}:{amount}" for role, amount in fact["needs"]]
                # 是否实际接管：新场次已经出现该谱系的成功入场才算接管。
                roots = {lineage[ent["ticket"]] for ent in members}
                taken_over = int(any(
                    scan["state"] in {"entered", "reentry"}
                    and scan["slot"] == row["new_slot"]
                    and lineage.get(scan["ticket"]) in roots
                    for scan in receipt_by_id.values()
                ))
            else:
                decision = "claimed"
                note = "换票名额已领取并冻结，等待交付"
                held_units = required_units
        elif last["state"] == "requested":
            if decision == "rejected":
                note = f"未交付换票待单已拒绝：{note}"

        conn.execute(
            "INSERT INTO fact_exchange(exchange, ticket_group, old_slot, new_slot, tickets,"
            " requested, last_event, last_arrival, companion_approval, decision, held_units,"
            " affected_members, quota_to_release, taken_over, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact["exchange"], row["group"], row["old_slot"], row["new_slot"],
                ";".join(row["ticket_list"]), iso(row["requested_dt"]), last["state"],
                last["arrival"], fact["approval"], decision,
                json.dumps(held_units, ensure_ascii=False), ";".join(affected),
                ";".join(quota_to_release), taken_over, note,
            ),
        )
    return facts


# ---------------------------------------------------------------------------
# 重建：场馆结算与对账
# ---------------------------------------------------------------------------

def _rate_lookup(rates_rows):
    return {(r["site"], r["role"]): r["cents"] for r in rates_rows}


def _build_settlement(conn, rates_rows, statements, receipt_by_id, consumption,
                      ent_by_ticket, lineage):
    """生成结算行并与场馆声明对账。

    结算单位是“消费资格”而不是扫描次数：一个谱系根在一场只产生一次计费；
    关联凭据的再入场不额外计费。儿童等依赖同行核验的角色，只有同行事实可
    证实（coupled 窗口成立且成人凭据未被作废）后才可结算。
    """
    rates = _rate_lookup(rates_rows)
    # 场馆声明原样留痕，已有场馆凭证始终保留。
    for st in statements:
        conn.execute(
            "INSERT INTO fact_venue_statement(settlement, arrival, day, site, receipt, kind, cents)"
            " VALUES (?,?,?,?,?,?,?)",
            (st["settlement"], st["arrival"], st["day_date"].isoformat(), st["site"],
             st["receipt"], st["kind"], st["cents"]),
        )

    # 场馆确认期按到达顺序开放。常规资料在第 n 批到达时，归入“当时已开放
    # 的最后一个确认期”；早于第一个确认期的资料在首期确认时一并纳入。
    # 更正/作废产生的反向差额只能登记到资料到达之后的下一个开放期，绝不能
    # 回写已经确认关闭的旧期；晚于最后一期的资料挂起等待场馆下次确认。
    venue_periods = sorted(
        {(st["settlement"], st["day_date"].isoformat(), st["arrival"]) for st in statements},
        key=lambda item: item[2],
    )
    first_period_arrival = venue_periods[0][2] if venue_periods else None
    last_period_arrival = venue_periods[-1][2] if venue_periods else None

    def open_period(arrival, is_reversal=False):
        if not venue_periods:
            return None
        if is_reversal:
            # 严格晚于资料到达批次的第一个确认期。
            for settlement, day, period_arrival in venue_periods:
                if period_arrival > arrival:
                    return settlement, day
            return None
        if arrival <= first_period_arrival:
            return venue_periods[0][0], venue_periods[0][1]
        current = venue_periods[0]
        for period in venue_periods:
            if period[2] <= arrival:
                current = period
            else:
                break
        if arrival > last_period_arrival:
            return None
        return current[0], current[1]

    # 当前有效消费 -> 结算行；同时判断儿童同行事实是否可证实。
    adult_entries = {}
    for (root, site, slot), item in consumption.items():
        root_ent = next(
            (e for ticket, e in ent_by_ticket.items() if lineage[ticket] == root), None
        )
        if root_ent and root_ent["role"] == "adult" and item["consumed"]:
            adult_entries[(root_ent["group"], site, slot)] = item

    lines = []  # (arrival, site, receipt, root, role, group, kind, cents, prior_settlement, prior_receipt, event_time)
    # 作废凭据的场馆原结算行，供 reversal 指回。
    prior_by_receipt = {}
    for st in statements:
        prior_by_receipt.setdefault(st["receipt"], []).append(st)
    # 每个凭据最早的 entered 声明到达批次（双发/作废全过程都在声明流水里），
    # 用于把原始计费行归到作废前的确认期。
    first_entered_arrival = {}
    for row in conn.execute(
        "SELECT receipt, MIN(arrival) AS first_arrival FROM fact_receipt_statement"
        " WHERE state='entered' GROUP BY receipt"
    ).fetchall():
        first_entered_arrival[row["receipt"]] = row["first_arrival"]

    for (root, site, slot), item in sorted(consumption.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        root_ent = next(
            (e for ticket, e in ent_by_ticket.items() if lineage[ticket] == root), None
        )
        if root_ent is None:
            continue
        rate = rates.get((site, root_ent["role"]))
        if rate is None:
            continue

        def child_companion_proven(entry_item):
            if not (root_ent["coupled"] == "yes" and root_ent["role"] == "child"):
                return True
            adult = adult_entries.get((root_ent["group"], site, slot))
            return bool(
                adult and coupled_condition_holds(adult["entry_time"], entry_item["entry_time"])
            )

        # 已被作废的原始入场：原始计费行仍然保留（与场馆已出具的凭证对应），
        # 再追加一条反向差额；净额为 0，资格视图显示未使用。
        if item["voided_receipt"]:
            void_scan = receipt_by_id[item["voided_receipt"]]
            # 反向差额与同行条件无关：凭据既已作废，原先任何计费都必须冲回。
            prior_stmts = sorted(
                prior_by_receipt.get(item["voided_receipt"], []), key=lambda s: s["arrival"]
            )
            prior = next(
                (s for s in prior_stmts if s["cents"] > 0),
                prior_stmts[0] if prior_stmts else None,
            )
            lines.append((
                void_scan["arrival"], site, item["voided_receipt"], root,
                root_ent["role"], root_ent["group"], "reversal", -rate,
                prior["settlement"] if prior else None,
                item["voided_receipt"], iso(void_scan["occurrence_dt"]),
            ))
            # 原计费行只在同行事实可证实（或本就不依赖同行）时补出，
            # 与场馆凭证配对；不可证实时不产生应付，只保留冲回。
            if child_companion_proven(item):
                original_arrival = first_entered_arrival.get(
                    item["voided_receipt"], void_scan["arrival"]
                )
                lines.append((
                    original_arrival, site, item["voided_receipt"], root,
                    root_ent["role"], root_ent["group"], "payable", rate, None, None,
                    iso(void_scan["occurrence_dt"]),
                ))
            continue

        if not item["consumed"]:
            # 只有再入场、没有独立消费的资格：不产生结算行。
            continue
        receipt = item["entry_receipt"]
        scan = receipt_by_id[receipt]
        if child_companion_proven(item):
            lines.append((scan["arrival"], site, receipt, root, root_ent["role"],
                          root_ent["group"], "payable", rate, None, None,
                          iso(item["entry_time"])))

    lines.sort(key=lambda line: line[0])
    expected_by_receipt = defaultdict(int)
    for (arrival, site, receipt, root, role, group, kind, cents,
         prior_settlement, prior_receipt, event_time) in lines:
        is_reversal = kind == "reversal"
        period = open_period(arrival, is_reversal=is_reversal)
        period_id = period[0] if period else f"PENDING-{site}"
        confirmed_day = period[1] if period else None
        conn.execute(
            "INSERT INTO fact_settlement_line(period_id, site, receipt, lineage_root, role,"
            " ticket_group, kind, cents, prior_settlement, prior_receipt, event_time, confirmed_day)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (period_id, site, receipt, root, role, group, kind, cents,
             prior_settlement, prior_receipt, event_time, confirmed_day),
        )
        expected_by_receipt[(site, receipt)] += cents

    # 对账：按凭据净额比较事实仓与场馆全部凭证的合计；场馆凭证原样保留，
    # 净额不一致即为需要登记的差额，场馆尚未声明的列入待确认。
    venue_by_receipt = defaultdict(int)
    for st in statements:
        venue_by_receipt[(st["site"], st["receipt"])] += st["cents"]
    all_keys = set(expected_by_receipt) | set(venue_by_receipt)
    for site, receipt in sorted(all_keys):
        expected = expected_by_receipt.get((site, receipt), 0)
        has_venue = (site, receipt) in venue_by_receipt
        venue = venue_by_receipt.get((site, receipt))
        if not has_venue:
            status, note = "missing_venue_confirmation", "事实仓有应付结算，场馆尚未在任何确认期声明"
        elif venue == expected:
            status, note = "matched", "与场馆凭证净额一致"
        else:
            status, note = "difference", f"净额差额 {expected - venue}，需在下一开放期登记"
        conn.execute(
            "INSERT INTO fact_reconciliation(period_id, site, receipt, expected_cents, venue_cents,"
            " status, note) VALUES (?,?,?,?,?,?,?)",
            ("", site, receipt, expected, venue, status, note),
        )

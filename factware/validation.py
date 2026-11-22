"""原始记录的字段级校验与规范化。

校验只针对单条记录自身：必填、取值域、时间/数字格式。记录之间的引用关系
（券号、凭据、名额）由引擎在构建事实时核对，发现问题进入隔离表而非丢弃。
"""
from .timeutil import parse_dt, parse_day

ROLES = {"adult", "child", "solo"}
SCAN_STATES = {"entered", "denied", "reentry", "void"}
BATCH_STATES = {"partial", "complete"}
EXCHANGE_STATES = {"requested", "claimed", "release_ack"}
SETTLEMENT_KINDS = {"accepted", "correction_received"}

REQUIRED_COLUMNS = {
    "entitlements.csv": ["arrival", "ticket", "group", "role", "site", "slot", "coupled", "purchaser"],
    "scans.csv": ["arrival", "stall", "receipt", "occurrence", "ticket", "site", "slot", "state"],
    "batches.csv": ["arrival", "stall", "site", "slot", "serial", "state"],
    "availability.csv": ["arrival", "site", "slot", "role", "units"],
    "exchanges.csv": ["arrival", "exchange", "requested", "group", "old_slot", "new_slot", "tickets", "state"],
    "rates.csv": ["site", "role", "cents"],
    "settlement.csv": ["arrival", "settlement", "day", "site", "receipt", "kind", "cents"],
    "ticket_issues.csv": ["arrival", "previous", "reissued", "transport_reference"],
}


def _as_int(value, filename, line, column, errors):
    text = str(value).strip()
    try:
        if text != str(int(text)):
            raise ValueError
        return int(text)
    except ValueError:
        errors.append(
            {"source": filename, "line": line, "reason": f"列 {column} 必须是整数，实际为 {value!r}"}
        )
        return None


def _nonempty(row, columns, filename, line, errors):
    missing = [column for column in columns if str(row.get(column, "")).strip() == ""]
    if missing:
        errors.append(
            {"source": filename, "line": line, "reason": f"必填列为空：{','.join(missing)}"}
        )
        return False
    return True


def _one_of(row, column, allowed, filename, line, errors):
    value = str(row.get(column, "")).strip()
    if value not in allowed:
        errors.append(
            {
                "source": filename,
                "line": line,
                "reason": f"列 {column} 取值 {value!r} 不在 {sorted(allowed)} 中",
            }
        )
        return None
    return value


def validate(sources):
    """校验全部来源，返回 (normalized, errors)。

    normalized 与 sources 同构，只保留通过单条校验的记录并完成类型转换；
    未通过的记录进入 errors，原始件本身不被修改。
    """
    normalized = {name: [] for name in REQUIRED_COLUMNS}
    errors = []

    for filename, required in REQUIRED_COLUMNS.items():
        for line, raw in enumerate(sources.get(filename, []), start=2):
            row = {key: ("" if value is None else str(value).strip()) for key, value in raw.items()}
            missing_columns = [column for column in required if column not in row]
            if missing_columns:
                errors.append(
                    {"source": filename, "line": line, "reason": f"缺少列：{','.join(missing_columns)}"}
                )
                continue
            if not _nonempty(row, [c for c in required if c != "link" and c != "companion_approval"], filename, line, errors):
                continue
            record = {"_line": line}
            record.update(row)

            if filename == "entitlements.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                if _one_of(row, "role", ROLES, filename, line, errors) is None:
                    continue
                if _one_of(row, "coupled", {"yes", "no"}, filename, line, errors) is None:
                    continue
            elif filename == "scans.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                if _one_of(row, "state", SCAN_STATES, filename, line, errors) is None:
                    continue
                try:
                    record["occurrence_dt"] = parse_dt(row["occurrence"])
                except ValueError:
                    errors.append({"source": filename, "line": line, "reason": "occurrence 时间格式无效"})
                    continue
                # reentry 必须带关联凭据；其他状态带 link 也允许（补发原券场景）。
                if record["state"] == "reentry" and not row.get("link"):
                    errors.append({"source": filename, "line": line, "reason": "reentry 缺少关联凭据 link"})
                    continue
            elif filename == "batches.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                record["serial"] = _as_int(row["serial"], filename, line, "serial", errors)
                if _one_of(row, "state", BATCH_STATES, filename, line, errors) is None:
                    continue
            elif filename == "availability.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                record["units"] = _as_int(row["units"], filename, line, "units", errors)
                if record["units"] is not None and record["units"] < 0:
                    errors.append({"source": filename, "line": line, "reason": "units 不能为负数"})
                    continue
                if _one_of(row, "role", ROLES, filename, line, errors) is None:
                    continue
            elif filename == "exchanges.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                try:
                    record["requested_dt"] = parse_dt(row["requested"])
                except ValueError:
                    errors.append({"source": filename, "line": line, "reason": "requested 时间格式无效"})
                    continue
                if _one_of(row, "state", EXCHANGE_STATES, filename, line, errors) is None:
                    continue
                record["ticket_list"] = [item for item in row["tickets"].split(";") if item]
                if not record["ticket_list"]:
                    errors.append({"source": filename, "line": line, "reason": "换票券清单 tickets 为空"})
                    continue
            elif filename == "rates.csv":
                record["arrival"] = 0
                record["cents"] = _as_int(row["cents"], filename, line, "cents", errors)
            elif filename == "settlement.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)
                record["cents"] = _as_int(row["cents"], filename, line, "cents", errors)
                if _one_of(row, "kind", SETTLEMENT_KINDS, filename, line, errors) is None:
                    continue
                try:
                    record["day_date"] = parse_day(row["day"])
                except ValueError:
                    errors.append({"source": filename, "line": line, "reason": "day 日期格式无效"})
                    continue
            elif filename == "ticket_issues.csv":
                record["arrival"] = _as_int(row["arrival"], filename, line, "arrival", errors)

            if any(value is None for key, value in record.items() if key in {"arrival", "units", "cents", "serial"}):
                continue
            normalized[filename].append(record)

    return normalized, errors

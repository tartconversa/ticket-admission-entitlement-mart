"""读取交接目录中的原始资料（CSV / TSV / JSONL）。

只做读取与格式层面的检查，不做业务判定。读取器按 `arrival` 字段保留到达
批次；没有到达批号的固定目录资料（rates.csv）归为第 0 批。
"""
from pathlib import Path
import csv
import json

# 交接资料约定的文件名与主键列。
SOURCES = (
    "entitlements.csv",
    "scans.csv",
    "batches.csv",
    "availability.csv",
    "exchanges.csv",
    "rates.csv",
    "settlement.csv",
    "ticket_issues.csv",
)


class SourceError(ValueError):
    """原件格式问题，带文件名与行号，方便售票同事退回资料。"""

    def __init__(self, filename, line, reason):
        super().__init__(f"{filename}:{line} {reason}")
        self.filename = filename
        self.line = line
        self.reason = reason


def read_records(path):
    """读取一个交接文件，返回 dict 记录列表；保留原始字符串。"""
    with path.open(encoding="utf-8", newline="") as stream:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in stream if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise SourceError(path.name, 0, "文件包含非对象记录")
            return rows
        delimiter = "\t" if path.suffix == ".tsv" else ","
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise SourceError(path.name, 1, "表头缺失或列名重复")
        rows = list(reader)
        for index, row in enumerate(rows, start=2):
            if None in row or None in row.values():
                raise SourceError(path.name, index, "记录列数与表头不符")
        return rows


def describe(root):
    """返回各已识别交接文件的结构和行数（供资料盘点工具复用）。"""
    root = Path(root)
    report = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in {".csv", ".tsv", ".jsonl"}:
            records = read_records(path)
            report[path.name] = {
                "记录数": len(records),
                "字段": sorted({key for record in records for key in record}),
            }
    return report


def load_directory(root):
    """按约定文件名读取整个交接目录。

    返回 (sources, errors)：sources 为 {文件名: [原始记录]}；缺少的文件以
    空列表表示，errors 收集读取阶段的格式错误。
    """
    root = Path(root)
    sources = {}
    errors = []
    for name in SOURCES:
        path = root / name
        if not path.exists():
            sources[name] = []
            continue
        try:
            sources[name] = read_records(path)
        except (SourceError, json.JSONDecodeError, OSError) as exc:
            sources[name] = []
            errors.append(
                {"source": name, "line": getattr(exc, "line", 0), "reason": str(exc)}
            )
    # 发现目录里未登记的交接文件也要提示，避免静默丢资料。
    known = set(SOURCES)
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in {".csv", ".tsv", ".jsonl"} and path.name not in known:
            errors.append(
                {"source": path.name, "line": 0, "reason": "未登记的交接文件，已忽略"}
            )
    return sources, errors

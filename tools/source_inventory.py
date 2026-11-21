"""查看合成资料的文件结构。"""
from pathlib import Path
import argparse
import csv
import json


def read_records(path):
    """读取一个交接文件中的原始记录。"""
    with path.open(encoding="utf-8", newline="") as stream:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in stream if line.strip()]
        delimiter = "\t" if path.suffix == ".tsv" else ","
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"文件表头无效：{path.name}")
        records = list(reader)
        if any(None in record or None in record.values() for record in records):
            raise ValueError(f"记录列数与表头不符：{path.name}")
        return records


def describe(root):
    """返回各已识别的交接文件的结构和行数。"""
    report = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in {".csv", ".tsv", ".jsonl"}:
            records = read_records(path)
            if any(not isinstance(record, dict) for record in records):
                raise ValueError(f"文件包含非对象记录：{path.name}")
            report[path.name] = {
                "记录数": len(records),
                "字段": sorted({key for record in records for key in record}),
            }
    return report


def main():
    parser = argparse.ArgumentParser(description="查看交接文件结构")
    parser.add_argument("directory", nargs="?", default=Path("examples"), type=Path)
    args = parser.parse_args()
    print(json.dumps(describe(args.directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

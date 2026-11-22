"""命令行入口：持续接收批次、客服核实、场馆对账。

示例：
  python -m factware.cli ingest examples
  python -m factware.cli tickets
  python -m factware.cli exchanges
  python -m factware.cli settlement
"""
import argparse
import json
import sys
from pathlib import Path

from .sources import load_directory
from .store import connect
from .engine import ingest
from . import queries

DEFAULT_DB = "data/factware.sqlite3"


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _open(args):
    return connect(args.db)


def cmd_ingest(args):
    sources, read_errors = load_directory(Path(args.directory))
    conn = _open(args)
    errors = ingest(conn, sources, read_errors)
    payload = {
        "database": args.db,
        "已处理来源": {name: len(rows) for name, rows in sources.items()},
        "隔离与校验问题": errors + queries.ingest_issues(conn),
    }
    conn.commit()
    conn.close()
    _print(payload)
    return 1 if errors else 0


def cmd_tickets(args):
    conn = _open(args)
    _print(queries.ticket_status(conn, args.ticket))
    conn.close()
    return 0


def cmd_attendance(args):
    conn = _open(args)
    _print(queries.attendance_experiences(conn))
    conn.close()
    return 0


def cmd_receipts(args):
    conn = _open(args)
    _print(queries.receipt_statements(conn, args.receipt))
    conn.close()
    return 0


def cmd_audit(args):
    conn = _open(args)
    _print(queries.slot_audit_status(conn))
    conn.close()
    return 0


def cmd_quota(args):
    conn = _open(args)
    _print(queries.quota_snapshot(conn))
    conn.close()
    return 0


def cmd_exchanges(args):
    conn = _open(args)
    _print(queries.exchange_decisions(conn, args.exchange))
    conn.close()
    return 0


def cmd_settlement(args):
    conn = _open(args)
    _print({
        "事实仓结算行": queries.settlement_lines(conn),
        "场馆对账": queries.reconciliation(conn),
        "场馆凭证留痕": queries.venue_statements(conn),
    })
    conn.close()
    return 0


def cmd_issues(args):
    conn = _open(args)
    _print(queries.ingest_issues(conn))
    conn.close()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="factware", description="联票事实仓")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 路径（默认 {DEFAULT_DB}）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="接收一个交接目录并重建事实")
    p.add_argument("directory", help="含交接 CSV 的目录")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("tickets", help="票权资格、消费与到场")
    p.add_argument("ticket", nargs="?", help="只看某张券")
    p.set_defaults(func=cmd_tickets)

    sub.add_parser("attendance", help="独立到场经历（含再入场）").set_defaults(func=cmd_attendance)

    p = sub.add_parser("receipts", help="凭据声明流水（双发/拒绝/作废）")
    p.add_argument("receipt", nargs="?", help="只看某张凭据")
    p.set_defaults(func=cmd_receipts)

    sub.add_parser("audit", help="场次核验齐备性").set_defaults(func=cmd_audit)
    sub.add_parser("quota", help="换票名额池与占用/冻结").set_defaults(func=cmd_quota)

    p = sub.add_parser("exchanges", help="换票决定")
    p.add_argument("exchange", nargs="?", help="只看某笔换票")
    p.set_defaults(func=cmd_exchanges)

    sub.add_parser("settlement", help="结算行与场馆对账").set_defaults(func=cmd_settlement)
    sub.add_parser("issues", help="隔离与校验问题").set_defaults(func=cmd_issues)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

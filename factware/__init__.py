"""联票事实仓：票权、入场、换票与场馆分账的本地事实处理。

设计原则见 docs/design.md：原始记录只追加，判定结果按最新资料全量重算，
任何更正都留下可追溯的差额，而不是悄悄覆盖旧事实。
"""

__all__ = ["sources", "validation", "store", "model", "engine", "cli"]

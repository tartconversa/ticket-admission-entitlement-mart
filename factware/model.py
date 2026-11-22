"""领域判定辅助：票号谱系、联票同行条件。"""
from datetime import timedelta

# coupled 条件：成人入场成功时间不得晚于儿童，且在儿童之前 30 分钟内。
COUPLED_WINDOW_MINUTES = 30


def build_lineage(entitlements, ticket_issues):
    """建立券号 -> 谱系根（最初消费资格券号）的映射。

    券号补发以 ticket_issues 的明确换发关系为准：补发券 T1R 与原券 T1 共享
    同一消费资格，因此再入场可以接回原消费，而两个确实不同的入场仍各自保留
    为独立到场经历。同一 reissued 出现多份声明时以晚到者为准。
    """
    issue_map = {}
    for issue in sorted(ticket_issues, key=lambda row: row["arrival"]):
        issue_map[issue["reissued"]] = issue["previous"]

    roots = {}
    for ent in entitlements:
        ticket = ent["ticket"]
        root = ticket
        seen = set()
        while root in issue_map and root not in seen:
            seen.add(root)
            root = issue_map[root]
        roots[ticket] = root
    return roots, issue_map


def coupled_condition_holds(adult_time, child_time):
    """成人成功入场不晚于儿童，且在儿童之前 30 分钟内（含边界）。"""
    if adult_time is None or child_time is None:
        return False
    delta = child_time - adult_time
    return timedelta(0) <= delta <= timedelta(minutes=COUPLED_WINDOW_MINUTES)

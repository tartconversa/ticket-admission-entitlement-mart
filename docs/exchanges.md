# 交接资料说明

`entitlements.csv` 用 `group` 表示联票组合，`role` 为 `adult`、`child` 或 `solo`。 `coupled` 条件为两人到同一场馆同一场次，成人入场成功时间不得晚于儿童且在此前 30 分钟内。 `scans.csv` 的 `receipt` 是核验凭据，同一凭据可被中心与亭子重复转发。 `link` 指关联原入场凭据或补发原券，`occurrence` 为实际时间。 `batches.csv` 的 `complete` 代表亭的该场次扫描审计边界。换票名额以 `availability.csv` 为准，针对要换的是哪一组券以及相关名额的领取状态见 `exchanges.csv`。场馆结算日用 UTC，费率按 `rates.csv` 的场馆与角色组合。同一联票存在部分消费时，新的场次安排需要场馆明确批准已入场成人以不新增消费的陪同资格换场，且承诺中仍需占用其新场次人数。少任一所需名额不能承诺。券号补发以 ticket_issues.csv 的明确换发关系为准。后续同一核验凭据的作废或批次更正以更晚到达的签字声明为准。

字段 `arrival` 为合成交接批号，同号记录属于同次接收。没有批号的文件为该批资料采用的固定目录。

- `entitlements.csv`
- `scans.csv`
- `batches.csv`
- `availability.csv`
- `exchanges.csv`
- `settlement.csv`
- `rates.csv`
- `ticket_issues.csv`

# 联票入园的权益消耗与场馆分账事实仓

接收票权、场次、检票交接与换票决定，处理来源迟报与记录冲突，为客服核实
换票、为场馆核对结算明细与回执提供本地事实仓。合成交接资料位于
`examples/`，字段含义见 [交接资料说明](docs/exchanges.md)，判定规则与表
结构见 [事实仓设计说明](docs/design.md)。

运行环境为 Python 3.14.10，仅依赖标准库与内置 SQLite。

## 快速开始

```bash
# 接收一个交接目录并重建事实（默认写入 data/factware.sqlite3）
python -m factware.cli ingest examples

# 客服视角
python -m factware.cli tickets            # 票权资格、消费与到场
python -m factware.cli tickets T1R        # 单张券（含谱系根与再入场）
python -m factware.cli attendance         # 独立到场经历（再入场不重复消耗）
python -m factware.cli receipts X14       # 凭据声明流水（双发/拒绝/作废）
python -m factware.cli audit              # 场次核验齐备性（缺批按待查）
python -m factware.cli quota              # 换票名额池与占用/冻结
python -m factware.cli exchanges          # 换票决定、受影响成员、待释放名额

# 场馆视角
python -m factware.cli settlement         # 结算行、反向差额与对账
python -m factware.cli issues             # 隔离与校验问题
```

所有输出为 JSON。`--db 路径` 可指定其他 SQLite 文件，重复 `ingest` 同一
目录是幂等的；业务更正应以新到达批次的新记录接入，事实仓会整体重判并以
结算差额留痕，而不是覆盖旧结论。

## 处理模型

- **原始层只追加**：交接原件永不修改，双发、作废、错误声明全部留痕。
- **派生层重建**：每批资料到达后基于全部已知原件重算凭据、消费、审计、
  换票与结算。
- **资格而非扫描次数**：补发券与再入场接回同一消费资格，一次离场再入保留
  为独立到场但不额外计费。
- **缺批待查**：只有场次的全部已知核验亭资料齐备，才能据此判定未入场。
- **整组兑现**：组合票换场必须整组可承诺，已入场成人需场馆陪同批准且仍占
  新场次名额；晚到核验改变资格时冻结名额，不允许立即转卖。
- **跨期差额**：作废的反向差额进入下一开放结算期并指回原票与原结算行，
  场馆已出具的凭证始终保留。

## 测试

```bash
python -m unittest discover -s tests
```

资料盘点工具只检查和读取原件，不写入：

```bash
python tools/source_inventory.py
```

# 联票入园的权益消耗与场馆分账事实

联票入园使用的合成交接资料位于 `examples/`。文件模拟同一业务周期中陆续收到的原始记录，其中包括来源迟报和有冲突的记录。字段和原始记录的含义见 [交接资料说明](docs/exchanges.md)。

运行环境为 Python 3.14.10。依赖 Python 标准库。

在项目根目录运行 `python tools/source_inventory.py` 可查看输入文件的列名和记录数。传入目录路径可检查另一批 CSV、TSV 或 JSONL 资料。运行 `python -m unittest discover -s tests` 可核对随项目提供的输入资料。资料工具仅检查和读取原件。

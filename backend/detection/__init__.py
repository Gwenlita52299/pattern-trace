"""交易结构级 CoinJoin 启发式检测 — 无 CSV / ML / 聚类依赖。

由 `coinjoin.py` 提供 CoinJoinDetector：基于交易自身结构的确定性规则判定，
直接回答「一笔 tx 是否是 CoinJoin」，无需任何外部标记文件。
"""

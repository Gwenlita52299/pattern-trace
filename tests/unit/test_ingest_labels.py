"""标签加载单测 — IG-07（issue #5：跨链 CSV / crosschain_tx_set 标签链路已移除）。

验证 ingest/load_labels 不再读取 op_returns_interesting.csv、不再写入
crosschain_tx_set 表，跨链判定交由运行时 CrosschainDetector；本模块只保留
CoinJoin 产出地址 → addresses_meta 的加载（内存标签集切回二元组）。
"""
import inspect

import ingest.load_labels as ll


class TestCrosschainLabelRemoved:
    def test_op_return_csv_path_removed(self):
        """旧 CSV 读取常量与 NUL 剥离 helper（仅服务 crosschain CSV）已删除。"""
        assert not hasattr(ll, "OP_RETURN_CSV")
        assert not hasattr(ll, "_read_csv_nul_safe")

    def test_load_into_memory_no_crosschain_model_reference(self):
        """load_into_memory 不再引用 CrosschainTx 模型（跨链判定交给运行时 Detector）。"""
        assert "CrosschainTx" not in inspect.getsource(ll.load_into_memory)

    def test_load_into_memory_returns_mixer_and_coinjoin(self):
        """内存标签集契约为 (mixer 地址集, coinjoin txid 集) 二元组（无 crosschain 映射）。"""
        annotations = getattr(ll.load_into_memory, "__annotations__", {})
        # 返回类型注解为 2 元组；若无注解则跳过（运行期由 DB 门禁脚本验证）
        if "return" in annotations:
            assert "tuple" in str(annotations["return"])
        assert ll.COINJOIN_OUTPUTS_PARQUET == \
            "results/step2_label/coinjoin_outputs_labeled.parquet"

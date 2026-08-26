"""标签加载单测 — IG-07（CSV 解析与 NUL 剥离；DB 落库由门禁脚本验证）。"""

from ingest.load_labels import _read_csv_nul_safe


class TestCsvNulSafe:
    def test_strips_nul_bytes(self, tmp_path):
        p = tmp_path / "cj.csv"
        p.write_bytes(
            b"txid,block_height\n\x00abc123,884469\ndef456,884470\n")
        rows = _read_csv_nul_safe(p)
        assert [r["txid"] for r in rows] == ["abc123", "def456"]

    def test_normal_csv_unaffected(self, tmp_path):
        p = tmp_path / "plain.csv"
        p.write_text("txid,protocol\naa11,runes\nbb22,thorchain_swap\n")
        rows = _read_csv_nul_safe(p)
        assert {r["protocol"] for r in rows} == {"runes", "thorchain_swap"}

    def test_empty_body_yields_no_rows(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_bytes(b"txid,protocol\n")
        assert _read_csv_nul_safe(p) == []

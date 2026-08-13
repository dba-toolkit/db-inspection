from __future__ import annotations

import unittest

from plugins.oracle.metrics import OracleMetricProvider, counter_rates, safe_float, summarize


class OracleMetricProviderTests(unittest.TestCase):
    def test_safe_float_oracle_semantics(self) -> None:
        self.assertEqual(safe_float("1,234.5"), 1234.5)
        self.assertIsNone(safe_float("-"))
        self.assertIsNone(safe_float("N/A"))

    def test_summarize_without_p95(self) -> None:
        result = summarize([1, 2, 3])
        self.assertEqual(set(result.keys()), {"count", "min", "average", "max"})
        self.assertEqual(result["average"], 2.0)

    def test_counter_rates(self) -> None:
        rows = [
            {"timestamp": "t0", "elapsed_ms": "0", "physical_reads": "0"},
            {"timestamp": "t1", "elapsed_ms": "1000", "physical_reads": "500"},
        ]
        rates = counter_rates(rows, ["physical_reads"])
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0]["physical_reads_per_sec"], 500.0)

    def test_provider_is_instance_based(self) -> None:
        provider = OracleMetricProvider()
        self.assertTrue(hasattr(provider, "derive"))


if __name__ == "__main__":
    unittest.main()

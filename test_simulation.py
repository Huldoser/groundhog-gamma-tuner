import os
import sys
import unittest

import modes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import simulate  # noqa: E402


class SimulationTests(unittest.TestCase):
    """Each mode on a simulated Gamma 601 does what its name says."""

    def _check(self, cooling, ambient):
        results = {
            key: simulate.run(key, 6, ambient, cooling) for key in modes.MODE_ORDER
        }
        best = max(results, key=lambda key: results[key]["good GH/s"])
        leanest = min(results, key=lambda key: results[key]["J/TH"])
        self.assertEqual(best, modes.MAX_HASHRATE, results)
        self.assertEqual(leanest, modes.EFFICIENCY_MODE, results)
        balanced = results[modes.BALANCED]
        self.assertLessEqual(balanced["peak ASIC °C"], modes.SAFE_MAX_TEMP, results)
        self.assertLessEqual(balanced["VR °C"], modes.SAFE_MAX_VR_TEMP, results)
        for result in results.values():
            self.assertLess(result["peak ASIC °C"], 71.0, results)

    def test_stock_cooling_on_a_warm_day(self):
        self._check("stock", 30)

    def test_custom_cooling_on_a_cool_day(self):
        self._check("custom", 16)


if __name__ == "__main__":
    unittest.main()

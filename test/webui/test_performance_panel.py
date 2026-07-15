import unittest

from webui.components import performance_panel


class PerformancePanelFormattingTests(unittest.TestCase):
    def test_unavailable_values_are_not_rendered_as_zero(self):
        self.assertEqual(performance_panel._gib(None), "N/A")
        self.assertEqual(performance_panel._percent(None), "N/A")
        self.assertEqual(performance_panel._temperature(None), "N/A")

    def test_available_values_include_units(self):
        self.assertEqual(performance_panel._gib(2 * 1024**3), "2.0 GB")
        self.assertEqual(performance_panel._percent(42.4), "42%")
        self.assertEqual(performance_panel._temperature(64.8), "65 °C")


if __name__ == "__main__":
    unittest.main()

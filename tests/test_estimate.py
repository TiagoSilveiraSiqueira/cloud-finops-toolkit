import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from estimate import ROOT, ValidationError, compare, read_json, validate_catalog, validate_scenario


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.catalog_data = read_json(ROOT / "catalog/prices-brazilsouth.json")
        self.catalog = validate_catalog(self.catalog_data)
        self.scenario_data = read_json(ROOT / "examples/laboratorio.json")

    def result(self, data=None):
        scenario = validate_scenario(self.scenario_data if data is None else data, self.catalog)
        return compare(self.catalog, scenario)

    def test_verified_example_multiplicity_totals_and_potential_savings(self):
        report = self.result()
        self.assertEqual(report["base"]["compute_usd"], "131.816")
        self.assertEqual(report["optimized"]["compute_usd"], "43.984")
        self.assertEqual(report["potential_savings_usd"], "87.832")
        self.assertEqual(report["potential_savings_percent"], "66.63")
        self.assertEqual(report["base"]["resources"][0]["compute_usd"], "98.112")

    def test_fractional_hours_are_calculated_with_decimal_without_float_drift(self):
        data = copy.deepcopy(self.scenario_data)
        row = {"name": "fractional", "armSkuName": "Standard_B1s", "quantity": 3, "hours": Decimal("0.1")}
        data["base"]["resources"] = [row]
        data["optimized"]["resources"] = []
        report = self.result(data)
        self.assertEqual(report["base"]["compute_usd"], "0.00504")
        self.assertEqual(report["potential_savings_usd"], "0.00504")
        self.assertEqual(report["potential_savings_percent"], "100")

    def test_no_rounding_per_resource_before_summing(self):
        data = copy.deepcopy(self.scenario_data)
        data["base"]["resources"] = [
            {"name": "a", "armSkuName": "Standard_B1s", "quantity": 1, "hours": Decimal("0.1")},
            {"name": "b", "armSkuName": "Standard_B1s", "quantity": 1, "hours": Decimal("0.1")},
        ]
        self.assertEqual(self.result(data)["base"]["compute_usd"], "0.00336")

    def test_zero_baseline_has_no_percentage(self):
        data = copy.deepcopy(self.scenario_data)
        data["base"]["resources"] = []
        data["optimized"]["resources"] = []
        report = self.result(data)
        self.assertEqual(report["potential_savings_usd"], "0")
        self.assertIsNone(report["potential_savings_percent"])
        self.assertEqual(report["percentage_note"], "undefined_baseline_zero")

    def test_bounded_large_scenario_and_tiny_baseline_do_not_overflow_percentage(self):
        prices = copy.deepcopy(self.catalog_data)
        prices["kind"] = "simulation"
        prices["source"]["http_status"] = None
        prices["source"]["query_url"] = "simulation://unit-test"
        for item in prices["items"]:
            item["source"] = "simulation://unit-test"
            item["retailPrice"] = "0.00000001" if item["armSkuName"] == "Standard_B1s" else "1000000"
        catalog = validate_catalog(prices)
        data = copy.deepcopy(self.scenario_data)
        data["period_hours"] = 1000000
        data["base"]["resources"] = [
            {"name": "small", "armSkuName": "Standard_B1s", "quantity": 1, "hours": Decimal("0.00000001")}
        ]
        data["optimized"]["resources"] = [
            {"name": str(index), "armSkuName": "Standard_B2s", "quantity": 1000000, "hours": 1000000}
            for index in range(1000)
        ]
        report = compare(catalog, validate_scenario(data, catalog))
        self.assertEqual(report["base"]["compute_usd"], "0.0000000000000001")
        self.assertEqual(report["optimized"]["compute_usd"], "1000000000000000000000")
        self.assertTrue(Decimal(report["potential_savings_percent"]).is_finite())

    def test_cost_increase_is_not_reported_as_positive_savings(self):
        data = copy.deepcopy(self.scenario_data)
        data["base"], data["optimized"] = data["optimized"], data["base"]
        report = self.result(data)
        self.assertEqual(report["potential_savings_usd"], "-87.832")
        self.assertLess(Decimal(report["potential_savings_percent"]), 0)

    def test_quantity_zero_does_not_bill_compute(self):
        data = copy.deepcopy(self.scenario_data)
        for row in data["base"]["resources"]:
            row["quantity"] = 0
        self.assertEqual(self.result(data)["base"]["compute_usd"], "0")

    def test_invalid_scenario_numbers_are_rejected(self):
        for field, invalid in (
            ("hours", -1), ("hours", True), ("hours", "12"),
            ("hours", Decimal("NaN")), ("hours", Decimal("Infinity")),
            ("hours", Decimal("0.000000001")), ("hours", 731),
            ("quantity", -2), ("quantity", False), ("quantity", Decimal("1.5")),
            ("quantity", 1000001),
        ):
            with self.subTest(field=field, invalid=str(invalid)):
                data = copy.deepcopy(self.scenario_data)
                data["base"]["resources"][0][field] = invalid
                with self.assertRaises(ValidationError):
                    validate_scenario(data, self.catalog)

    def test_unknown_sku_currency_region_field_and_duplicate_name_are_rejected(self):
        changes = [
            lambda d: d["base"]["resources"][0].update(armSkuName="Standard_UNKNOWN"),
            lambda d: d.update(currencyCode="BRL"),
            lambda d: d.update(armRegionName="eastus"),
            lambda d: d["base"]["resources"][0].update(unit="hours"),
            lambda d: d["base"]["resources"][1].update(name="app-lab"),
            lambda d: d.update(period_hours=0),
            lambda d: d.update(schema_version=True),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                data = copy.deepcopy(self.scenario_data)
                change(data)
                with self.assertRaises(ValidationError):
                    validate_scenario(data, self.catalog)

    def test_wrong_units_mixed_currency_non_regular_prices_and_bad_provenance(self):
        changes = [
            {"unitOfMeasure": "1 GB"}, {"currencyCode": "BRL"},
            {"productName": "Virtual Machines BS Series Windows"},
            {"meterName": "B1s Spot"}, {"skuName": "B1s Low Priority"},
            {"type": "Reservation"}, {"type": "DevTestConsumption"},
            {"retailPrice": "NaN"}, {"retailPrice": "-0.1"},
            {"retailPrice": True}, {"tierMinimumUnits": "1"},
            {"consulted_at": "2026-11-24T00:00:00Z"}, {"source": "https://example.invalid"},
        ]
        for change in changes:
            with self.subTest(change=change):
                data = copy.deepcopy(self.catalog_data)
                data["items"][0].update(change)
                with self.assertRaises(ValidationError):
                    validate_catalog(data)

    def test_duplicate_catalog_sku_is_rejected(self):
        data = copy.deepcopy(self.catalog_data)
        data["items"].append(copy.deepcopy(data["items"][0]))
        with self.assertRaises(ValidationError):
            validate_catalog(data)

    def test_json_rejects_duplicate_keys_nan_and_infinity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            for text in ('{"hours": 1, "hours": 2}', '{"hours": NaN}', '{"hours": Infinity}'):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValidationError):
                        read_json(path)

    def test_cli_json_from_outside_project_uses_default_local_files(self):
        with tempfile.TemporaryDirectory() as folder:
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", str(ROOT / "estimate.py"), "--json"],
                cwd=folder, capture_output=True, text=True, encoding="utf-8", check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["base"]["compute_usd"], "131.816")
        self.assertEqual(len(report["catalog_sha256"]), 64)

    def test_cli_validation_returns_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            path.write_text('{"schema_version": 1}', encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", str(ROOT / "estimate.py"), str(path)],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Erro:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()

"""Pruebas unitarias del índice Mercadona.

Solo biblioteca estándar. Usan una base de datos SQLite en memoria, por lo que
no tocan `data/mercadona.sqlite3` ni realizan peticiones de red.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "run.py"

spec = importlib.util.spec_from_file_location("run", SRC)
run = importlib.util.module_from_spec(spec)
sys.modules["run"] = run
spec.loader.exec_module(run)


def make_db(rows_by_date):
    """Crea una base en memoria con los productos indicados.

    `rows_by_date` es un dict {fecha: [(product_id, price), ...]}.
    """
    conn = run.db_connect(":memory:")
    config = {"postal_code": "08028", "warehouse": "bcn1"}
    for snapshot_date, rows in rows_by_date.items():
        products = [
            {
                "product_id": pid,
                "name": f"Producto {pid}",
                "price": price,
                "format": "unidad",
                "category": "Test",
                "unit_price": price,
            }
            for pid, price in rows
        ]
        run.save_snapshot(conn, snapshot_date, config, products, "test", force=True)
    return conn


class IndexTests(unittest.TestCase):
    def test_index_equal_weight_base_100(self):
        conn = make_db({
            "2026-01-01": [("1", 1.0), ("2", 2.0)],
            "2026-01-02": [("1", 1.1), ("2", 2.0)],
        })
        result = run.index_change(conn, "2026-01-01", "2026-01-02")
        self.assertIsNotNone(result)
        value, count = result
        # (1.1 + 2.0) / (1.0 + 2.0) * 100 = 103.333...
        self.assertAlmostEqual(value, 103.3333, places=3)
        self.assertEqual(count, 2)

    def test_index_same_date_is_100(self):
        conn = make_db({"2026-01-01": [("1", 1.0), ("2", 2.0)]})
        value, count = run.index_change(conn, "2026-01-01", "2026-01-01")
        self.assertAlmostEqual(value, 100.0, places=6)
        self.assertEqual(count, 2)


class PeriodChangeTests(unittest.TestCase):
    def test_period_change_uses_only_intersection(self):
        # El producto 3 solo existe el segundo día: no debe alterar el cambio.
        conn = make_db({
            "2026-01-01": [("1", 1.0), ("2", 2.0)],
            "2026-01-02": [("1", 1.1), ("2", 2.0), ("3", 100.0)],
        })
        value, count = run.period_change(conn, "2026-01-01", "2026-01-02")
        self.assertAlmostEqual(value, 3.3333, places=3)
        self.assertEqual(count, 2)

    def test_period_change_none_without_intersection(self):
        conn = make_db({
            "2026-01-01": [("1", 1.0)],
            "2026-01-02": [("2", 2.0)],
        })
        self.assertIsNone(run.period_change(conn, "2026-01-01", "2026-01-02"))


class ReferenceTests(unittest.TestCase):
    def test_previous_date(self):
        conn = make_db({"2026-01-01": [("1", 1.0)], "2026-01-02": [("1", 1.0)]})
        self.assertEqual(run.previous_date(conn, "2026-01-02"), "2026-01-01")
        self.assertIsNone(run.previous_date(conn, "2026-01-01"))

    def test_previous_date_month_only(self):
        conn = make_db({
            "2026-01-31": [("1", 1.0)],
            "2026-02-01": [("1", 1.0)],
            "2026-02-15": [("1", 1.0)],
        })
        self.assertEqual(run.previous_date(conn, "2026-02-15", month_only=True), "2026-01-31")

    def test_first_snapshot_of_day(self):
        conn = make_db({
            "2026-01-02T08:00:00": [("1", 1.0)],
            "2026-01-02T16:00:00": [("1", 1.0)],
        })
        self.assertEqual(run.first_snapshot_of_day(conn, "2026-01-02T16:00:00"), "2026-01-02T08:00:00")

    def test_annual_reference_partial_when_no_previous_year(self):
        conn = make_db({"2026-03-01": [("1", 1.0)], "2026-06-01": [("1", 1.0)]})
        reference, partial = run.annual_reference(conn, "2026-06-01")
        self.assertEqual(reference, "2026-03-01")
        self.assertTrue(partial)

    def test_annual_reference_uses_previous_year_when_available(self):
        conn = make_db({"2025-12-31": [("1", 1.0)], "2026-03-01": [("1", 1.0)]})
        reference, partial = run.annual_reference(conn, "2026-03-01")
        self.assertEqual(reference, "2025-12-31")
        self.assertFalse(partial)


class SnapshotTests(unittest.TestCase):
    def test_duplicate_snapshot_is_not_overwritten(self):
        conn = make_db({"2026-01-01": [("1", 1.0)]})
        config = {"postal_code": "08028", "warehouse": "bcn1"}
        saved = run.save_snapshot(
            conn, "2026-01-01", config,
            [{"product_id": "1", "name": "X", "price": 99.0, "format": "u", "category": "c", "unit_price": 99.0}],
            "test",
        )
        self.assertFalse(saved)
        price = conn.execute("SELECT price FROM prices WHERE product_id='1'").fetchone()[0]
        self.assertEqual(price, 1.0)

    def test_completeness_guard_rejects_large_drop(self):
        conn = make_db({"2026-01-01": [(str(i), 1.0) for i in range(100)]})
        config = {"completeness_tolerance": 0.25}
        with self.assertRaises(RuntimeError):
            run.check_completeness(conn, "2026-01-02", 10, config, allow_incomplete=False)
        # Con --allow-incomplete no debe lanzar.
        run.check_completeness(conn, "2026-01-02", 10, config, allow_incomplete=True)

    def test_completeness_guard_allows_small_deviation(self):
        conn = make_db({"2026-01-01": [(str(i), 1.0) for i in range(100)]})
        config = {"completeness_tolerance": 0.25}
        run.check_completeness(conn, "2026-01-02", 95, config, allow_incomplete=False)


class ConfigTests(unittest.TestCase):
    def test_validate_config_fills_defaults(self):
        config = run.validate_config({"warehouse": "bcn1"})
        self.assertEqual(config["warehouse"], "bcn1")
        self.assertEqual(config["request_retries"], 3)
        # Por defecto avisa en lugar de abortar: la tienda prohíbe `/api`.
        self.assertEqual(config["robots_policy"], "warn")

    def test_validate_config_accepts_known_robots_policy(self):
        for policy in run.ROBOTS_POLICIES:
            config = run.validate_config({"warehouse": "bcn1", "robots_policy": policy})
            self.assertEqual(config["robots_policy"], policy)

    def test_validate_config_rejects_unknown_robots_policy(self):
        with self.assertRaises(ValueError):
            run.validate_config({"warehouse": "bcn1", "robots_policy": "quizá"})

    def test_validate_config_migrates_legacy_respect_robots(self):
        """`respect_robots` era un booleano en versiones anteriores."""
        blocked = run.validate_config({"warehouse": "bcn1", "respect_robots": True})
        self.assertEqual(blocked["robots_policy"], "block")
        self.assertNotIn("respect_robots", blocked)
        ignored = run.validate_config({"warehouse": "bcn1", "respect_robots": False})
        self.assertEqual(ignored["robots_policy"], "ignore")
        # `robots_policy` manda si están las dos claves.
        explicit = run.validate_config({"warehouse": "bcn1", "robots_policy": "warn", "respect_robots": True})
        self.assertEqual(explicit["robots_policy"], "warn")

    def test_validate_config_requires_warehouse(self):
        with self.assertRaises(ValueError):
            run.validate_config({"warehouse": ""})

    def test_validate_config_rejects_bad_number(self):
        with self.assertRaises(ValueError):
            run.validate_config({"warehouse": "bcn1", "request_delay_seconds": "abc"})


class DashboardTests(unittest.TestCase):
    def test_dashboard_is_self_contained_and_has_data(self):
        payload = {
            "base_date": "2026-01-01",
            "captured_at": "2026-01-02T10:00:00",
            "zone": "CP 08028 · almacén bcn1",
            "history": [
                {"date": "2026-01-01", "index": 100.0, "products": 2, "intraday": None, "monthly": None, "annual": None},
                {"date": "2026-01-02", "index": 103.3, "products": 2, "intraday": 3.3, "monthly": None, "annual": 3.3},
            ],
            "movers": [{"name": "Leche </script>", "category": "Lácteos", "old_price": 1.0, "new_price": 1.1, "change": 10.0}],
            "annual_partial": True,
        }
        html = run.dashboard_html(payload)
        self.assertIn("Índice Mercadona", html)
        self.assertIn("CP 08028 · almacén bcn1", html)
        self.assertIn("103.3", html)
        # Un cierre de <script> dentro de los datos se neutraliza como <\/script>.
        self.assertIn("<\\/script>", html)

    def test_zone_is_html_escaped(self):
        payload = {
            "base_date": "2026-01-01",
            "captured_at": "2026-01-02T10:00:00",
            "zone": "CP <08028> · almacén bcn1",
            "history": [
                {"date": "2026-01-01", "index": 100.0, "products": 1, "intraday": None, "monthly": None, "annual": None},
            ],
            "movers": [],
            "annual_partial": True,
        }
        html = run.dashboard_html(payload)
        amp = chr(38)
        # El texto visible se escapa; los datos del <script> conservan el original.
        self.assertIn('<span id="zone">CP ' + amp + "lt;08028" + amp + "gt; · almacén bcn1</span>", html)


class RobotsTests(unittest.TestCase):
    """La comprobación de robots.txt no debe romper la captura automática."""

    # Extracto real: la tienda publica `Disallow: /api` (y `Disallow: /`).
    ROBOTS = "User-agent: *\nDisallow: /\nDisallow: /api\n"

    def test_robots_allows_detects_disallowed_api(self):
        self.assertFalse(run.robots_allows(self.ROBOTS, "https://tienda.mercadona.es/api/categories/?lang=es"))

    def test_robots_allows_permits_unlisted_path(self):
        text = "User-agent: *\nDisallow: /privado\n"
        self.assertTrue(run.robots_allows(text, "https://tienda.mercadona.es/api/categories/"))

    def test_enforce_robots_warn_does_not_raise(self):
        config = {"robots_policy": "warn"}
        with mock.patch.object(run, "robots_verdict", return_value=False):
            run.enforce_robots("https://tienda.mercadona.es/api/x", config)

    def test_enforce_robots_block_raises(self):
        config = {"robots_policy": "block"}
        with mock.patch.object(run, "robots_verdict", return_value=False):
            with self.assertRaises(RuntimeError):
                run.enforce_robots("https://tienda.mercadona.es/api/x", config)

    def test_enforce_robots_ignore_skips_lookup(self):
        config = {"robots_policy": "ignore"}
        with mock.patch.object(run, "robots_verdict", return_value=False) as verdict:
            run.enforce_robots("https://tienda.mercadona.es/api/x", config)
        verdict.assert_not_called()

    def test_enforce_robots_continues_when_robots_unreadable(self):
        """Si no se puede leer robots.txt (verdict None), no se aborta ni en modo block."""
        config = {"robots_policy": "block"}
        with mock.patch.object(run, "robots_verdict", return_value=None):
            run.enforce_robots("https://tienda.mercadona.es/api/x", config)


class OutputDirTests(unittest.TestCase):
    def test_output_dir_keeps_generated_files_out_of_docs(self):
        conn = make_db({"2026-01-01": [("1", 1.0)], "2026-01-02": [("1", 1.1)]})
        config = {"postal_code": "08028", "warehouse": "bcn1"}
        with tempfile.TemporaryDirectory() as tmp:
            text = run.make_outputs(conn, "2026-01-02", config, Path(tmp))
            for name in ("post.txt", "index.html", "data.json"):
                self.assertTrue((Path(tmp) / name).is_file(), f"falta {name}")
            self.assertIn("Índice Mercadona", text)
            self.assertIn("Índice: 110,00", text)


if __name__ == "__main__":
    unittest.main()
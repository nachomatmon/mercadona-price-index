#!/usr/bin/env python3
"""Captura precios, calcula un índice y genera un dashboard estático.

Solo biblioteca estándar. Los endpoints de la tienda no son una API pública
documentada: úsalo con moderación y respeta las condiciones del sitio.

Compatibilidad: las tablas y columnas existentes se conservan. Las mejoras se
aplican de forma aditiva mediante `PRAGMA user_version`, de modo que el
histórico ya guardado en `data/mercadona.sqlite3` sigue siendo utilizable.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "out"
DOCS = ROOT / "docs"
API = "https://tienda.mercadona.es/api"
SITE = "https://tienda.mercadona.es"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9",
    "User-Agent": "MercadonaPriceIndex/0.1 (personal research; contact: local)",
}
SCHEMA_VERSION = 1
ROBOTS_POLICIES = ("block", "warn", "ignore")
DEFAULT_CONFIG: dict[str, Any] = {
    "postal_code": "",
    "warehouse": "",
    "language": "es",
    "request_delay_seconds": 0.25,
    "request_retries": 3,
    "request_timeout_seconds": 45,
    "completeness_tolerance": 0.25,
    "robots_policy": "warn",
}

log = logging.getLogger("mercadona")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def validate_config(raw: Any) -> dict[str, Any]:
    """Valida y normaliza la configuración con mensajes claros."""
    if not isinstance(raw, dict):
        raise ValueError("config.json debe contener un objeto JSON.")
    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    config.update(raw)
    if not str(config.get("warehouse") or "").strip():
        raise ValueError("config.json: 'warehouse' es obligatorio y no puede estar vacío.")
    try:
        config["request_delay_seconds"] = max(0.0, float(config["request_delay_seconds"]))
    except (TypeError, ValueError):
        raise ValueError("config.json: 'request_delay_seconds' debe ser numérico.")
    try:
        config["request_retries"] = max(0, int(config["request_retries"]))
    except (TypeError, ValueError):
        raise ValueError("config.json: 'request_retries' debe ser un entero.")
    try:
        config["request_timeout_seconds"] = max(1.0, float(config["request_timeout_seconds"]))
    except (TypeError, ValueError):
        raise ValueError("config.json: 'request_timeout_seconds' debe ser numérico.")
    try:
        config["completeness_tolerance"] = max(0.0, float(config["completeness_tolerance"]))
    except (TypeError, ValueError):
        raise ValueError("config.json: 'completeness_tolerance' debe ser numérico.")
    # Compatibilidad: versiones anteriores usaban el booleano `respect_robots`.
    if "robots_policy" not in raw:
        if raw.get("respect_robots") is False:
            config["robots_policy"] = "ignore"
        elif raw.get("respect_robots") is True:
            config["robots_policy"] = "block"
    config.pop("respect_robots", None)
    config["robots_policy"] = str(config["robots_policy"]).strip().lower()
    if config["robots_policy"] not in ROBOTS_POLICIES:
        raise ValueError("config.json: 'robots_policy' debe ser " + ", ".join(ROBOTS_POLICIES) + ".")
    config["postal_code"] = str(config.get("postal_code") or "")
    config["warehouse"] = str(config["warehouse"]).strip()
    config["language"] = str(config.get("language") or "es")
    return config


def zone_label(config: dict[str, Any]) -> str:
    postal = str(config.get("postal_code") or "sin definir")
    warehouse = str(config.get("warehouse") or "sin definir")
    return f"CP {postal} · almacén {warehouse}"


# --------------------------------------------------------------------------- #
# Descarga (con reintentos, backoff y respeto de robots.txt)
# --------------------------------------------------------------------------- #
def robots_allows(robots_text: str, url: str, user_agent: str = HEADERS["User-Agent"]) -> bool:
    """Interpreta un `robots.txt` ya descargado. Función pura: no toca la red."""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(user_agent, url)


def robots_verdict(url: str, config: dict[str, Any]) -> bool | None:
    """Devuelve True si robots.txt permite la URL, False si la prohíbe y None si no se pudo leer."""
    robots_url = urllib.parse.urljoin(SITE, "/robots.txt")
    try:
        req = urllib.request.Request(robots_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=config["request_timeout_seconds"]) as response:
            text = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        log.warning("No se pudo leer robots.txt (%s); se continúa sin restricción.", error)
        return None
    return robots_allows(text, url)


def enforce_robots(url: str, config: dict[str, Any]) -> None:
    """Aplica `robots_policy`: `block` cancela, `warn` avisa y `ignore` ni consulta.

    La tienda publica `Disallow: /api`, así que bloquear por defecto dejaría la
    captura automática sin efecto. Por eso el valor por defecto avisa en lugar de
    abortar: la decisión queda explícita en config.json y se registra en cada
    ejecución, sin romper el workflow silenciosamente.
    """
    policy = str(config.get("robots_policy", "warn"))
    if policy == "ignore":
        return
    if robots_verdict(url, config) is not False:
        return
    message = f"robots.txt de {SITE} prohíbe {url}"
    if policy == "block":
        raise RuntimeError(f"{message}; captura cancelada (robots_policy=block).")
    log.warning("%s; se continúa porque robots_policy=warn. Revisa las condiciones del sitio.", message)


def request_json(url: str, config: dict[str, Any]) -> Any:
    """GET JSON con reintentos y backoff exponencial."""
    retries = int(config["request_retries"])
    base_delay = max(0.5, float(config["request_delay_seconds"]) or 0.5)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=config["request_timeout_seconds"]) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as error:
            last_error = error
            if attempt >= retries:
                break
            wait = base_delay * (2 ** attempt)
            log.warning("Fallo al pedir %s (%s). Reintento %d/%d en %.1fs", url, error, attempt + 1, retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"No se pudo descargar {url}: {last_error}")


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def as_number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def product_from(raw: dict[str, Any], category: str = "") -> dict[str, Any] | None:
    """Normaliza los formatos de producto observados en el frontend de Mercadona."""
    product_id = raw.get("id") or raw.get("product_id")
    if product_id is None:
        return None
    prices = raw.get("price_instructions") or raw.get("price") or {}
    if not isinstance(prices, dict):
        prices = {}
    price = as_number(prices.get("unit_price") or prices.get("price") or raw.get("price"))
    if price is None:
        return None
    name = raw.get("display_name") or raw.get("name") or raw.get("product_name")
    if not name:
        return None
    return {
        "product_id": str(product_id), "name": str(name).strip(), "price": price,
        "format": str(prices.get("size_format") or raw.get("format") or "unidad"),
        "category": category or str(raw.get("category") or "Sin categoría"),
        "unit_price": as_number(prices.get("bulk_price")),
    }


def category_ids(tree: Any) -> list[tuple[str, str]]:
    """Devuelve únicamente las categorías de segundo nivel.

    La respuesta raíz tiene bloques editoriales que también contienen campos
    `id`; no todos responden a `/categories/<id>/`. La estructura estable es
    `results -> categories`, que es justo la que usa el frontend.
    """
    if not isinstance(tree, dict) or not isinstance(tree.get("results"), list):
        raise RuntimeError("Respuesta inesperada de categorías: falta 'results'.")
    found: dict[str, str] = {}
    for section in tree["results"]:
        if not isinstance(section, dict) or not isinstance(section.get("categories"), list):
            continue
        for category in section["categories"]:
            if not isinstance(category, dict) or category.get("id") is None:
                continue
            found[str(category["id"])] = str(category.get("name") or category.get("display_name") or "Sin categoría")
    if not found:
        raise RuntimeError("No se encontraron categorías de segundo nivel.")
    return list(found.items())


def fetch_catalog(config: dict[str, Any]) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"lang": config.get("language", "es"), "wh": config.get("warehouse", "")})
    root_url = f"{API}/categories/?{params}"
    enforce_robots(root_url, config)
    root = request_json(root_url, config)
    items: dict[str, dict[str, Any]] = {}
    categories = category_ids(root)
    failures = 0
    for category_id, category_name in categories:
        time.sleep(float(config.get("request_delay_seconds", 0.25)))
        url = f"{API}/categories/{urllib.parse.quote(category_id)}/?{params}"
        try:
            payload = request_json(url, config)
        except RuntimeError as error:
            failures += 1
            log.warning("Categoría '%s' omitida: %s", category_name, error)
            continue
        for raw in walk(payload):
            product = product_from(raw, category_name)
            if product:
                items[product["product_id"]] = product
    if not items:
        raise RuntimeError("No se extrajo ningún producto. La estructura de la tienda puede haber cambiado.")
    if failures:
        log.warning("%d de %d categorías no se pudieron descargar.", failures, len(categories))
    return sorted(items.values(), key=lambda p: p["product_id"])


# --------------------------------------------------------------------------- #
# Base de datos (migraciones aditivas, nunca destructivas)
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    postal_code TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    snapshot_date TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    format TEXT NOT NULL,
    price REAL NOT NULL,
    bulk_price REAL,
    PRIMARY KEY(snapshot_date, product_id),
    FOREIGN KEY(snapshot_date) REFERENCES snapshots(snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_prices_product_date ON prices(product_id, snapshot_date);
"""


def _init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def db_connect(database_path: Path | str | None = None) -> sqlite3.Connection:
    if database_path is not None and str(database_path) == ":memory:":
        return _init_db(sqlite3.connect(":memory:"))
    path = Path(database_path) if database_path else (DATA / "mercadona.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    return _init_db(sqlite3.connect(path))


def snapshot_exists(conn: sqlite3.Connection, snapshot_date: str) -> bool:
    return conn.execute("SELECT 1 FROM snapshots WHERE snapshot_date = ?", (snapshot_date,)).fetchone() is not None


def latest_snapshot(conn: sqlite3.Connection, before: str | None = None) -> str | None:
    if before is None:
        return conn.execute("SELECT max(snapshot_date) FROM snapshots").fetchone()[0]
    return conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ?", (before,)).fetchone()[0]


def latest_product_count(conn: sqlite3.Connection, before: str | None = None) -> int | None:
    date_id = latest_snapshot(conn, before)
    if not date_id:
        return None
    return conn.execute("SELECT count(*) FROM prices WHERE snapshot_date = ?", (date_id,)).fetchone()[0]


def check_completeness(conn: sqlite3.Connection, snapshot_date: str, count: int, config: dict[str, Any], allow_incomplete: bool) -> None:
    """Evita guardar capturas claramente incompletas que sesgarían el índice."""
    if allow_incomplete:
        log.warning("Comprobación de integridad omitida (--allow-incomplete).")
        return
    previous = latest_product_count(conn, before=snapshot_date)
    if not previous:
        return
    tolerance = float(config.get("completeness_tolerance", 0.25))
    deviation = abs(count - previous) / previous
    if deviation > tolerance:
        raise RuntimeError(
            f"Captura incompleta: {count} productos frente a {previous} "
            f"(desviación {deviation:.1%} > {tolerance:.0%}). Usa --allow-incomplete para forzarla."
        )


def save_snapshot(conn: sqlite3.Connection, snapshot_date: str, config: dict[str, Any], products: list[dict[str, Any]], source: str, force: bool = False) -> bool:
    if snapshot_exists(conn, snapshot_date):
        if not force:
            log.warning("La captura %s ya existe; se conserva y no se sobrescribe.", snapshot_date)
            return False
        log.warning("Sobrescribiendo la captura %s (--force).", snapshot_date)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
            (snapshot_date, datetime.now().isoformat(timespec="seconds"), str(config.get("postal_code", "")), str(config.get("warehouse", "")), source),
        )
        conn.execute("DELETE FROM prices WHERE snapshot_date = ?", (snapshot_date,))
        conn.executemany(
            "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(snapshot_date, p["product_id"], p["name"], p["category"], p["format"], p["price"], p["unit_price"]) for p in products],
        )
    return True


# --------------------------------------------------------------------------- #
# Cálculo del índice
# --------------------------------------------------------------------------- #
def previous_date(conn: sqlite3.Connection, current: str, month_only: bool = False) -> str | None:
    if month_only:
        return conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ? AND substr(snapshot_date,1,7) < substr(?,1,7)", (current, current)).fetchone()[0]
    return conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ?", (current,)).fetchone()[0]


def calendar_day(snapshot_id: str) -> date:
    return date.fromisoformat(snapshot_id[:10])


def first_snapshot_of_day(conn: sqlite3.Connection, current: str) -> str | None:
    return conn.execute("SELECT min(snapshot_date) FROM snapshots WHERE substr(snapshot_date, 1, 10) = ?", (current[:10],)).fetchone()[0]


def annual_reference(conn: sqlite3.Connection, current: str) -> tuple[str | None, bool]:
    """Último dato anterior al año; si no existe, primera captura del año.

    El booleano indica que la serie anual aún es parcial por no haber datos del
    año anterior.
    """
    start = f"{current[:4]}-01-01"
    before_year = conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ?", (start,)).fetchone()[0]
    if before_year:
        return before_year, False
    return conn.execute("SELECT min(snapshot_date) FROM snapshots WHERE substr(snapshot_date, 1, 4) = ?", (current[:4],)).fetchone()[0], True


def index_change(conn: sqlite3.Connection, base: str, current: str) -> tuple[float, int] | None:
    row = conn.execute("""
      SELECT sum(c.price) * 100.0 / sum(b.price), count(*)
      FROM prices b JOIN prices c ON b.product_id=c.product_id
      WHERE b.snapshot_date=? AND c.snapshot_date=?
    """, (base, current)).fetchone()
    return (float(row[0]), int(row[1])) if row[0] is not None else None


def period_change(conn: sqlite3.Connection, previous: str, current: str) -> tuple[float, int] | None:
    """Cambio de una misma cesta entre dos fechas, usando solo la intersección.

    Así una baja/alta temporal de catálogo no altera artificialmente el cambio
    diario o mensual. El índice de nivel conserva su base inicial por separado.
    """
    row = conn.execute("""
      SELECT sum(c.price) * 100.0 / sum(p.price) - 100.0, count(*)
      FROM prices p JOIN prices c ON p.product_id=c.product_id
      WHERE p.snapshot_date=? AND c.snapshot_date=?
    """, (previous, current)).fetchone()
    return (float(row[0]), int(row[1])) if row[0] is not None else None


def price_movers(conn: sqlite3.Connection, previous: str | None, current: str) -> list[dict[str, Any]]:
    if previous is None:
        return []
    rows = conn.execute("""
      SELECT c.name, c.category, p.price AS old_price, c.price AS new_price,
             (c.price / p.price - 1) * 100.0 AS change
      FROM prices p JOIN prices c ON p.product_id = c.product_id
      WHERE p.snapshot_date = ? AND c.snapshot_date = ?
      ORDER BY abs(c.price / p.price - 1) DESC, c.name
      LIMIT 12
    """, (previous, current)).fetchall()
    return [{"name": r[0], "category": r[1], "old_price": r[2], "new_price": r[3], "change": r[4]} for r in rows]


# --------------------------------------------------------------------------- #
# Dashboard estático
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Análisis adicional: 7 días, categorías y productos
# --------------------------------------------------------------------------- #
def week_reference(conn: sqlite3.Connection, current: str) -> str | None:
    """Última captura con al menos 7 días de antigüedad respecto a la actual."""
    row = conn.execute(
        "SELECT max(snapshot_date) FROM snapshots WHERE date(snapshot_date) <= date(?, '-7 days')",
        (current[:10],),
    ).fetchone()[0]
    return row


def category_changes(conn: sqlite3.Connection, base: str, current: str, limit: int = 10) -> list[dict[str, Any]]:
    """Variación acumulada por categoría usando solo productos comparables."""
    rows = conn.execute("""
      SELECT c.category, count(*), (sum(c.price)/sum(p.price)-1)*100.0
      FROM prices p JOIN prices c
        ON p.product_id=c.product_id AND p.snapshot_date=? AND c.snapshot_date=?
      WHERE p.price > 0
      GROUP BY c.category
      ORDER BY abs((sum(c.price)/sum(p.price)-1)) DESC
      LIMIT ?
    """, (base, current, limit)).fetchall()
    return [{"category": r[0], "count": int(r[1]), "change": float(r[2])} for r in rows]


def product_changes(conn: sqlite3.Connection, base: str, current: str) -> list[list[Any]]:
    """Variación acumulada por producto para toda la cesta comparable.

    Formato compacto [id, nombre, categoría, precio_base, precio_actual, %]
    para que el HTML siga siendo ligero con miles de productos.
    """
    rows = conn.execute("""
      SELECT c.product_id, c.name, c.category, p.price, c.price,
             (c.price/p.price-1)*100.0
      FROM prices p JOIN prices c
        ON p.product_id=c.product_id AND p.snapshot_date=? AND c.snapshot_date=?
      WHERE p.price > 0
      ORDER BY abs((c.price/p.price-1)) DESC, c.name
    """, (base, current)).fetchall()
    return [[r[0], r[1], r[2], round(r[3], 4), round(r[4], 4), round(r[5], 4)] for r in rows]


# --------------------------------------------------------------------------- #
# Dashboard estático
# --------------------------------------------------------------------------- #
DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Índice Mercadona</title>
<meta name="description" content="Índice de precios independiente de la tienda online de Mercadona.">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f5f7f3">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0d1512">
<meta property="og:title" content="Índice Mercadona">
<meta property="og:description" content="Seguimiento independiente de precios: índice, inflación y variación por producto.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3' fill='%230d6b4e'/%3E%3Ctext x='8' y='12.5' font-size='11' text-anchor='middle' fill='%23fff' font-family='sans-serif'%3E%E2%82%AC%3C/text%3E%3C/svg%3E">
<style>
:root{color-scheme:light dark;--ink:#173b32;--muted:#5e716b;--surface:#fff;--paper:#f5f7f3;--green:#0d6b4e;--red:#bb3c35;--line:#dce4df;--blue:#2563eb;--amber:#d97706}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1120px;margin:auto;padding:32px 20px 40px}
header{display:flex;justify-content:space-between;gap:20px;align-items:start;border-bottom:1px solid var(--line);padding-bottom:22px}
h1{font-size:clamp(28px,5vw,45px);margin:0 0 6px;letter-spacing:-.04em}h2{font-size:20px;margin:0 0 16px}h3{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600}
p{margin:0;color:var(--muted)}.tag{color:var(--green);font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.09em}
.updated{text-align:right;font-size:14px;white-space:nowrap}

.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}
.metric,.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px}.metric{padding:18px}
.metric span{display:block;color:var(--muted);font-size:13px}.metric strong{display:block;font-size:30px;letter-spacing:-.04em;margin-top:7px;font-variant-numeric:tabular-nums}
.spark{display:block;width:100%;height:28px;margin-top:10px;opacity:.9}
.spark polyline{fill:none;stroke:var(--green);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.positive{color:var(--red)}.negative{color:var(--green)}
.layout{display:grid;grid-template-columns:1.45fr .9fr;gap:18px}.panel{padding:22px}
#chartPanel{position:relative}
#chart{display:block;width:100%;height:auto;overflow:visible}
.axis{stroke:var(--line);stroke-width:1}.grid{stroke:var(--line);stroke-width:.6;opacity:.6;stroke-dasharray:2 4}
.chart-week{fill:none;stroke:var(--green);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
.chart-monthly{fill:none;stroke:var(--blue);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}
.chart-annual{fill:none;stroke:var(--amber);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.area-annual{fill:var(--amber);opacity:.10;stroke:none}
.end-dot.c-week{fill:var(--green)}.end-dot.c-monthly{fill:var(--blue)}.end-dot.c-annual{fill:var(--amber)}
.end-label{font-size:11px;font-weight:700}.end-label.c-week{fill:var(--green)}.end-label.c-monthly{fill:var(--blue)}.end-label.c-annual{fill:var(--amber)}
.crosshair{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.7}
.chart-label{fill:var(--muted);font-size:12px}
#legend{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 12px;font-size:13px;color:var(--muted)}
.legend-item{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;padding:2px 4px;border-radius:6px}
.legend-item.off{opacity:.35;text-decoration:line-through}
.legend-item i{width:14px;height:3px;border-radius:2px;display:inline-block}
i.c-week{background:var(--green)}i.c-monthly{background:var(--blue)}i.c-annual{background:var(--amber)}
#ranges{display:flex;gap:6px;margin:0 0 12px}
#ranges button{font:inherit;font-size:13px;padding:4px 10px;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;cursor:pointer}
#ranges button.on{background:var(--green);border-color:var(--green);color:#fff;font-weight:600}
#tip{position:absolute;display:none;pointer-events:none;background:var(--ink);color:var(--paper);border-radius:8px;padding:8px 11px;font-size:12.5px;line-height:1.55;box-shadow:0 6px 18px rgba(0,0,0,.25);z-index:5;white-space:nowrap}
#tip b{font-size:12px}

table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:11px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600}th.sortable{cursor:pointer}th.sortable:hover{color:var(--ink)}
td:last-child,th:last-child{text-align:right;font-variant-numeric:tabular-nums}
.num{font-variant-numeric:tabular-nums;text-align:right}
.up{color:var(--red);font-weight:700}.down{color:var(--green);font-weight:700}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:700}
.pill.up{background:rgba(187,60,53,.12)}.pill.down{background:rgba(13,107,78,.12)}
.mbar{height:3px;border-radius:2px;margin-top:5px;background:var(--line);overflow:hidden}
.mbar i{display:block;height:100%;border-radius:2px}
.mbar i.up{background:var(--red)}.mbar i.down{background:var(--green)}
.catrow{display:grid;grid-template-columns:minmax(90px,34%) 1fr auto;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.catrow:last-child{border-bottom:0}
.catbar{height:8px;border-radius:4px;background:var(--line);overflow:hidden}
.catbar i{display:block;height:100%;border-radius:4px}
.catbar i.up{background:var(--red)}.catbar i.down{background:var(--green)}
.catpct{font-variant-numeric:tabular-nums;font-weight:700;min-width:64px;text-align:right}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 14px;font-size:13.5px;color:var(--muted)}
.controls input[type=search],.controls select{font:inherit;font-size:13.5px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink)}
.controls input[type=search]{min-width:220px}
.controls label{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
.btn{font:inherit;font-size:13.5px;padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink);cursor:pointer}
.btn:hover{border-color:var(--muted)}
#pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:12px;font-size:13px;color:var(--muted)}
#pager button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--surface);border-radius:8px;cursor:pointer;color:var(--ink)}
#pager button:disabled{opacity:.4;cursor:default}
.method{margin-top:18px;padding-top:18px;border-top:1px solid var(--line);font-size:14px;line-height:1.5}
footer{margin-top:26px;padding-top:16px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
footer a{color:var(--green);text-decoration:none}footer a:hover{text-decoration:underline}
.empty{padding:34px 10px;text-align:center;color:var(--muted);font-size:14px}
@media(max-width:760px){main{padding:22px 14px}header{display:block}.updated{text-align:left;margin-top:12px;white-space:normal}
.metrics{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.metric strong{font-size:25px}.controls input[type=search]{min-width:0;flex:1}}
@media(prefers-color-scheme:dark){:root{--ink:#e6f0eb;--muted:#a7b7b0;--surface:#17221e;--paper:#0d1512;--green:#4bc090;--red:#ff8b83;--line:#304139;--blue:#7aa2ff;--amber:#f0b35c}}
</style></head>
<body>

<main>
<header>
  <div><div class="tag">Seguimiento independiente</div><h1>Índice Mercadona</h1><p>Precios online · <span id="zone">__ZONE__</span></p></div>
  <p class="updated" id="updated"></p>
</header>
<section class="metrics" aria-label="Indicadores principales">
  <div class="metric"><span>Índice base 100</span><strong id="index">—</strong><svg class="spark" id="spark-index" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"></svg></div>
  <div class="metric"><span>Variación 7 días</span><strong id="week">—</strong><svg class="spark" id="spark-week" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"></svg></div>
  <div class="metric"><span>Inflación mensual</span><strong id="monthly">—</strong><svg class="spark" id="spark-monthly" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"></svg></div>
  <div class="metric"><span>Inflación anual acumulada</span><strong id="annual">—</strong><svg class="spark" id="spark-annual" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true"></svg></div>
</section>
<section class="layout">
  <article class="panel" id="chartPanel">
    <h2>Evolución de la inflación</h2>
    <div id="legend"></div>
    <div id="ranges"></div>
    <svg id="chart" viewBox="0 0 680 300" role="img" aria-label="Evolución temporal de la inflación a 7 días, mensual y anual"></svg>
    <p id="chart-note"></p>
    <div id="tip"></div>
  </article>
  <article class="panel">
    <h2>Mayores cambios desde la última captura</h2>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Producto</th><th>Antes</th><th>Ahora</th><th>Variación</th></tr></thead>
        <tbody id="movers"></tbody>
      </table>
    </div>
  </article>
</section>
<section class="panel" style="margin-top:18px">
  <h2>Variación por categoría <small style="font-weight:400;color:var(--muted)">· acumulada desde la base</small></h2>
  <div id="cats"></div>
</section>
<section class="panel" style="margin-top:18px" id="products-panel">
  <h2>Inflación por producto <small style="font-weight:400;color:var(--muted)">· acumulada desde la base (<span id="pcount">0</span> productos)</small></h2>
  <div class="controls">
    <input type="search" id="q" placeholder="Buscar producto o categoría…">
    <label><input type="checkbox" id="onlyChanges"> Solo cambios</label>
    <select id="pageSize"><option>25</option><option selected>50</option><option>100</option></select>
    <button class="btn" id="csv" type="button">Descargar CSV</button>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th class="sortable" data-k="1">Producto</th>
        <th class="sortable" data-k="2">Categoría</th>
        <th class="sortable num" data-k="3">Precio base</th>
        <th class="sortable num" data-k="4">Ahora</th>
        <th class="sortable num" data-k="5">Variación</th>
      </tr></thead>
      <tbody id="productsBody"></tbody>
    </table>
  </div>
  <div id="pager">
    <button id="prev" type="button">‹ Anterior</button>
    <span id="pageInfo"></span>
    <button id="next" type="button">Siguiente ›</button>
  </div>
</section>
<section class="panel method">
  <h2>Cómo se calcula</h2>
  <p>Se realiza una captura diaria de la tienda online. La variación de 7 días compara con la última captura disponible de hace al menos una semana; la mensual con el último dato del mes anterior; y la anual con el último dato previo al 1 de enero (marcada como parcial si aún no hay historia del año anterior). El índice usa una cesta de peso igual por producto y formato de venta, comparando solo productos presentes en ambas fechas; no es un índice oficial.</p>
</section>
<footer>
  <span>Datos: <a href="data.json" download>data.json</a> · Código y metodología en el repositorio.</span>
  <span>Sin backend, sin CDN, sin cookies.</span>
</footer>
</main>

<script>
const data = __DATA__;
const fmtEuro = n => new Intl.NumberFormat('es-ES',{style:'currency',currency:'EUR'}).format(n);
const fmtNum = n => new Intl.NumberFormat('es-ES',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n);
const fmtPct = n => n==null ? 'Sin dato' : ((n>=0?'+':'') + fmtNum(n) + '%');
const ESC = {'&':'&#38;','<':'&#60;','>':'&#62;','"':'&#34;',"'":'&#39;'};
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ESC[c]);
const $ = id => document.getElementById(id);
function setMetric(id, value){
  const el = $(id);
  el.textContent = fmtPct(value);
  el.className = value>0 ? 'positive' : (value<0 ? 'negative' : '');
}
function sparkline(id, series){
  const svg = $(id); if (!svg) return;
  const vals = series.filter(v => v!=null);
  if (vals.length < 2){ svg.innerHTML=''; return; }
  const mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
  const span = (mx-mn)||1;
  const pts = [];
  series.forEach((v,i)=>{ if(v!=null){ pts.push(((i/(series.length-1))*118+1).toFixed(1)+','+(27-((v-mn)/span)*24).toFixed(1)); } });
  svg.innerHTML = '<polyline points="'+pts.join(' ')+'"/>';
}
const last = data.history[data.history.length-1];
$('zone').textContent = data.zone;
$('updated').textContent = 'Actualizado: ' + new Date(data.captured_at).toLocaleString('es-ES');
$('index').textContent = fmtNum(last.index);
setMetric('week', data.week!=null ? data.week : (last.week!=null?last.week:null));
setMetric('monthly', last.monthly);
setMetric('annual', last.annual);
sparkline('spark-index', data.history.map(h=>h.index));
sparkline('spark-week', data.history.map(h=>h.week));
sparkline('spark-monthly', data.history.map(h=>h.monthly));
sparkline('spark-annual', data.history.map(h=>h.annual));

const svg = $('chart'), tip = $('tip');
const W=680,H=300,L=58,R=56,T=16,B=38;
const SERIES = [
  {key:'week', label:'7 días', cls:'chart-week', dot:'c-week'},
  {key:'monthly', label:'Mensual', cls:'chart-monthly', dot:'c-monthly'},
  {key:'annual', label:'Anual', cls:'chart-annual', dot:'c-annual'}
];
const hidden = new Set();
let rangeDays = null;
const RANGES = [['Todo',null],['1 año',365],['90 días',90],['30 días',30]];
$('ranges').innerHTML = RANGES.map((r,i)=>'<button type="button" data-i="'+i+'" class="'+(i===0?'on':'')+'">'+r[0]+'</button>').join('');
$('ranges').addEventListener('click', e=>{
  const b = e.target.closest('button'); if(!b) return;
  rangeDays = RANGES[+b.dataset.i][1];
  document.querySelectorAll('#ranges button').forEach(x=>x.classList.toggle('on', x===b));
  render();
});
$('legend').innerHTML = SERIES.map(s=>'<span class="legend-item" data-k="'+s.key+'"><i class="'+s.cls+'"></i>'+s.label+'</span>').join('');
$('legend').addEventListener('click', e=>{
  const li = e.target.closest('.legend-item'); if(!li) return;
  const k = li.dataset.k;
  if (hidden.has(k)) { hidden.delete(k); li.classList.remove('off'); }
  else { hidden.add(k); li.classList.add('off'); }
  render();
});
function currentHistory(){
  if (!rangeDays) return data.history;
  const cutoff = new Date(last.date); cutoff.setDate(cutoff.getDate() - rangeDays);
  return data.history.filter(h => new Date(h.date) >= cutoff);
}
function render(){
  const hist = currentHistory();
  const active = SERIES.filter(s => !hidden.has(s.key) && hist.some(h=>h[s.key]!=null));
  if (hist.length < 2){
    svg.innerHTML = '<text class="chart-label" x="'+L+'" y="140">Se necesitan al menos dos capturas para dibujar la serie.</text>';
    $('chart-note').textContent = hist.length + ' captura(s) disponible(s).';
    return;
  }
  const values = [];
  active.forEach(s => hist.forEach(h => { if (h[s.key]!=null) values.push(h[s.key]); }));
  let lo, hi;
  if (!values.length){ lo=-1; hi=1; }
  else {
    const mn = Math.min.apply(null, values), mx = Math.max.apply(null, values);
    if (mn===mx){ lo=mn-1; hi=mx+1; } else { const pad=Math.max(.1,(mx-mn)*.15); lo=mn-pad; hi=mx+pad; }
  }
  const N = hist.length;
  const X = i => L + (W-L-R) * (i/(N-1));
  const Y = v => T + (H-T-B) * (1 - (v-lo)/(hi-lo));
  let c = '';
  for (let g=0; g<=3; g++){
    const v = lo + (hi-lo)*(g/3), y = Y(v);
    c += '<line class="grid" x1="'+L+'" y1="'+y.toFixed(1)+'" x2="'+(W-R)+'" y2="'+y.toFixed(1)+'"/>';
    c += '<text class="chart-label" x="4" y="'+(y+4).toFixed(1)+'">'+fmtNum(v)+'%</text>';
  }
  const ticks = Math.min(6, N);
  for (let t=0; t<ticks; t++){
    const i = Math.round((N-1)*t/(ticks-1));
    const d = hist[i].date.slice(5,10).replace('-','/');
    c += '<text class="chart-label" text-anchor="middle" x="'+X(i).toFixed(1)+'" y="'+(H-10)+'">'+d+'</text>';
  }
  const annualS = SERIES.find(s=>s.key==='annual');
  if (active.includes(annualS)){
    const pts = [];
    hist.forEach((h,i)=>{ if(h.annual!=null) pts.push(X(i).toFixed(1)+','+Y(h.annual).toFixed(1)); });
    if (pts.length>1) c += '<polygon class="area-annual" points="'+L+','+Y(Math.max(0,lo)).toFixed(1)+' '+pts.join(' ')+' '+(W-R)+','+Y(Math.max(0,lo)).toFixed(1)+'"/>';
  }
  active.forEach(s=>{
    const pts=[];
    hist.forEach((h,i)=>{ if(h[s.key]!=null) pts.push(X(i).toFixed(1)+','+Y(h[s.key]).toFixed(1)); });
    c += '<polyline class="'+s.cls+'" points="'+pts.join(' ')+'"/>';
    const li = N-1, lv = hist[li][s.key];
    if (lv!=null){
      c += '<circle class="end-dot '+s.dot+'" cx="'+X(li).toFixed(1)+'" cy="'+Y(lv).toFixed(1)+'" r="3.5"/>';
      c += '<text class="end-label '+s.dot+'" x="'+(X(li)+6).toFixed(1)+'" y="'+(Y(lv)+4).toFixed(1)+'">'+fmtNum(lv)+'%</text>';
    }
  });
  svg.innerHTML = c;
  svg.onmousemove = ev => {
    const rect = svg.getBoundingClientRect();
    const px = (ev.clientX-rect.left)/rect.width*W;
    let bi=0, bd=1e9;
    for (let i=0;i<N;i++){ const d=Math.abs(X(i)-px); if(d<bd){bd=d;bi=i;} }
    const h = hist[bi];
    let html = '<b>'+h.date.slice(0,10)+'</b>';
    SERIES.forEach(s=>{ if(h[s.key]!=null) html += '<br>'+s.label+': '+fmtPct(h[s.key]); });
    tip.innerHTML = html;
    tip.style.display='block';
    const r2 = $('chartPanel').getBoundingClientRect();
    tip.style.left = Math.min(ev.clientX-r2.left+14, r2.width-170)+'px';
    tip.style.top = Math.min(ev.clientY-r2.top+14, r2.height-96)+'px';
    svg.innerHTML = c + '<line class="crosshair" x1="'+X(bi).toFixed(1)+'" y1="'+T+'" x2="'+X(bi).toFixed(1)+'" y2="'+(H-B)+'"/>';
  };
  svg.onmouseleave = () => { tip.style.display='none'; render(); };
  $('chart-note').textContent = (data.annual_partial?'Anual parcial desde la primera captura disponible del año. ':'Anual desde el último dato previo al 1 de enero. ') + N + ' capturas.';
}
render();

const M = data.movers||[];
const maxAbs = Math.max.apply(null, M.map(m=>Math.abs(m.change)).concat([1]));
$('movers').innerHTML = M.length ? M.map(m=>{
  const cls = m.change>0?'up':'down';
  return '<tr><td><strong>'+esc(m.name)+'</strong><br><small>'+esc(m.category)+'</small></td>'
    + '<td class="num">'+fmtEuro(m.old_price)+'</td><td class="num">'+fmtEuro(m.new_price)+'</td>'
    + '<td><span class="pill '+cls+'">'+fmtPct(m.change)+'</span><div class="mbar"><i class="'+cls+'" style="width:'+(Math.abs(m.change)/maxAbs*100).toFixed(0)+'%"></i></div></td></tr>';
}).join('') : '<tr><td colspan="4" class="empty">Aún no hay una captura anterior para comparar.</td></tr>';
const CATS = data.categories||[];
const cmax = Math.max.apply(null, CATS.map(x=>Math.abs(x.change)).concat([1]));
$('cats').innerHTML = CATS.length ? CATS.map(c2=>{
  const cls = c2.change>0?'up':'down';
  return '<div class="catrow"><span>'+esc(c2.category)+'</span>'
    + '<div class="catbar"><i class="'+cls+'" style="width:'+(Math.abs(c2.change)/cmax*100).toFixed(1)+'%"></i></div>'
    + '<span class="catpct '+cls+'">'+fmtPct(c2.change)+' <small style="font-weight:400;color:var(--muted)">('+c2.count+')</small></span></div>';
}).join('') : '<div class="empty">Sin datos comparables todavía.</div>';
const P = data.products||[];
let sortK = 5, sortDir = -1, page = 0, psize = 50;
function view(){
  const q = $('q').value.trim().toLowerCase();
  const only = $('onlyChanges').checked;
  let rows = P.filter(r => (!only || r[3]!==r[4]) && (!q || (r[1]+' '+r[2]+' '+r[0]).toLowerCase().includes(q)));
  rows.sort((a,b)=>{
    const va=a[sortK], vb=b[sortK];
    if (typeof va === 'string' || typeof vb === 'string') return String(va).localeCompare(String(vb),'es')*sortDir;
    return (va-vb)*sortDir;
  });
  return rows;
}
function renderTable(){
  const rows = view();
  const pages = Math.max(1, Math.ceil(rows.length/psize));
  if (page >= pages) page = pages-1;
  const slice = rows.slice(page*psize, page*psize+psize);
  $('pcount').textContent = P.length.toLocaleString('es-ES');
  $('productsBody').innerHTML = slice.length ? slice.map(r=>
    '<tr><td><strong>'+esc(r[1])+'</strong><br><small style="color:var(--muted)">'+esc(r[0])+'</small></td>'
    + '<td>'+esc(r[2])+'</td><td class="num">'+fmtEuro(r[3])+'</td><td class="num">'+fmtEuro(r[4])+'</td>'
    + '<td><span class="pill '+(r[5]>0?'up':(r[5]<0?'down':''))+'">'+fmtPct(r[5])+'</span></td></tr>'
  ).join('') : '<tr><td colspan="5" class="empty">Ningún producto coincide con la búsqueda.</td></tr>';
  $('pageInfo').textContent = 'Página '+(page+1)+' de '+pages+' · '+rows.length.toLocaleString('es-ES')+' productos';
  $('prev').disabled = page===0;
  $('next').disabled = page>=pages-1;
}
$('q').addEventListener('input', ()=>{ page=0; renderTable(); });
$('onlyChanges').addEventListener('change', ()=>{ page=0; renderTable(); });
$('pageSize').addEventListener('change', e=>{ psize=+e.target.value; page=0; renderTable(); });
$('prev').addEventListener('click', ()=>{ if(page>0){page--; renderTable();} });
$('next').addEventListener('click', ()=>{ page++; renderTable(); });
document.querySelectorAll('#products-panel th.sortable').forEach(th=>{
  th.addEventListener('click', ()=>{
    const k = +th.dataset.k;
    if (sortK===k) sortDir = -sortDir; else { sortK=k; sortDir = (k===1||k===2)?1:-1; }
    renderTable();
  });
});
$('csv').addEventListener('click', ()=>{
  const nl = String.fromCharCode(10), sep = String.fromCharCode(59);
  const head = ['id','producto','categoria','precio_base','precio_actual','variacion_pct'].join(sep);
  const lines = view().map(r=>[r[0],r[1],r[2],r[3],r[4],r[5]].join(sep));
  const blob = new Blob([String.fromCharCode(0xFEFF)+head+nl+lines.join(nl)], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'inflacion-por-producto.csv';
  a.click();
  URL.revokeObjectURL(a.href);
});
renderTable();
</script>
</html>"""


def dashboard_html(payload: dict[str, Any]) -> str:
    """Dashboard autocontenido: no necesita backend, CDN ni cookies."""
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        DASHBOARD_TEMPLATE
        .replace("__DATA__", data)
        .replace("__ZONE__", html.escape(str(payload.get("zone", ""))))
    )

# --------------------------------------------------------------------------- #
# Salidas
# --------------------------------------------------------------------------- #
def make_outputs(conn: sqlite3.Connection, current: str, config: dict[str, Any], output_dir: Path | None = None) -> str:
    base = conn.execute("SELECT min(snapshot_date) FROM snapshots").fetchone()[0]
    current_index = index_change(conn, base, current)
    if current_index is None:
        raise RuntimeError("No hay productos comparables entre la base y la fecha solicitada.")
    prev = previous_date(conn, current)
    week_base = week_reference(conn, current)
    week = None
    if week_base:
        result = period_change(conn, week_base, current)
        if result:
            week = result[0]
    monthly = None
    previous_month = previous_date(conn, current, month_only=True)
    if previous_month:
        result = period_change(conn, previous_month, current)
        if result:
            monthly = result[0]
    annual_base, annual_partial = annual_reference(conn, current)
    annual = None
    if annual_base:
        result = period_change(conn, annual_base, current)
        if result:
            annual = result[0]

    def pct(value: float | None) -> str:
        return "sin dato" if value is None else f"{value:+.2f}%".replace(".", ",")

    lines = [
        f"Índice Mercadona — {calendar_day(current).strftime('%d/%m/%Y')}", "",
        f"Índice: {current_index[0]:.2f}".replace(".", ","),
        f"Variación 7 días: {pct(week)}",
        f"Inflación mensual: {pct(monthly)}",
        f"Inflación anual acumulada: {pct(annual)}", "",
        f"Cesta comparable: {current_index[1]} productos",
        f"Zona: {zone_label(config)}",
        "Metodología: cesta de peso igual por producto.",
    ]
    text = "\n".join(lines)
    # `--output-dir` permite generar las salidas en otra carpeta para no
    # sobrescribir el dashboard publicado al hacer pruebas con fixtures.
    if output_dir:
        target = Path(output_dir)
        post_path = target / "post.txt"
        index_path = target / "index.html"
        data_path = target / "data.json"
    else:
        post_path = OUT / "post.txt"
        index_path = DOCS / "index.html"
        data_path = DOCS / "data.json"
    for path in {post_path, index_path, data_path}:
        path.parent.mkdir(parents=True, exist_ok=True)
    post_path.write_text(text, encoding="utf-8")

    history = []
    for (snapshot_date,) in conn.execute("SELECT snapshot_date FROM snapshots ORDER BY snapshot_date"):
        level = index_change(conn, base, snapshot_date)
        week_ref = week_reference(conn, snapshot_date)
        change_week = period_change(conn, week_ref, snapshot_date) if week_ref else None
        before_month = previous_date(conn, snapshot_date, month_only=True)
        change_month = period_change(conn, before_month, snapshot_date) if before_month else None
        year_base, partial = annual_reference(conn, snapshot_date)
        change_year = period_change(conn, year_base, snapshot_date) if year_base else None
        if level:
            history.append({
                "date": snapshot_date,
                "index": round(level[0], 4),
                "products": level[1],
                "week": round(change_week[0], 4) if change_week else None,
                "monthly": round(change_month[0], 4) if change_month else None,
                "annual": round(change_year[0], 4) if change_year else None,
            })

    payload = {
        "base_date": base,
        "captured_at": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds"),
        "zone": zone_label(config),
        "history": history[-365:],
        "movers": price_movers(conn, prev, current),
        "annual_partial": annual_partial,
        "week": week,
    }
    # data.json se mantiene ligero (sin el detalle por producto); el detalle
    # viaja embebido en index.html para que el dashboard siga siendo un único
    # archivo autocontenido sin peticiones adicionales.
    dashboard_payload = dict(payload)
    dashboard_payload["categories"] = category_changes(conn, base, current, limit=12)
    dashboard_payload["products"] = product_changes(conn, base, current)
    index_path.write_text(dashboard_html(dashboard_payload), encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Captura precios de Mercadona y publica el índice.")
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds"),
                        help="Identificador ISO de la captura; por defecto incluye segundos para evitar colisiones")
    parser.add_argument("--import-fixture", type=Path, help="JSON local con una lista de productos normalizados para pruebas")
    parser.add_argument("--database", type=Path, help="Ruta alternativa a la base de datos; no afecta al histórico real")
    parser.add_argument("--output-dir", type=Path, help="Carpeta alternativa para post.txt, index.html y data.json; evita pisar el dashboard publicado")
    parser.add_argument("--force", action="store_true", help="Sobrescribe una captura existente con el mismo identificador")
    parser.add_argument("--allow-incomplete", action="store_true", help="Permite guardar capturas aunque el número de productos difiera mucho")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING o ERROR")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    config_path = ROOT / "config.json"
    if not config_path.exists():
        log.error("Falta config.json. Ejecuta: Copy-Item config.example.json config.json")
        return 2
    try:
        config = validate_config(read_json(config_path))
    except ValueError as error:
        log.error("Configuración inválida: %s", error)
        return 2

    if args.database and not args.output_dir:
        log.warning("Se usa una base de datos alternativa sin --output-dir: se sobrescribirán docs/ y out/ del proyecto.")

    conn = db_connect(args.database)
    try:
        already_exists = snapshot_exists(conn, args.date)
        if already_exists and not args.force:
            log.info("La captura %s ya existe; se regeneran las salidas sin volver a descargar.", args.date)
        else:
            if args.import_fixture:
                products = read_json(args.import_fixture)
                if not isinstance(products, list):
                    log.error("El fixture debe ser una lista JSON de productos.")
                    return 3
                source = "fixture"
            else:
                try:
                    products = fetch_catalog(config)
                except RuntimeError as error:
                    log.error("%s", error)
                    return 5
                source = "mercadona-web"

            if not products:
                log.error("No hay productos que guardar.")
                return 3
            try:
                check_completeness(conn, args.date, len(products), config, args.allow_incomplete)
            except RuntimeError as error:
                log.error("%s", error)
                return 4
            save_snapshot(conn, args.date, config, products, source, force=args.force)
            log.info("Captura %s guardada con %d productos.", args.date, len(products))

        print(make_outputs(conn, args.date, config, args.output_dir))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
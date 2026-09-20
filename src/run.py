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
DEFAULT_CONFIG: dict[str, Any] = {
    "postal_code": "",
    "warehouse": "",
    "language": "es",
    "request_delay_seconds": 0.25,
    "request_retries": 3,
    "request_timeout_seconds": 45,
    "completeness_tolerance": 0.25,
    "respect_robots": True,
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
    config["respect_robots"] = bool(config.get("respect_robots", True))
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
def robots_allowed(url: str, config: dict[str, Any]) -> bool:
    if not config.get("respect_robots", True):
        log.warning("Comprobación de robots.txt desactivada por configuración.")
        return True
    parser = urllib.robotparser.RobotFileParser()
    robots_url = urllib.parse.urljoin(SITE, "/robots.txt")
    parser.set_url(robots_url)
    try:
        req = urllib.request.Request(robots_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=config["request_timeout_seconds"]) as response:
            parser.parse(response.read().decode("utf-8", "replace").splitlines())
    except (urllib.error.URLError, OSError, ValueError) as error:
        log.warning("No se pudo leer robots.txt (%s); se continúa sin restricción.", error)
        return True
    allowed = parser.can_fetch(HEADERS["User-Agent"], url)
    if not allowed:
        log.error("robots.txt prohíbe acceder a %s", url)
    return allowed


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
    if not robots_allowed(root_url, config):
        raise RuntimeError("robots.txt no permite el acceso; se cancela la captura.")
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
DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Índice Mercadona</title>
<style>
:root{color-scheme:light dark;--ink:#173b32;--muted:#5e716b;--surface:#fff;--paper:#f5f7f3;--green:#0d6b4e;--red:#bb3c35;--line:#dce4df}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1120px;margin:auto;padding:32px 20px 56px}
header{display:flex;justify-content:space-between;gap:20px;align-items:start;border-bottom:1px solid var(--line);padding-bottom:22px}
h1{font-size:clamp(28px,5vw,45px);margin:0 0 6px;letter-spacing:-.04em}h2{font-size:20px;margin:0 0 16px}
p{margin:0;color:var(--muted)}.tag{color:var(--green);font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.09em}
.updated{text-align:right;font-size:14px;white-space:nowrap}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}
.metric,.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px}.metric{padding:18px}
.metric span{display:block;color:var(--muted);font-size:13px}.metric strong{display:block;font-size:30px;letter-spacing:-.04em;margin-top:7px}
.positive{color:var(--red)}.negative{color:var(--green)}
.layout{display:grid;grid-template-columns:1.45fr .9fr;gap:18px}.panel{padding:22px}
#chart{display:block;width:100%;height:auto;overflow:visible}
.axis{stroke:var(--line);stroke-width:1}
.chart-intraday{fill:none;stroke:var(--green);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.chart-monthly{fill:none;stroke:#2563eb;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.chart-annual{fill:none;stroke:#d97706;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.chart-label{fill:var(--muted);font-size:12px}
#legend{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 12px;font-size:13px;color:var(--muted)}
.legend-item{display:inline-flex;align-items:center;gap:6px}
.legend-item i{width:14px;height:3px;border-radius:2px;display:inline-block}
i.chart-intraday{background:var(--green)}i.chart-monthly{background:#2563eb}i.chart-annual{background:#d97706}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:11px 5px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600}
td:last-child,th:last-child{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--red);font-weight:700}.down{color:var(--green);font-weight:700}
.method{margin-top:18px;padding-top:18px;border-top:1px solid var(--line);font-size:14px;line-height:1.5}
@media(max-width:760px){main{padding:22px 14px}header{display:block}.updated{text-align:left;margin-top:12px;white-space:normal}
.metrics{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.metric strong{font-size:25px}}
@media(prefers-color-scheme:dark){:root{--ink:#e6f0eb;--muted:#a7b7b0;--surface:#17221e;--paper:#0d1512;--green:#4bc090;--red:#ff8b83;--line:#304139}}
</style>
<main>
<header>
  <div><div class="tag">Seguimiento independiente</div><h1>Índice Mercadona</h1><p>Precios online · <span id="zone">__ZONE__</span></p></div>
  <p class="updated" id="updated"></p>
</header>
<section class="metrics" aria-label="Indicadores principales">
  <div class="metric"><span>Índice base 100</span><strong id="index">—</strong></div>
  <div class="metric"><span>Inflación intradía</span><strong id="intraday">—</strong></div>
  <div class="metric"><span>Inflación mensual</span><strong id="monthly">—</strong></div>
  <div class="metric"><span>Inflación anual acumulada</span><strong id="annual">—</strong></div>
</section>
<section class="layout">
  <article class="panel">
    <h2>Evolución de la inflación</h2>
    <div id="legend"></div>
    <svg id="chart" viewBox="0 0 680 280" role="img" aria-label="Evolución temporal de la inflación intradía, mensual y anual"></svg>
    <p id="chart-note"></p>
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
<section class="panel method">
  <h2>Cómo se calcula</h2>
  <p>Se realizan tres capturas al día. La inflación intradía compara cada captura con la primera del día; la mensual con el último dato del mes anterior; y la anual con el último dato previo al 1 de enero. Cuando aún no hay historia del año anterior, la anual se calcula desde la primera captura disponible del año y se señala como parcial. El índice usa una cesta de peso igual por producto y formato de venta; no es un índice oficial.</p>
</section>
</main>
<script>
const data = __DATA__;
const fmtEuro = n => new Intl.NumberFormat('es-ES',{style:'currency',currency:'EUR'}).format(n);
const fmtNum = n => new Intl.NumberFormat('es-ES',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n);
const fmtPct = n => n==null ? 'Sin dato' : ((n>=0?'+':'') + fmtNum(n) + '%');
const ESCAPE = {'&':'&#38;','<':'&#60;','>':'&#62;','"':'&#34;',"'":'&#39;'};
const escapeHtml = s => String(s==null?'':s).replace(/[&<>"']/g, c => ESCAPE[c]);
function setMetric(id, value){
  const el = document.getElementById(id);
  el.textContent = fmtPct(value);
  el.className = value>0 ? 'positive' : (value<0 ? 'negative' : '');
}
const last = data.history[data.history.length-1];
document.getElementById('zone').textContent = data.zone;
document.getElementById('updated').textContent = 'Actualizado: ' + new Date(data.captured_at).toLocaleString('es-ES');
document.getElementById('index').textContent = fmtNum(last.index);
setMetric('intraday', last.intraday);
setMetric('monthly', last.monthly);
setMetric('annual', last.annual);
const moversBody = document.getElementById('movers');
if (data.movers && data.movers.length) {
  moversBody.innerHTML = data.movers.map(function(m){
    const cls = m.change>0 ? 'up' : 'down';
    return '<tr><td><strong>'+escapeHtml(m.name)+'</strong><br><small>'+escapeHtml(m.category)+'</small></td>'
      + '<td>'+fmtEuro(m.old_price)+'</td><td>'+fmtEuro(m.new_price)+'</td>'
      + '<td class="'+cls+'">'+fmtPct(m.change)+'</td></tr>';
  }).join('');
} else {
  moversBody.innerHTML = '<tr><td colspan="4">Aún no hay una captura anterior para comparar.</td></tr>';
}
const svg = document.getElementById('chart');
const W=680, H=280, L=58, R=18, T=18, B=38;
const defs = [
  {key:'intraday', label:'Intradía', cls:'chart-intraday'},
  {key:'monthly', label:'Mensual', cls:'chart-monthly'},
  {key:'annual', label:'Anual', cls:'chart-annual'}
];
const active = defs.filter(function(d){
  return data.history.some(function(h){ return h[d.key]!=null; });
});
document.getElementById('legend').innerHTML = active.map(function(d){
  return '<span class="legend-item"><i class="'+d.cls+'"></i>'+d.label+'</span>';
}).join('');
const values = [];
active.forEach(function(d){ data.history.forEach(function(h){ if (h[d.key]!=null) values.push(h[d.key]); }); });
let lo, hi;
if (values.length) {
  const mn = Math.min.apply(null, values), mx = Math.max.apply(null, values);
  if (mn === mx) { lo = mn - 1; hi = mx + 1; }
  else { const pad = Math.max(0.1, (mx-mn)*0.15); lo = mn - pad; hi = mx + pad; }
} else { lo = -1; hi = 1; }
const N = data.history.length;
const X = i => L + (W-L-R) * (N<=1 ? 0.5 : i/(N-1));
const Y = v => T + (H-T-B) * (1 - (v-lo)/(hi-lo));
function segments(key){
  const segs = []; let cur = [];
  data.history.forEach(function(h,i){
    const v = h[key];
    if (v==null) { if (cur.length) { segs.push(cur); cur = []; } }
    else { cur.push(X(i).toFixed(1)+','+Y(v).toFixed(1)); }
  });
  if (cur.length) segs.push(cur);
  return segs;
}
let content = '';
if (lo < 0 && hi > 0) {
  content += '<line class="axis" x1="'+L+'" y1="'+Y(0)+'" x2="'+(W-R)+'" y2="'+Y(0)+'"/>';
  content += '<text class="chart-label" x="4" y="'+(Y(0)+4)+'">0%</text>';
}
content += '<text class="chart-label" x="4" y="'+(Y(hi)+4)+'">'+fmtNum(hi)+'%</text>';
content += '<text class="chart-label" x="4" y="'+(Y(lo)+4)+'">'+fmtNum(lo)+'%</text>';
active.forEach(function(d){
  segments(d.key).forEach(function(seg){
    content += '<polyline class="'+d.cls+'" points="'+seg.join(' ')+'"/>';
  });
});
content += '<text class="chart-label" x="'+L+'" y="'+(H-8)+'">'+data.history[0].date.slice(0,10)+'</text>';
content += '<text class="chart-label" text-anchor="end" x="'+(W-R)+'" y="'+(H-8)+'">'+last.date.slice(0,10)+'</text>';
svg.innerHTML = content;
document.getElementById('chart-note').textContent =
  (data.annual_partial ? 'Anual parcial desde la primera captura disponible del año. ' : 'Anual desde el último dato previo al 1 de enero. ')
  + N + ' capturas disponibles.';
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
    intraday = None
    day_start = first_snapshot_of_day(conn, current)
    if day_start:
        result = period_change(conn, day_start, current)
        if result:
            intraday = result[0]
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
        f"Inflación intradía: {pct(intraday)}",
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
        start_day = first_snapshot_of_day(conn, snapshot_date)
        change = period_change(conn, start_day, snapshot_date) if start_day else None
        before_month = previous_date(conn, snapshot_date, month_only=True)
        change_month = period_change(conn, before_month, snapshot_date) if before_month else None
        year_base, partial = annual_reference(conn, snapshot_date)
        change_year = period_change(conn, year_base, snapshot_date) if year_base else None
        if level:
            history.append({
                "date": snapshot_date,
                "index": round(level[0], 4),
                "products": level[1],
                "intraday": round(change[0], 4) if change else None,
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
    }
    index_path.write_text(dashboard_html(payload), encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
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
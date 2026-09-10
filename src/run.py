#!/usr/bin/env python3
"""Captura precios, calcula un índice y genera un dashboard estático.

Solo biblioteca estándar. Los endpoints de la tienda no son una API pública
documentada: úsalo con moderación y respeta las condiciones del sitio.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "out"
DOCS = ROOT / "docs"
API = "https://tienda.mercadona.es/api"
HEADERS = {"Accept": "application/json", "Accept-Language": "es-ES,es;q=0.9", "User-Agent": "MercadonaPriceIndex/0.1 (personal research; contact: local)"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def request_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


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
    root = request_json(f"{API}/categories/?{params}")
    items: dict[str, dict[str, Any]] = {}
    for category_id, category_name in category_ids(root):
        time.sleep(float(config.get("request_delay_seconds", 0.25)))
        payload = request_json(f"{API}/categories/{urllib.parse.quote(category_id)}/?{params}")
        for raw in walk(payload):
            product = product_from(raw, category_name)
            if product:
                items[product["product_id"]] = product
    if not items:
        raise RuntimeError("No se extrajo ningún producto. La estructura de la tienda puede haber cambiado.")
    return sorted(items.values(), key=lambda p: p["product_id"])


def db_connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or (DATA / "mercadona.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (snapshot_date TEXT PRIMARY KEY, captured_at TEXT NOT NULL, postal_code TEXT NOT NULL, warehouse TEXT NOT NULL, source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS prices (snapshot_date TEXT NOT NULL, product_id TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, format TEXT NOT NULL, price REAL NOT NULL, bulk_price REAL, PRIMARY KEY(snapshot_date, product_id), FOREIGN KEY(snapshot_date) REFERENCES snapshots(snapshot_date));
    CREATE INDEX IF NOT EXISTS ix_prices_product_date ON prices(product_id, snapshot_date);
    """)
    return conn


def save_snapshot(conn: sqlite3.Connection, snapshot_date: str, config: dict[str, Any], products: list[dict[str, Any]], source: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)", (snapshot_date, datetime.now().isoformat(timespec="seconds"), str(config.get("postal_code", "")), str(config.get("warehouse", "")), source))
        conn.execute("DELETE FROM prices WHERE snapshot_date = ?", (snapshot_date,))
        conn.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", [(snapshot_date, p["product_id"], p["name"], p["category"], p["format"], p["price"], p["unit_price"]) for p in products])


def previous_date(conn: sqlite3.Connection, current: str, month_only: bool = False) -> str | None:
    if month_only:
        return conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ? AND substr(snapshot_date,1,7) < substr(?,1,7)", (current, current)).fetchone()[0]
    return conn.execute("SELECT max(snapshot_date) FROM snapshots WHERE snapshot_date < ?", (current,)).fetchone()[0]


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


def dashboard_html(payload: dict[str, Any]) -> str:
    """Dashboard autocontenido: no necesita backend, CDN ni cookies."""
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html>
<html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Índice Mercadona · 08028</title>
<style>
:root{{color-scheme:light dark;--ink:#173b32;--muted:#5e716b;--surface:#fff;--paper:#f5f7f3;--green:#0d6b4e;--red:#bb3c35;--line:#dce4df}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:1120px;margin:auto;padding:32px 20px 56px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:start;border-bottom:1px solid var(--line);padding-bottom:22px}}h1{{font-size:clamp(28px,5vw,45px);margin:0 0 6px;letter-spacing:-.04em}}h2{{font-size:20px;margin:0 0 16px}}p{{margin:0;color:var(--muted)}}.tag{{color:var(--green);font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.09em}}.updated{{text-align:right;font-size:14px;white-space:nowrap}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}}.metric,.panel{{background:var(--surface);border:1px solid var(--line);border-radius:12px}}.metric{{padding:18px}}.metric span{{display:block;color:var(--muted);font-size:13px}}.metric strong{{display:block;font-size:30px;letter-spacing:-.04em;margin-top:7px}}.positive{{color:var(--red)}}.negative{{color:var(--green)}}.layout{{display:grid;grid-template-columns:1.45fr .9fr;gap:18px}}.panel{{padding:22px}}#chart{{display:block;width:100%;height:auto;overflow:visible}}.axis{{stroke:var(--line);stroke-width:1}}.chart-line{{fill:none;stroke:var(--green);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}}.chart-dot{{fill:var(--green)}}.chart-label{{fill:var(--muted);font-size:12px}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:11px 5px;border-bottom:1px solid var(--line);text-align:left}}th{{color:var(--muted);font-weight:600}}td:last-child,th:last-child{{text-align:right;font-variant-numeric:tabular-nums}}.up{{color:var(--red);font-weight:700}}.down{{color:var(--green);font-weight:700}}.method{{margin-top:18px;padding-top:18px;border-top:1px solid var(--line);font-size:14px;line-height:1.5}}@media(max-width:760px){{main{{padding:22px 14px}}header{{display:block}}.updated{{text-align:left;margin-top:12px;white-space:normal}}.metrics{{grid-template-columns:repeat(2,1fr)}}.layout{{grid-template-columns:1fr}}.metric strong{{font-size:25px}}}}@media(prefers-color-scheme:dark){{:root{{--ink:#e6f0eb;--muted:#a7b7b0;--surface:#17221e;--paper:#0d1512;--green:#4bc090;--red:#ff8b83;--line:#304139}}}}
</style>
<main><header><div><div class="tag">Seguimiento independiente</div><h1>Índice Mercadona</h1><p>Precios online para 08028 · Barcelona</p></div><p class="updated" id="updated"></p></header>
<section class="metrics" aria-label="Indicadores principales"><div class="metric"><span>Índice base 100</span><strong id="index">—</strong></div><div class="metric"><span>Inflación diaria</span><strong id="daily">—</strong></div><div class="metric"><span>Inflación mensual</span><strong id="monthly">—</strong></div><div class="metric"><span>Productos comparables</span><strong id="products">—</strong></div></section>
<section class="layout"><article class="panel"><h2>Evolución del índice</h2><svg id="chart" viewBox="0 0 680 270" role="img" aria-label="Evolución temporal del índice de precios"></svg><p id="chart-note"></p></article><article class="panel"><h2>Mayores cambios diarios</h2><div style="overflow-x:auto"><table><thead><tr><th>Producto</th><th>Antes</th><th>Ahora</th><th>Variación</th></tr></thead><tbody id="movers"></tbody></table></div></article></section>
<section class="panel method"><h2>Cómo se calcula</h2><p>El índice compara una cesta de peso igual por producto y formato de venta. Las variaciones usan únicamente productos disponibles en ambas fechas, para evitar que altas o bajas de catálogo se confundan con inflación. No es un índice oficial ni una estimación del gasto de los hogares.</p></section></main>
<script>const data={data};const euro=n=>new Intl.NumberFormat('es-ES',{{style:'currency',currency:'EUR'}}).format(n);const number=n=>new Intl.NumberFormat('es-ES',{{maximumFractionDigits:2,minimumFractionDigits:2}}).format(n);const pct=n=>n==null?'Sin dato':`${{n>=0?'+':''}}${{number(n)}}%`;const last=data.history.at(-1);document.getElementById('updated').textContent=`Actualizado: ${{new Date(data.captured_at).toLocaleString('es-ES')}}`;document.getElementById('index').textContent=number(last.index);for(const [id,value] of [['daily',last.daily],['monthly',last.monthly]]){{const el=document.getElementById(id);el.textContent=pct(value);el.className=value>0?'positive':value<0?'negative':''}}document.getElementById('products').textContent=new Intl.NumberFormat('es-ES').format(last.products);document.getElementById('chart-note').textContent=`Base: ${{data.base_date}} · ${{data.history.length}} capturas disponibles`;document.getElementById('movers').innerHTML=data.movers.length?data.movers.map(x=>`<tr><td><strong>${{x.name}}</strong><br><small>${{x.category}}</small></td><td>${{euro(x.old_price)}}</td><td>${{euro(x.new_price)}}</td><td class="${{x.change>0?'up':'down'}}">${{pct(x.change)}}</td></tr>`).join(''):'<tr><td colspan="4">Aún no hay una captura anterior para comparar.</td></tr>';const svg=document.getElementById('chart'),w=680,h=270,l=55,r=18,t=18,b=35,values=data.history.map(x=>x.index),min=Math.min(...values),max=Math.max(...values),pad=Math.max(.5,(max-min)*.15),lo=min-pad,hi=max+pad,x=i=>l+(w-l-r)*(data.history.length===1?.5:i/(data.history.length-1)),y=v=>t+(h-t-b)*(1-(v-lo)/(hi-lo));svg.innerHTML=`<line class="axis" x1="${{l}}" y1="${{y(lo)}}" x2="${{w-r}}" y2="${{y(lo)}}"/><line class="axis" x1="${{l}}" y1="${{y(100)}}" x2="${{w-r}}" y2="${{y(100)}}"/><text class="chart-label" x="4" y="${{y(hi)+4}}">${{number(hi)}}</text><text class="chart-label" x="4" y="${{y(100)+4}}">100</text><polyline class="chart-line" points="${{data.history.map((d,i)=>`${{x(i)}},${{y(d.index)}}`).join(' ')}}"/>${{data.history.map((d,i)=>`<circle class="chart-dot" cx="${{x(i)}}" cy="${{y(d.index)}}" r="4"><title>${{d.date}}: ${{number(d.index)}}</title></circle>`).join('')}}<text class="chart-label" x="${{l}}" y="${{h-8}}">${{data.history[0].date}}</text><text class="chart-label" text-anchor="end" x="${{w-r}}" y="${{h-8}}">${{last.date}}</text>`;</script></html>'''


def make_outputs(conn: sqlite3.Connection, current: str, config: dict[str, Any]) -> str:
    base = conn.execute("SELECT min(snapshot_date) FROM snapshots").fetchone()[0]
    current_index = index_change(conn, base, current)
    if current_index is None:
        raise RuntimeError("No hay productos comparables entre la base y la fecha solicitada.")
    prev = previous_date(conn, current)
    daily = None
    if prev:
        result = period_change(conn, prev, current)
        if result:
            daily = result[0]
    monthly = None
    previous_month = previous_date(conn, current, month_only=True)
    if previous_month:
        result = period_change(conn, previous_month, current)
        if result:
            monthly = result[0]
    def pct(value: float | None) -> str:
        return "sin dato" if value is None else f"{value:+.2f}%".replace(".", ",")
    lines = [f"Índice Mercadona — {date.fromisoformat(current).strftime('%d/%m/%Y')}", "", f"Índice: {current_index[0]:.2f}".replace(".", ","), f"Variación diaria: {pct(daily)}"]
    lines.append(f"Variación mensual: {pct(monthly)}")
    lines += ["", f"Cesta comparable: {current_index[1]} productos", f"Zona: CP {config.get('postal_code', 'sin definir')} · almacén {config.get('warehouse', 'sin definir')}", "Metodología: cesta de peso igual por producto."]
    text = "\n".join(lines)
    OUT.mkdir(exist_ok=True); DOCS.mkdir(exist_ok=True)
    (OUT / "post.txt").write_text(text, encoding="utf-8")
    history = []
    for snapshot_date, in conn.execute("SELECT snapshot_date FROM snapshots ORDER BY snapshot_date"):
        level = index_change(conn, base, snapshot_date)
        before = previous_date(conn, snapshot_date)
        change = period_change(conn, before, snapshot_date) if before else None
        before_month = previous_date(conn, snapshot_date, month_only=True)
        change_month = period_change(conn, before_month, snapshot_date) if before_month else None
        if level:
            history.append({"date": snapshot_date, "index": round(level[0], 4), "products": level[1], "daily": round(change[0], 4) if change else None, "monthly": round(change_month[0], 4) if change_month else None})
    payload = {"base_date": base, "captured_at": datetime.now().isoformat(timespec="seconds"), "history": history[-365:], "movers": price_movers(conn, prev, current)}
    (DOCS / "index.html").write_text(dashboard_html(payload), encoding="utf-8")
    (DOCS / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--import-fixture", type=Path, help="JSON local con una lista de productos normalizados para pruebas")
    parser.add_argument("--database", type=Path, help="Ruta alternativa para una base de pruebas; no afecta data/mercadona.sqlite3")
    args = parser.parse_args()
    config_path = ROOT / "config.json"
    if not config_path.exists():
        print("Falta config.json. Ejecuta: Copy-Item config.example.json config.json", file=sys.stderr)
        return 2
    config = read_json(config_path)
    products = read_json(args.import_fixture) if args.import_fixture else fetch_catalog(config)
    if not isinstance(products, list): raise ValueError("El fixture debe ser una lista JSON de productos.")
    conn = db_connect(args.database); save_snapshot(conn, args.date, config, products, "fixture" if args.import_fixture else "mercadona-web")
    print(make_outputs(conn, args.date, config)); return 0


if __name__ == "__main__":
    raise SystemExit(main())

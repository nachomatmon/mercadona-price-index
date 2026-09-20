# Índice Mercadona

Proyecto personal, sin dependencias y sin coste de infraestructura, para capturar precios diarios de la tienda online de Mercadona, calcular un índice reproducible y publicar un dashboard estático.

## Qué hace

1. Descarga el árbol de categorías y sus productos de la tienda online.
2. Guarda un histórico diario en SQLite (`data/mercadona.sqlite3`).
3. Calcula un índice de cesta fija con base 100 en la primera captura útil.
4. Genera un dashboard en `docs/index.html`, con evolución, inflación y mayores cambios.
5. Realiza tres capturas diarias y calcula inflación intradía, mensual y anual acumulada.


## Arranque local (Windows)

```powershell
Copy-Item config.example.json config.json
py -3 src\run.py
```

La primera ejecución fija la base del índice y todavía no muestra variaciones. A partir de ahí, cada captura nueva ya calcula la inflación intradía (frente a la primera captura del mismo día), la mensual (frente al último dato del mes anterior) y la anual acumulada.

Para probar el cálculo sin conectarse a Mercadona:

```powershell
py -3 src\run.py --import-fixture tests\fixtures\day-1.json --date 2026-09-30 --database tests\tmp.sqlite3 --output-dir tests\tmp-out
py -3 src\run.py --import-fixture tests\fixtures\day-2.json --date 2026-10-01 --database tests\tmp.sqlite3 --output-dir tests\tmp-out
```

El parámetro `--database` mantiene las pruebas aisladas del histórico real y `--output-dir` evita pisar el dashboard publicado en `docs/`.

## Pruebas

Las pruebas usan SQLite en memoria, no tocan el histórico real y no hacen peticiones de red:

```powershell
py -3 -m unittest discover -s tests -p "test_*.py" -v
```

## Opciones de línea de comandos

| Opción | Para qué sirve |
| --- | --- |
| `--date ISO` | Identificador de la captura. Por defecto incluye la hora local con segundos, para que las tres capturas diarias convivan sin pisarse. |
| `--database RUTA` | Usa otra base de datos. El histórico real no se toca. |
| `--output-dir RUTA` | Escribe `post.txt`, `index.html` y `data.json` en otra carpeta en lugar de `out/` y `docs/`. |
| `--import-fixture JSON` | Carga una lista de productos normalizados en lugar de descargarlos. |
| `--force` | Reescribe una captura que ya existe con ese identificador. Sin esta opción, una captura repetida se conserva y solo se regeneran las salidas. |
| `--allow-incomplete` | Permite guardar una captura aunque el número de productos se aleje mucho del anterior. |
| `--log-level` | `DEBUG`, `INFO`, `WARNING` o `ERROR`. |

La ejecución es **idempotente**: repetirla con la misma fecha no duplica datos ni descarga de nuevo, únicamente vuelve a generar el dashboard y el resumen.

## Configuración (`config.json`)

| Clave | Por defecto | Para qué sirve |
| --- | --- | --- |
| `postal_code` | `""` | Código postal asociado a la captura. |
| `warehouse` | — (obligatorio) | Almacén (`wh`) que se consulta en la tienda. |
| `language` | `"es"` | Idioma de las peticiones. |
| `request_delay_seconds` | `0.25` | Espera entre categorías. |
| `request_retries` | `3` | Reintentos por petición, con espera exponencial. |
| `request_timeout_seconds` | `45` | Tiempo máximo por petición. |
| `completeness_tolerance` | `0.25` | Desviación máxima de nº de productos antes de rechazar la captura. |
| `robots_policy` | `"warn"` | `"warn"` avisa si robots.txt lo prohíbe, `"block"` cancela la captura y `"ignore"` ni lo consulta. |

## Protecciones y compatibilidad

- **robots.txt**: antes de descargar nada se consulta `https://tienda.mercadona.es/robots.txt`. La tienda publica `Disallow: /api`, y por eso el valor por defecto (`robots_policy: "warn"`) **avisa en el log y continúa**: bloquear por defecto dejaría la captura automática sin efecto. Con `"block"` la captura se cancela y con `"ignore"` ni se consulta. La decisión queda explícita en `config.json` en lugar de tomarse por ti.
- **Reintentos**: cada petición se reintenta con espera exponencial (`request_retries`) y hay un retardo entre categorías (`request_delay_seconds`) para no castigar el servidor.
- **Categorías tolerantes a fallos**: si una categoría falla tras los reintentos, se avisa y se continúa con las demás, en vez de abortar toda la captura.
- **Control de integridad**: si una captura trae muchos menos productos que la anterior (más de `completeness_tolerance`), se rechaza para no falsear el índice.
- **Histórico intacto**: el esquema solo crece. Las versiones se controlan con `PRAGMA user_version`, así que el histórico ya guardado en `data/mercadona.sqlite3` se sigue leyendo sin migraciones destructivas ni recálculos de la base.

## Automatización gratis

En tu ordenador, crea una tarea diaria en el **Programador de tareas de Windows** que ejecute:

```text
py C:\Users\Lenovo\Documents\Mercadona\src\run.py
```

Una vez validado, puedes activar el workflow incluido en `.github/workflows/daily.yml` en un repositorio público. GitHub Actions y GitHub Pages permiten que el código, los datos y la página estática vivan allí sin servidor propio. Antes de activarlo, revisa los términos de Mercadona y la carga que produces: el script limita las peticiones y no intenta eludir bloqueos.

El workflow `daily.yml` ejecuta las pruebas antes de capturar, de modo que un cambio que rompa el parser no llega a publicar datos erróneos. `tests.yml` pasa las mismas pruebas en cada `push` y `pull request`.

### Despliegue sin coste en GitHub

1. Crea un repositorio **público** y sube esta carpeta.
2. En GitHub, abre `Settings` → `Actions` → `General` y permite que los workflows tengan permiso de lectura y escritura.
3. En `Settings` → `Pages`, selecciona **GitHub Actions** como fuente.
4. En `Actions`, ejecuta una vez `Captura diaria del índice` con **Run workflow**. A partir de ahí se ejecutará diariamente y publicará una web en `https://TU-USUARIO.github.io/TU-REPOSITORIO/`.

## Metodología

`Índice(t) = 100 × suma(precio(t) de productos comparables) / suma(precio(base) de esos mismos productos)`.

Es una cesta de **peso igual por producto y formato de venta**, no un IPC oficial ni una estimación del gasto de los hogares. Cada variación usa únicamente productos presentes en ambas fechas comparadas, para no confundir una baja/alta temporal de catálogo con una variación de precios. La inflación intradía compara con la primera captura del día; la mensual con el último dato del mes anterior; y la anual con el último dato previo al 1 de enero. Si todavía no existe, la anual se muestra desde la primera captura disponible del año como parcial. El código postal y el almacén quedan registrados con cada observación para no mezclar zonas.

Para un índice más representativo, añade después una tabla de ponderaciones por producto; no cambies la definición histórica sin publicar una nueva serie/base.

## Estructura

```text
src/run.py                 ejecutable único: captura, cálculo y dashboard
tests/test_run.py          pruebas unitarias con SQLite en memoria
tests/fixtures/            datos de ejemplo para probar sin red
data/mercadona.sqlite3     histórico versionado (se sube para no perder la serie)
out/post.txt               resumen de texto generado
docs/                      dashboard estático para GitHub Pages
.github/workflows/         captura programada (daily.yml) y pruebas (tests.yml)
```

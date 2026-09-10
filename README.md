# Índice Mercadona

Proyecto personal, sin dependencias y sin coste de infraestructura, para capturar precios diarios de la tienda online de Mercadona, calcular un índice reproducible y publicar un dashboard estático.

## Qué hace

1. Descarga el árbol de categorías y sus productos de la tienda online.
2. Guarda un histórico diario en SQLite (`data/mercadona.sqlite3`).
3. Calcula un índice de cesta fija con base 100 en la primera captura útil.
4. Genera un dashboard en `docs/index.html`, con evolución, inflación y mayores cambios.
5. Calcula inflación diaria y mensual respecto al último dato disponible del mes anterior.


## Arranque local (Windows)

```powershell
Copy-Item config.example.json config.json
py -3 src\run.py
```

La primera ejecución fija la base del índice y todavía no muestra variaciones. La segunda ya genera la variación diaria.

Para probar el cálculo sin conectarse a Mercadona:

```powershell
py -3 src\run.py --import-fixture tests\fixtures\day-1.json --date 2026-09-30 --database tests\tmp.sqlite3
py -3 src\run.py --import-fixture tests\fixtures\day-2.json --date 2026-10-01 --database tests\tmp.sqlite3
```

El parámetro `--database` mantiene las pruebas aisladas del histórico real.

## Automatización gratis

En tu ordenador, crea una tarea diaria en el **Programador de tareas de Windows** que ejecute:

```text
py C:\Users\Lenovo\Documents\Mercadona\src\run.py
```

Una vez validado, puedes activar el workflow incluido en `.github/workflows/daily.yml` en un repositorio público. GitHub Actions y GitHub Pages permiten que el código, los datos y la página estática vivan allí sin servidor propio. Antes de activarlo, revisa los términos de Mercadona y la carga que produces: el script limita las peticiones y no intenta eludir bloqueos.

### Despliegue sin coste en GitHub

1. Crea un repositorio **público** y sube esta carpeta.
2. En GitHub, abre `Settings` → `Actions` → `General` y permite que los workflows tengan permiso de lectura y escritura.
3. En `Settings` → `Pages`, selecciona **GitHub Actions** como fuente.
4. En `Actions`, ejecuta una vez `Captura diaria del índice` con **Run workflow**. A partir de ahí se ejecutará diariamente y publicará una web en `https://TU-USUARIO.github.io/TU-REPOSITORIO/`.

## Metodología

`Índice(t) = 100 × suma(precio(t) de productos comparables) / suma(precio(base) de esos mismos productos)`.

Es una cesta de **peso igual por producto y formato de venta**, no un IPC oficial ni una estimación del gasto de los hogares. Cada variación usa únicamente productos presentes en ambas fechas comparadas, para no confundir una baja/alta temporal de catálogo con una variación de precios. El código postal y el almacén quedan registrados con cada observación para no mezclar zonas.

Para un índice más representativo, añade después una tabla de ponderaciones por producto; no cambies la definición histórica sin publicar una nueva serie/base.

## Estructura

```text
src/run.py                 ejecutable único
data/mercadona.sqlite3     histórico local (no se sube por defecto)
out/                       resumen de texto generado
docs/                      dashboard estático para GitHub Pages
.github/workflows/         ejecución diaria opcional
```

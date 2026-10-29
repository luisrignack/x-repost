# xrepost · panel web local

Panel para previsualizar, repostear y deshacer desde una interfaz web, usando el
mismo motor que el CLI (`xrepost.sh` + `xrepost.py`). Vive en la rama `web-ui`
y en su propia carpeta (`web/`); la rama `main` sigue siendo solo CLI.

## Requisitos

- Python 3.10 o superior (solo biblioteca estándar; no hay que instalar nada).
- Chrome con la sesión de X iniciada y el endpoint CDP que ya usa el CLI
  (`BU_CDP_URL`, por defecto `http://127.0.0.1:9222`).
- `xrepost.sh` y `xrepost.py` en la raíz del repositorio.

## Arranque

Desde la raíz del worktree:

```bash
./web/run.sh
```

o directamente:

```bash
python3 web/app.py
```

El servidor escucha **solo en `127.0.0.1`**, genera un token aleatorio y abre el
panel en una ventana de Chrome aparte. La terminal muestra el enlace completo:

```text
Panel: http://127.0.0.1:8765/?token=…
```

Opciones útiles:

```bash
python3 web/app.py --no-open          # no abrir el navegador
python3 web/app.py --port 9000        # puerto concreto (0 = aleatorio)
python3 web/app.py --verbose          # registrar peticiones HTTP
python3 web/app.py --token mi-token   # token fijo (solo para pruebas locales)
```

También se puede cambiar el motor, el historial, las cuentas recordadas y la
carpeta de ejecuciones:

```bash
python3 web/app.py --engine ./xrepost.sh \
  --history ~/.local/state/xrepost/history.jsonl \
  --accounts ~/.local/state/xrepost/accounts.json \
  --runs ./web/.runs
```

## Autocompletado de cuentas

El campo **Cuentas** sugiere las cuentas usadas antes, tanto desde el panel como
desde el CLI. Las coincidencias se actualizan al escribir, se ordenan por
coincidencia (exacta, prefijo, subcadena), uso reciente y frecuencia, y se
pueden elegir con el ratón o con el teclado:

- `↑` / `↓` mueven la selección.
- `Enter` acepta la sugerencia resaltada.
- `Tab` completa la cuenta resaltada y añade una coma para la siguiente.
- `Esc` cierra el desplegable.

Las cuentas se guardan en `~/.local/state/xrepost/accounts.json` (o en la ruta
de `X_ACCOUNTS` / `--accounts`), junto al historial. El panel registra la cuenta
en cuanto se previsualiza una búsqueda y reutiliza el mismo archivo que el CLI.
Al arrancar, el servidor lo indica en la línea `Cuentas:`.

## Flujo

1. **Previsualizar**: busca publicaciones con el mismo motor del CLI. No toca la
   cuenta; genera un listado y archivos CSV/Markdown descargables.
2. **Repostear**: solo se habilita tras una previsualización completada. Pide
   confirmación explícita en un diálogo y exige una pausa mínima de 5 s.
3. **Historial**: muestra el último estado de cada publicación (reposteado,
   deshecho, etc.) y permite deshacer un repost concreto.
4. **Deshacer última tanda**: revierte los reposts que hizo la herramienta en la
   última ejecución y que sigan vigentes.

La aplicación repostea **exactamente el listado previsualizado**, no vuelve a
buscar en X entre la vista previa y la confirmación. Para ello el panel guarda
el listado revisado y el motor lo consume mediante `X_ONLY_URLS_FILE` (uso
interno del panel; el CLI normal no lo necesita).

## Seguridad

- Escucha solo en `127.0.0.1`; no lo expongas a la red.
- Token obligatorio en la URL, en cookie `HttpOnly`/`SameSite=Strict` o en la
  cabecera `X-Token`.
- Comprueba `Host` y `Origin` para bloquear peticiones de otros orígenes.
- Una sola ejecución a la vez (Chrome es una única sesión).
- Aplicar requiere `confirm: true`, una previsualización completada con los
  mismos parámetros y al menos 5 s de pausa.
- El servidor no guarda contraseñas ni cookies de X: usa la sesión de Chrome.

## Navegador separado

El panel se abre con un perfil propio en
`~/.cache/xrepost-web/panel-profile`. Así no comparte pestaña ni proceso con la
instancia de Chrome que controla `browser-use` en el puerto 9222. Si usas el
mismo perfil, una ejecución puede navegar fuera del panel.

## Pruebas

Las pruebas usan un motor simulado: no abren Chrome, no acceden a X y no tocan
el historial real.

```bash
cd web
python3 -m unittest -v
```

Comprueban autenticación, comprobaciones de `Host`/`Origin`, validación de
fechas y parámetros, previsualización, exportaciones, confirmación, lista exacta
aplicada, historial, deshacer, bloqueo de una sola ejecución, cancelación y
sugerencias de cuentas (registro al previsualizar, ranking y exclusión).

## Límites actuales

- No hay selección manual post a post: se aplica el listado previsualizado hasta
  el máximo indicado (1–50).
- El historial es el mismo JSONL del CLI
  (`~/.local/state/xrepost/history.jsonl`); el panel solo deshace lo que la
  herramienta registró como suyo.
- El panel no sustituye al CLI: lo invoca como subproceso. Los valores por
  defecto siguen siendo conservadores (dry-run salvo confirmación, retweets y
  respuestas omitidos).

## Mantener la rama al día

`main` no se toca. Para traer cambios del CLI a `web-ui`:

```bash
git checkout web-ui
git merge main
```

Si hay conflictos en `xrepost.py`, conserva la carga de `X_ONLY_URLS_FILE` y el
resto de cambios del CLI.

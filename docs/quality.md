# Calidad de código, tests y Git Flow

[Índice](README.md) · [Contribuir](../CONTRIBUTING.md) · [Workflow CI](../.github/workflows/ci.yml)

## Preparar el entorno

```sh
uv sync --locked
uv run pre-commit install
uv run python scripts/quality.py all
```

Se instalan hooks `pre-commit` y `commit-msg` en **este clon**, no globalmente.
Los nuevos clones requieren ejecutar la instalación. Si ya tienes hooks propios,
revisa la configuración antes de sustituirlos. No instales hooks desde una copia
de código que no hayas revisado.

Las herramientas son gratuitas y están fijadas en `uv.lock`. Su instalación puede
descargar paquetes; `actionlint-py` descarga el binario de la versión fijada de
actionlint durante su construcción. No se requieren APIs de pago ni tokens de LLM.

## Comandos por fase

| Comando con `uv run python scripts/quality.py` | Qué hace | Modifica código |
| --- | --- | --- |
| `format` | Ruff autofix y formateador | Sí; revisar el diff |
| `check` | Ruff lint/formato, mypy, yamllint, configuración de hooks y actionlint | No |
| `test` | pytest, cobertura de aplicación, JUnit y XML de cobertura | Solo reportes/cachés |
| `build` | Construye wheel y sdist | Solo artefactos en `dist/` |
| `all` | `check` → `test` → `build`, deteniéndose ante el primer fallo | Solo artefactos/cachés |

Los comandos son multiplataforma, no usan shell para construir ejecuciones y tienen
timeout por herramienta. El orden local es deliberado: evita gastar tiempo en tests
si el código no pasa los controles estáticos. En CI, los jobs independientes corren
en paralelo; los tests corren en Windows y Linux.

## Controles locales

[`.pre-commit-config.yaml`](../.pre-commit-config.yaml) reutiliza el entorno uv para
evitar un entorno Python diferente por hook. Comprueba:

- Rama de trabajo conforme al esquema Git Flow; bloquea commits directos desde
  `main`, `develop` o un HEAD separado.
- Asunto del commit con Conventional Commits, máximo 100 caracteres.
- Marcadores de conflictos, claves privadas y archivos añadidos mayores de 500 KiB.
- Espacios al final, nueva línea final, sintaxis TOML, lint/formato Python y YAML.
- Sintaxis, expresiones y estructura de GitHub Actions mediante actionlint.

Ruff y los hooks de espacios/nueva línea pueden modificar archivos. Si sucede,
revisa los cambios, vuelve a añadirlos y repite el commit. No desactives controles
para ocultar un fallo. La detección de claves privadas no es un escáner exhaustivo
de secretos: sigue revisando tokens, datos de candidatos y sesiones manualmente.

Los hooks ligeros no ejecutan toda la suite ni mypy en cada commit. Antes de abrir
un PR ejecuta `all`. `actionlint` desactiva los motores opcionales ShellCheck y
Pyflakes para mantener iguales las comprobaciones de Windows y Linux; no promete
analizar todos los lenguajes embebidos en un paso `run`.

## Tests y cobertura

El mínimo **95%** está centralizado en `tool.coverage.report.fail_under`, con cobertura
de ramas activada. Se aplica al paquete de aplicación `job_radar`; no se mezcla con
la cobertura de scripts de mantenimiento. Los scripts también tienen tests, aunque
no forman parte de ese porcentaje. `pytest` valida configuración/marcadores y trata
recursos no cerrados y excepciones no gestionadas de finalizadores como errores.

La suite incluye reglas y límites, importación parcial, deduplicación, rollback de
datos/auditoría, dos escritores independientes, CLI offline, contratos documentados,
Git Flow, mensajes y propiedades de seguridad del workflow. Una prueba con dos
escritores no demuestra capacidad de producción ni sustituye un benchmark.

`test` genera `reports/junit.xml` y `reports/coverage.xml`, ignorados por Git.
CI conserva únicamente esos XML durante siete días, incluso cuando falla el test
si llegaron a generarse. No sube bases SQLite, sesiones ni evidencias reales.

## Flujo de ramas

```text
main ─────────────────────────── publicación estable
  └── develop ───────────────── integración
        └── codex/feature/* ─── PR hacia develop
        └── codex/fix/* ─────── PR hacia develop
        └── codex/chore/* ───── PR hacia develop
        └── codex/release/* ─── PR hacia main + sincronización con develop
main ── codex/hotfix/* ──────── PR hacia main + sincronización con develop
```

También se admiten `codex/docs/*`, `codex/refactor/*`, `codex/test/*` y `codex/ci/*`.
El slug utiliza letras minúsculas, números y separadores `-`, `_` o `.`; sin espacios
ni niveles adicionales. No hace falta instalar la extensión `git-flow`.

| Destino del PR | Orígenes permitidos |
| --- | --- |
| `develop` | Cualquier rama `codex/<tipo admitido>/<slug>`, `main` para sincronizar, y ramas Dependabot reconocidas |
| `main` | `develop`, `codex/release/*` o `codex/hotfix/*` |

Una feature no entra directamente a `main`. Tras publicar una release/hotfix,
sincroniza los cambios con `develop` mediante PR. Los merges remotos no ejecutan tus
hooks locales; los valida el workflow del PR. No hay auto-merge ni publicación de
releases configurados.

En un clon con `develop` ya disponible y sin cambios pendientes:

```sh
git switch develop
git pull --ff-only
git switch -c codex/feature/source-reader
```

`develop` debe existir en el remoto antes de abrir PR hacia ella. En un repositorio
nuevo, el mantenedor crea y publica esa rama desde el punto estable de `main`; esta
documentación no crea ramas remotas automáticamente.

## Conventional Commits

```text
feat(discovery): add source reader
fix(storage): preserve audit rollback
docs: describe filter precedence
ci: add Windows test reports
feat(config)!: change schema version
```

Tipos admitidos: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`,
`revert` y `test`. El scope y `!` son opcionales. El asunto no lleva saltos de línea,
espacios sobrantes ni más de 100 caracteres; el cuerpo puede ampliar el contexto.
El título del PR usa la misma regla. Con squash merge, conserva ese título como
asunto del commit resultante. CI valida el título del PR, no todos los commits de
su historial; la validación de cada commit individual es local.

## GitHub Actions

Se ejecuta en pushes a `main`, `develop` y `codex/**`, PR hacia `main`/`develop`,
merge queues y ejecución manual. Editar un título de PR vuelve a validar su política.
No hay filtros de rutas que puedan dejar un check requerido sin ejecutar.

```text
Static quality ────────┐
Tests (Ubuntu/Windows) ├──> Quality gate
Git Flow policy ──────┘
```

El check estable `Quality gate` falla si alguno de sus jobs requeridos falla,
se cancela o se omite. Las acciones están fijadas por SHA, el token solo tiene
`contents: read`, checkout no conserva credenciales y no se usa `pull_request_target`.
Los títulos/ramas de PR se leen como datos JSON/variables, nunca como comandos shell.
Los caches aceleran instalaciones; los timeouts y cancelación de ejecuciones
anteriores evitan trabajo obsoleto.

El job estático también ejecuta los hooks sobre todos los archivos versionados.
Omite solo el hook de nombre de rama local porque checkout puede estar separado de
una rama; el job `Git Flow policy` valida los refs remotos. Si un hook corrige un
archivo, CI falla para que se incorpore la corrección: no hace commits ni pushes.

[Dependabot](../.github/dependabot.yml) propone actualizaciones semanales de Python
y Actions hacia `develop`, agrupando actualizaciones Python menores/patch. No
aprueba ni integra sus propios cambios. Esa configuración necesita publicarse en la
rama por defecto del repositorio para que el servicio la descubra.

## Activación remota pendiente del mantenedor

Los archivos de CI **no son protección de ramas**. Después de publicar los cambios
y obtener una ejecución real correcta, configura rulesets de `main` y `develop`:

1. Exigir PR y el check `Quality gate`; no permitir integración con checks pendientes.
2. Prohibir force-push y borrado de las ramas estables.
3. Exigir resolución de conversaciones; revisiones obligatorias si hay otro
   mantenedor disponible, sin bloquear un proyecto unipersonal por autoaprobación.
4. Revisar especialmente cambios de workflows, políticas, dependencias y scripts.

No se activan estas reglas automáticamente ni se cambian ajustes de facturación.
Si GitHub no puede iniciar los runners, no se debe presentar la configuración como
CI validado: ejecuta los controles locales y resuelve el impedimento del servicio.

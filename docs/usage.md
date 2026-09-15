# Uso de la CLI

[Índice](README.md) · [Configuración](configuration.md) · [Operación](operations.md)

## Preparación

Ejecuta desde la raíz del clon. Se requiere uv, Python 3.14 y SQLite >= 3.51.3 en
ese intérprete; `.python-version` fija el parche usado por el proyecto. OpenCLI,
Agent Reach y CodeGraph son opcionales para los comandos offline de esta guía.
Para recolección con caché, plantillas y Gmail directo, consulta
[ejecución sin asistente](autonomous.md).

```sh
uv sync --locked
uv run radar doctor
uv run radar --help
uv run radar config validate
```

`doctor` muestra versiones, CPU lógicas, memoria disponible y presencia de las
herramientas opcionales. `session_status: "not_checked"` significa que no comprobó
cuentas. `runtime_ok` no certifica compatibilidad de todas las integraciones futuras.

## Comandos y efectos

| Comando | Opciones propias | Efecto |
| --- | --- | --- |
| `doctor` | Ninguna | Diagnóstico local; no crea la base |
| `config validate` | Ninguna | Valida configuración y catálogo; devuelve su hash |
| `catalog` | `--enabled-only`, `--category`, `--source` | Lista metadatos; no accede a sitios |
| `plan` | `--limit`, `--category`, `--source` | Genera consultas para fuentes habilitadas; `executed: false` |
| `init` | `--data-dir` | Crea directorio/base si faltan y verifica el esquema |
| `ingest` | `--input` obligatorio, `--data-dir` | Lee JSONL local y persiste ofertas y auditoría |
| `status` | `--data-dir` | Cuenta ofertas, decisiones, ejecuciones y eventos |
| `metrics` | `--data-dir` | Imprime una instantánea en formato Prometheus |

La opción global `--config` va **antes** del comando y por defecto usa
`config/example.yaml`. Solo `config`, `catalog`, `plan` e `ingest` cargan ese YAML.
El directorio de datos predeterminado es `.local/radar`; contiene `radar.sqlite3`.
`ingest` puede crear la base, por lo que `init` es opcional. `status` y `metrics`
no inicializan una base ausente y no hacen escrituras de negocio.

Las salidas de negocio son JSON en stdout, excepto `metrics` (texto Prometheus).
El log JSON de importación completada va a stderr. La ayuda y los errores de sintaxis
de argumentos siguen el formato de argparse, no el formato JSON de negocio.

## Seleccionar fuentes y planificar

```sh
uv run radar catalog --enabled-only --category social
uv run radar catalog --source remoteok --source indeed
uv run radar plan --category web_search --category job_board --limit 10
uv run radar plan --source remoteok --limit 2
```

Las opciones repetidas se combinan por unión dentro de cada selector y por
intersección entre categorías y fuentes. Un ID desconocido devuelve error; una
selección válida sin fuentes habilitadas devuelve un plan vacío, no un error.
`--source` no habilita una fuente desactivada.

`--limit` debe estar entre 1 y `runtime.max_queries`; si se omite usa ese presupuesto.
No hay ejecución ni ampliación automática cuando faltan consultas. Para una fuente
`domain`, las consultas llevan `site:dominio`; eso no equivale a haber leído la página.

## Importación reproducible

Usa un directorio nuevo para comprobar los contadores de este ejemplo:

```sh
uv run radar init --data-dir .local/tutorial
uv run radar ingest --input examples/jobs.jsonl --data-dir .local/tutorial
uv run radar ingest --input examples/jobs.jsonl --data-dir .local/tutorial
uv run radar status --data-dir .local/tutorial
uv run radar metrics --data-dir .local/tutorial
```

La primera importación inserta cuatro ofertas. Repetir inmediatamente el mismo
archivo con la misma configuración registra cuatro observaciones sin cambios:
`unchanged: 4`, cuatro ofertas totales, dos ejecuciones completadas y doce eventos
de auditoría. Los UUID varían. Las fechas del ejemplo son estáticas y la evaluación
puede cambiar al cruzar límites de antigüedad; los contadores no son una promesa para
una base que ya contenga otras ejecuciones.

## Contrato JSONL

Un objeto JSON por línea, codificado en UTF-8. No es un array JSON. Ejemplo completo:

```json
{"source_id":"remoteok","external_id":"demo-001","title":"DevOps Engineer","company":"Example Company","url":"https://example.com/jobs/demo-001","description":"Terraform y Kubernetes. Oferta ficticia.","remote":true,"published_at":"2026-09-14T00:00:00Z","language":"es"}
```

| Campo | Regla |
| --- | --- |
| `source_id` | Obligatorio; 1–64 caracteres y presente en el catálogo |
| `external_id` | Obligatorio; 1–256 caracteres, sin quedar vacío al quitar espacios |
| `title`, `company` | Obligatorios; 1–512 y 1–256 caracteres respectivamente; no solo espacios |
| `url` | Obligatorio; hasta 4096 caracteres; HTTP(S) absoluto sin usuario/contraseña |
| `description` | Opcional; texto hasta 50 000 caracteres; por defecto vacío |
| `remote` | Booleano o `null`; por defecto desconocido |
| `published_at` | Fecha ISO con zona horaria o `null`; por defecto desconocida |
| `language` | Texto de 2–16 caracteres o `null`; se compara con los idiomas configurados |

Importar una oferta de una fuente desactivada es válido si su ID está registrado:
`enabled` controla el planificador, no el archivo local. El ID externo se conserva,
pero la identidad para deduplicar es la URL normalizada, no ese ID.

Límites: 64 KiB por línea incluyendo su terminador, 8 MiB por archivo y
`runtime.max_records` objetos no vacíos. Las líneas en blanco se omiten, aunque
cuentan para el tamaño total. Se rechazan campos desconocidos, tipos incompatibles,
fechas sin zona y entradas inválidas; la importación se detiene, no salta el registro.
El tamaño en bytes puede limitar una descripción antes de alcanzar su máximo de
caracteres. No se descargan las URLs del JSONL.

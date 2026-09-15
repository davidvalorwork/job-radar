# Configuración y criterios de clasificación

[Índice](README.md) · [Ejemplo completo](../config/example.yaml) · [Catálogo](../config/sources.yaml)

## Archivos y precedencia actual

Guarda una copia completa del ejemplo en `config/local.yaml` y edítala para tus
preferencias. El prefijo `config/local*` está ignorado por Git. Se carga **un solo
archivo**, no hay fusión automática con el ejemplo ni variables de entorno para
sobrescribirlo. Usa:

```sh
uv run radar --config config/local.yaml config validate
uv run radar --config config/local.yaml plan --limit 10
```

`sources_file` se resuelve respecto al directorio del YAML elegido. Si necesitas
un catálogo privado, guarda por ejemplo `config/local-sources.yaml` y referencia
`sources_file: local-sources.yaml`. No publiques páginas privadas, contactos o sesiones.

Se rechazan campos desconocidos, IDs duplicados, claves YAML repetidas, aliases y
archivos mayores de 1 MiB. Usa booleanos YAML reales, no cadenas `"true"`/`"false"`.
La validación devuelve un SHA-256 de configuración y catálogo normalizados: incluye
también fuentes desactivadas; no guarda una copia recuperable de toda la política.

## Campos superiores y perfiles

| Campo | Valor por defecto / límite | Uso |
| --- | --- | --- |
| `schema_version` | `1`, único admitido | Contrato de configuración |
| `sources_file` | `sources.yaml` | Ruta al catálogo |
| `profiles` | Obligatorio, 1–20 perfiles | Reglas de roles y consultas |
| `profiles[].id` | Obligatorio, único | Patrón `[a-z][a-z0-9_]{0,63}` |
| `profiles[].priority` | `50`, entre 0 y 100 | Mayor prioridad primero; empate por ID |
| `profiles[].title_terms` | Obligatorio, 1–64 términos | Basta uno en el título |
| `profiles[].required_context_any` | `[]`, hasta 64 | Si no está vacío, exige uno en título o descripción |
| `profiles[].preferred_terms` | `[]`, hasta 64 | Añade puntuación; no es requisito |
| `profiles[].queries` | Obligatorio, 1–20 | Variantes literales para el planificador |

Cada término/consulta debe tener contenido y hasta 512 caracteres. El emparejamiento
no distingue mayúsculas y usa límites de palabra; no es una búsqueda semántica ni
traduce sinónimos. Gana el primer perfil coincidente por prioridad. La puntuación es
50 más 10 por término preferido encontrado, con máximo 100; sin perfil es 0.
No existe un umbral configurable de puntuación que autorice contactos.

El ejemplo prioriza DevOps/SRE/Platform/Cloud (`100`) frente a Full Stack AI (`80`).
Después cubre backend (`75`), frontend (`70`), full stack web (`68`) y serverless/AWS
Lambda (`65`). Full Stack AI exige contexto AI/LLM/RAG; los demás perfiles son
tecnologías/roles explícitos. Cambiar `title_terms` no regenera `queries`, ni las
consultas generan términos de clasificación.

### Ecuación de posiciones y tecnologías

La búsqueda combina el rol del título con tecnologías preferidas:

```text
DevOps/SRE
  > Full Stack AI (AI/LLM/RAG)
  > Backend (Node.js | Python | Django)
  > Frontend (React | Angular)
  > Full Stack web (React + Node.js/Python/Django)
  > Serverless (AWS Lambda)
```

Cada línea tiene consultas literales para encontrar vacantes; el clasificador solo
acepta un perfil cuando un término aparece en el título y, si aplica, el contexto
requerido aparece en título o descripción. `preferred_terms` suma puntuación, pero no
convierte una tecnología mencionada en un requisito ni autoriza contacto. Los perfiles
se pueden copiar a `config/local.yaml` y ajustar sin cambiar el código. Si una oferta
usa un título genérico, queda para revisión o `no_role_match`; no se fuerza una
clasificación por leer únicamente la descripción.

## Filtros implementados

| Campo en `filters` | Predeterminado | Efecto |
| --- | --- | --- |
| `remote_only` | `true` | Rechaza `remote: false`; desconocido requiere revisión |
| `max_age_days` | `30`, entre 1 y 3650 o `null` | Rechaza antigüedad superior al límite; `null` desactiva el filtro |
| `languages` | `[en, es]`, hasta 20 | Lista permitida; vacía desactiva el filtro, incluido idioma desconocido |
| `excluded_companies` | `[]`, hasta 1000 | Coincidencia exacta del nombre sin distinguir mayúsculas |
| `min_monthly_salary_usd` | `null` | Umbral mensual USD **exclusivo**; `null` desactiva el filtro salarial |
| `include_undisclosed_salary` | `true` | Acepta salario no publicado cuando el filtro salarial está activo |

Las fechas futuras o ausentes y el idioma desconocido requieren revisión solo si
sus filtros están activos. Con
`remote_only: false`, el valor remoto desconocido deja de ser motivo de revisión.
Las empresas excluidas no se resuelven por identidad corporativa o dominio.

Se conservan motivos como `no_role_match`, `not_remote`, `publication_too_old`,
`company_excluded` o `language_unknown`. Resultado final: si hay algún rechazo,
`rejected`; sin rechazo pero con incertidumbre, `review`; en otro caso, `qualified`.
Todas las decisiones se guardan, no solo las ofertas aceptadas.

### Salario publicado y no publicado

Los registros JSONL admiten `salary_min_monthly_usd`, `salary_max_monthly_usd` y
`salary_disclosed` (por defecto `false`). Los importes deben ser finitos, no negativos
y estar **ya normalizados a USD mensuales**. Para una cantidad exacta, indica ambos
extremos iguales. Un extremo informado implica salario publicado aunque se omita
`salary_disclosed`. No se extraen salarios del texto ni se convierten monedas automáticamente.

Con un umbral de `2000`:

- Importe exacto de 2000 o máximo inferior/igual: `rejected`, `salary_at_or_below_threshold`.
- Mínimo estrictamente superior: supera el filtro salarial.
- Rango 1800–2500, solo un máximo superior o mínimo de 2000: `review`,
  `salary_range_requires_confirmation`; no se descarta ni se presume un salario garantizado.
- Sin salario publicado y sin extremos: aceptado si `include_undisclosed_salary: true`;
  en caso contrario, `rejected`, `salary_undisclosed`.
- Salario publicado pero todavía sin normalizar (otra moneda, pago por hora sin
  horas garantizadas, etc.): marca `salary_disclosed: true` sin extremos;
  `review`, `salary_conversion_required`. No lo clasifiques como salario no publicado.

Esta configuración desactiva las preferencias de modalidad, antigüedad e idioma:

```yaml
filters:
  remote_only: false
  max_age_days: null
  languages: []
  excluded_companies: []
  min_monthly_salary_usd: 2000
  include_undisclosed_salary: true
```

Los perfiles continúan delimitando los roles buscados. No se exige moneda de cobro,
criptomonedas ni plataforma de pago. La clasificación no autoriza envíos ni confirma
que una vacante siga abierta o acepte al candidato: esas verificaciones son separadas.

## Presupuestos

| Campo en `runtime` | Predeterminado | Límite |
| --- | --- | --- |
| `batch_size` | `100` | 1–1000 registros por transacción |
| `max_records` | `2000` | 1–100 000 registros por importación; siguen aplicando 8 MiB |
| `max_queries` | `200` | 1–1000 consultas de salida por plan |
| `network_enabled` | `false` | `true` no está admitido |
| `sending_enabled` | `false` | `true` no está admitido |

Un lote mayor reduce commits pero aumenta memoria y el trabajo no confirmado si
falla ese lote. No aumenta por sí mismo la concurrencia: la importación es secuencial.
No hay opciones operativas de cuota diaria, trabajadores o envío en este esquema.
El flujo separado [mail](mail.md) usa un manifiesto privado, confirmación de lote y
`--daily-limit` (1–500); no cambia estos flags de los comandos offline.

## Catálogo de fuentes

El catálogo admite `schema_version: 1` y entre 1 y 500 entradas en `sources`.

| Campo de fuente | Regla / significado |
| --- | --- |
| `id` | Obligatorio y único, mismo patrón que los perfiles |
| `label` | Obligatorio, 1–128 caracteres |
| `category` | `web_search`, `social`, `job_board`, `community`, `company_page` o `feed` |
| `url` | HTTP(S) o ausente; obligatorio para `domain` |
| `enabled` | `false` por defecto; seleccionada para planificar, no conectada |
| `priority` | `50` por defecto; 0–100, mayor primero |
| `query_support` | Obligatorio: `keyword`, `domain`, `target_only` o `unavailable` |
| `command_refs` | Hasta 16 referencias informativas; nunca comandos ejecutados |
| `tags` | Hasta 20 etiquetas; metadatos, sin filtro CLI por etiquetas todavía |
| `access_status` | Solo `unverified`; no certifica sesiones |

`keyword` conserva la consulta; `domain` antepone `site:host` sin usar la ruta de
`url`. Las entradas `target_only` y `unavailable` deben permanecer deshabilitadas.
El plan recorre perfiles por prioridad, luego cada consulta y las fuentes por
prioridad; deduplica por fuente y texto sin distinguir mayúsculas ni espacios en
los extremos. Una cuota pequeña puede agotarse antes de llegar al segundo perfil.

## Si hay pocos resultados

En esta versión los ajustes son **manuales**: añadir sinónimos y consultas explícitas,
habilitar más fuentes planificables, ampliar antigüedad o flexibilizar preferencias.
Valida otra vez y genera un nuevo plan; esto no ejecuta búsquedas. No inventes claves
como `relaxation` o `workers`: el esquema las rechazará.

La herencia global → rol → fuente → página y los escalones automáticos están en la
[hoja de ruta](roadmap.md). La espera de 48 horas es un parámetro de una función de
dominio para futuros contactos, no un campo de este YAML. No elimina restricciones
por supresión, envío incierto o contacto no verificado.

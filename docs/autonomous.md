# Ejecución sin asistente y sin llamadas a LLM

El defecto anterior era de arquitectura: el relay Gmail necesitaba que el asistente
atendiera cada operación. Investigar páginas y redactar mensajes en el chat sumaba
consumo. Esta ruta mueve esas tareas repetitivas a código, archivos y caché.
Desarrollar/depurar con Codex sigue consumiendo tokens: no es consumo del programa.

Antes de recolectar para un envío autónomo, comprobar `mail check --credentials`.
La revisión acotada `radar review` evita cargar páginas completas en el asistente.
Presupuestos, cursores, métricas y limitaciones: [eficiencia de tokens](token-efficiency.md).

## Qué está conectado

```text
config + catálogo → plan --output → collect --allow-network → expand --allow-network
                                                                  ↓
                                                   normalize → caché de evidencias
                                                                  ↓
                                                  ingest + revisión acotada
                                                                  ↓
candidatos estructurados + perfil revisado → mail compose → mail prepare
                                                                  ↓
                       confirmar hash → mail draft --bridge-dir (revisión manual)
                                                                  ↓
                                      Gmail Drafts → tú decides si pulsas Enviar
                                                                  ↓
                                   confirmar hash → mail run --credentials
                                                                  ↓
                                    Gmail → verificar → outbox + auditoría
```

Una vez preparado y confirmado un lote, `mail run --credentials` no necesita
Codex. No hay un scraper universal ni extracción automática fiable de contactos
desde cualquier HTML. `collect` guarda páginas/resultados; **no** convierte una
dirección encontrada en permiso de candidatura. No hay scheduler ni ejecución
diaria configurada por estos comandos.

## 1. Preparar consultas y recolectar por lotes

Necesitas las rutas locales de Agent Reach: `curl` para Jina Reader y `mcporter`
con el servidor Exa configurado para búsquedas. En Windows, Node debe poder ejecutar
directamente el entrypoint instalado de mcporter; no se pasan consultas a un shell.
La presencia de un comando no prueba que su servidor/sesión funcione.

```sh
uv run radar plan --limit 20 --output .local/plan.json
uv run radar collect --input .local/plan.json --allow-network --workers 4 --ttl-seconds 86400
uv run radar expand --allow-network --workers 4 --ttl-seconds 86400
uv run radar normalize --output .local/jobs.jsonl
uv run --config config/local.yaml radar ingest --input .local/jobs.jsonl
```

El archivo conserva fuente, perfil y consulta. `collect` también acepta un JSON/YAML
con peticiones explícitas:

```json
[
  {"kind": "search", "value": "site:company.example DevOps careers"},
  {"kind": "web", "value": "https://company.example/careers"}
]
```

Los dominios del ejemplo son ficticios. Solo enviar búsquedas/URLs públicas, sin
credenciales ni datos privados, a los proveedores. No se accede a sesiones sociales
autenticadas. Exa/Jina tienen límites y pueden cambiar disponibilidad: no son una
promesa de servicio gratuito ilimitado. El programa no compra créditos ni cambia
a proveedores de pago; ante limitación corta nuevas consultas de esa ruta.

Configuración: máximo 1.000 peticiones por entrada, 1–8 workers (4 por defecto), TTL
0–604.800 segundos (86.400 por defecto), caché negativa hasta 300 segundos, hasta
1 MiB por respuesta y 45 segundos por proceso. Las consultas exactas repetidas se
ejecutan una vez. Una caché válida evita la llamada externa; `--ttl-seconds 0` fuerza
nueva lectura. No hay reintentos automáticos dentro de la misma ejecución.

`expand` sigue solamente enlaces HTTPS públicos ya guardados que tengan una ruta de
empleo/carrera. No sigue perfiles, imágenes, `mailto`, rastreadores ni enlaces con
credenciales. Reutiliza la misma caché y no borra una evidencia válida si una lectura
posterior falla por límite temporal. `normalize` crea JSONL local, no crea contactos,
no convierte salarios y no concede permiso de envío.

La detección de límites examina el preámbulo del proveedor y stderr, no el texto
de las ofertas: «429 seguidores» en una página válida no bloquea la ruta. Una página
de desafío/CAPTCHA se registra como fallida; no se intenta eludirla.

`--data-dir` por defecto es `.local/research`. `evidence.sqlite3` guarda la última
respuesta por petición, fecha, estado y SHA-256; `research_audit` registra intentos.
No conserva todas las versiones anteriores del contenido. stdout solo devuelve
`input`, `unique`, `cached`, `fetched`, `failed`, `llm_calls`. Revisar `failed` y los
estados de caché: que una petición esté guardada no significa que haya funcionado.
La caché no renueva por sí misma la fecha de verificación de un contacto.

## 2. Componer sin generación de texto

Usa el [manifiesto de correo](mail.md), omitiendo `subject` y `body`, con ofertas y
contactos ya normalizados. El perfil es JSON/YAML privado:

```json
{
  "sender": "sender@example.com",
  "facts_reviewed": false,
  "signature": "Example Person",
  "portfolio": "https://portfolio.example",
  "examples": [{
    "title_terms": ["devops", "sre"],
    "text": "Replace with experience you can substantiate."
  }]
}
```

Reemplaza los ejemplos y marca `facts_reviewed` solo después de revisarlos. El
algoritmo toma el primer ejemplo cuyo término coincide con el título; no inventa
experiencia cuando no coincide ninguno. `official_application_contact` y
`suppression_reviewed` se preservan: componer nunca los aprueba automáticamente.
Opcionalmente configura `subject` y `body` con `$title`, `$company`, `$url`,
`$example`, `$portfolio`, `$signature`. Variables desconocidas fallan.

```sh
uv run radar mail compose --input .local/candidates.json --profile .local/profile.json --output .local/manifest.json
uv run radar --config config/local.yaml mail prepare --manifest .local/manifest.json --data-dir .local/mail
```

Máximo 100 candidatos. No se sobreescriben manifiestos existentes. No se envía nada
al componer/preparar. La configuración de salario y roles sigue aplicándose en
`prepare`; las plantillas no relajan filtros. Revisa el manifiesto antes de confirmar.

## 3. Autorizar Gmail una vez

Instala el extra opcional; no hacen falta claves de OpenAI ni suscripciones de IA:

```sh
uv sync --locked --extra gmail
uv run --extra gmail radar mail auth --client-secret .local/gmail/client-secret.json --credentials .local/gmail/token.json --account sender@example.com
```

Antes del comando, sigue la [configuración oficial de Google para Python](https://developers.google.com/workspace/gmail/api/quickstart/python):
crea/elige tu proyecto, habilita Gmail API, configura el consentimiento y crea un
cliente OAuth de escritorio. Si está en pruebas, añade tu cuenta como usuario de
prueba. Guarda su JSON en la ruta privada indicada. El navegador pedirá permiso
para `gmail.readonly` y `gmail.send`; se comprueba que autorices la cuenta indicada.
No se envían mensajes durante la autorización. No compartir estos JSON por chat/Git.

Se requiere refresh token para la renovación del acceso. El estado de pruebas,
revocaciones o políticas de Google pueden exigir nueva autorización: no se promete
una sesión permanente. Ante credenciales inválidas, `run` se detiene y no abre un
login interactivo por sorpresa. `auth` tampoco sobreescribe archivos existentes.
En Windows, restringe el acceso a `.local/gmail` con permisos de tu usuario; el modo
POSIX `0600` de creación no sustituye una ACL de Windows.

## 4. Ejecutar el lote confirmado

```sh
uv run --extra gmail radar mail run --batch BATCH_HASH --confirm BATCH_HASH --credentials .local/gmail/token.json --data-dir .local/mail --daily-limit 500
uv run radar mail status --data-dir .local/mail
```

Reemplaza ambos hashes por el devuelto por `prepare`. **Este comando envía**. Usa
siempre el mismo `--data-dir` para una cuenta, incluyendo los lotes anteriores; una
base nueva perdería parte de sus reservas/supresiones. Historial de 48 horas,
cuota móvil de 24 horas, bajas y reconciliación se describen en [correo](mail.md).

Las lecturas de metadatos se agrupan de 50 en 50 y se reutilizan en memoria solo
para mensajes SENT. La búsqueda se refresca antes de cada reserva; no se cachean
borradores ni decisiones de supresión. El [batch de Gmail](https://developers.google.com/workspace/gmail/api/guides/batch)
reduce viajes HTTP, no convierte 100 operaciones en una sola unidad de cuota.
El envío es secuencial y sin reintentos ambiguos; paralelizarlo arriesgaría duplicados.

`mail run` añade un resumen con `transport`, `provider_round_trips`,
`metadata_cache_hits` y `llm_calls: 0`. Este último cuenta llamadas del programa a
modelos, no tokens del asistente ni implementación interna de proveedores externos.
Ninguna métrica publica destinatarios, contenido de mensajes o credenciales.

## 5. Crear borradores para revisión manual

Cuando prefieras revisar y enviar tú mismo, usa el trabajador local de Chrome ya
conectado a tu Gmail. Este modo no usa OAuth, IA ni `--enable-send`:

```sh
uv run radar mail draft --batch BATCH_HASH --confirm BATCH_HASH --bridge-dir .local/chrome/bridge --data-dir .local/mail
```

El trabajador abre el mensaje, verifica destinatario, asunto, cuerpo y PDF, usa
solamente el control exacto de Gmail **Guardar y cerrar / Save & close**, y confirma
que el borrador aparece en Gmail. Nunca activa ni pulsa **Enviar**. Cada borrador se
reserva en `outbox.sqlite3`; un timeout queda como `unknown` y no se repite de forma
automática. Los borradores ya creados también se excluyen de `mail run`, para que un
comando posterior no los envíe por accidente.

El puente debe estar iniciado sin `--enable-send` y la extensión debe estar recargada
con la misma versión que muestra `Código`. Si no puede identificar el control o el
borrador final, se detiene y deja el caso para revisión.

## Evidencia de la optimización

Pruebas sintéticas, sin red ni correo real:

| Escenario | Resultado comprobado |
| --- | --- |
| 100 peticiones idénticas | 1 lectura; segunda ejecución con caché: 0 |
| 100 metadatos Gmail | 2 batches de 50; segunda consulta: 0 lecturas de metadatos |
| 100 candidaturas con hechos aprobados | 100 mensajes por plantilla, sin modelo |
| Timeout al verificar después de enviar | Estado incierto, reconciliación sin reenviar |
| Relanzar un lote ya enviado | 0 envíos adicionales |

No son benchmarks de velocidad real ni una garantía de encontrar/enviar 100 ofertas
válidas por día. Ejecuta `uv run python scripts/quality.py all` para comprobar lint,
tipado, YAML, workflows, tests con cobertura mínima del 95% y construcción del paquete.

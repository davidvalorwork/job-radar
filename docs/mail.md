# Envío Gmail desde Job Radar

El transporte recomendado es Gmail OAuth directo: `mail run --credentials` ejecuta
el lote sin asistente después de autorizar tu propia cuenta una vez. Instalación,
autorización y fases: [ejecución sin asistente](autonomous.md). La CLI no llama a LLM.
El transporte antiguo `--bridge-dir` sigue siendo asistido: requiere una sesión que
atienda solicitudes y puede consumir tokens del asistente. No confundir ambos modos.

## Flujo y responsabilidades

```text
Manifiesto privado + filtros + PDF
  → mail prepare → lote inmutable en SQLite → confirmación del hash
  → perfil Gmail → historial completo de 48h → bajas/borradores
  → reserva transaccional de cuota/cooldown → Gmail send
  → Gmail read-back → sent + auditoría
                     ↘ unknown → bloqueo → reconcile (solo lectura del proveedor)
```

Hexágono: `domain/mail.py` define los valores; `application/delivery.py` el caso de
uso y los puertos; `outbox.py`, `mail_plan.py`, `gmail_bridge.py`, `gmail_native.py`
y `mail_templates.py` son adaptadores.
Solo `bootstrap.py` conecta estas piezas. El envío es secuencial por cuenta para
poder verificar cada resultado antes de continuar, no un worker por destinatario.

## Manifiesto privado

Guárdalo dentro de `.local/`. Ejemplo **ficticio**, que requiere un PDF propio:

```json
{
  "campaign": "example-reviewed-batch",
  "sender": "sender@example.com",
  "cv": "cv.pdf",
  "suppressed_recipients": [],
  "suppressed_companies": [],
  "candidates": [{
    "job": {
      "source_id": "company_pages",
      "external_id": "example-devops",
      "title": "DevOps Engineer",
      "company": "Example Company",
      "url": "https://company.example/careers/devops"
    },
    "recipient": "jobs@company.example",
    "company_key": "company.example",
    "subject": "DevOps application",
    "body": "Replace with a factual, individually reviewed application.",
    "contact_source_url": "https://company.example/careers/devops",
    "verified_at": "2026-09-14T12:00:00Z",
    "official_application_contact": true,
    "suppression_reviewed": true
  }]
}
```

No marques las verificaciones como verdaderas sin comprobar la fuente y el historial.
`company_key` es el dominio canónico de la empresa: usa el mismo para sus alias y
direcciones. No uses `gmail.com` como empresa de todos los reclutadores independientes.
Solo si no hay empresa identificada, usa `contact:recruiter@example.com` para un
contacto independiente: debe coincidir exactamente con `recipient`. No permite
evadir el cooldown de una empresa conocida ni el del propio destinatario.
Una dirección por mensaje, sin CC/BCC. Máximo 100 candidatos por manifiesto; el YAML/JSON
está limitado a 1 MiB. El CV debe empezar por `%PDF-` y ocupar como máximo 2 MB;
esa comprobación de formato no sustituye revisar el contenido del CV.

`cv` se resuelve respecto al manifiesto. El PDF se captura al preparar el lote:
cambiar después el archivo original no cambia los bytes autorizados. El hash del
lote vincula campaña, configuración, cuerpos, destinatarios, evidencia y PDF.

Solo pasan a la cola ofertas `qualified` y con verificaciones declaradas; las
`review`/`rejected` se cuentan y no se envían. Usa los filtros de tu configuración,
incluida la política de salario no publicado. Una fecha de evidencia caducada
(más de 24 horas) exige volver a comprobar el contacto y preparar un nuevo lote.

## Ejecutar

```sh
uv run radar --config config/local.yaml mail prepare --manifest .local/mail-manifest.json
uv run radar mail status
uv run --extra gmail radar mail run --batch BATCH_HASH --confirm BATCH_HASH --credentials .local/gmail/token.json --daily-limit 500
```

Reemplaza `BATCH_HASH` por el hash devuelto por `prepare`, después de revisar el
contenido del manifiesto. El comando `run` llama al transporte seleccionado y **envía**
el lote confirmado, no es un dry-run. Preparar o consultar no envía nada.
Todos los comandos admiten `--data-dir`; usa **la misma base para la misma cuenta**.
Se crea `outbox.sqlite3`, separado de la base de ofertas `radar.sqlite3`.

La cuota local configurable es un máximo de destinatarios en 24 horas móviles (1–500),
no una promesa de disponer de 500 ofertas ni la cuota que Gmail permita. Incluye
salidas manuales que Gmail devuelve y envíos
locales, deduplicados por ID del proveedor. La reserva es atómica entre procesos
que comparten SQLite; no puede bloquear otros clientes Gmail ni otras bases locales.

Las direcciones o empresas contactadas hace menos de 48 horas se bloquean. El
historial anterior no impide una nueva candidatura pertinente. Repetir el mismo
lote no reenvía elementos enviados; una campaña nueva requiere nueva preparación
y confirmación. Ninguna caducidad programa automáticamente seguimientos.

## Contrato del relay autorizado

`FileConnector` crea una subcarpeta UUID por ejecución y un archivo
`ID.request.json` por operación. Campos: `id`, `method`, `arguments`, `expires_at`.
Solo se permiten `gmail_get_profile`, `gmail_search_emails`, `gmail_send_email` y
`gmail_read_email`. No hay comandos shell ni métodos de borrado en el protocolo.

El host autorizado debe:

1. Comprobar que el proceso sigue activo, el ID no se despachó y la solicitud no expiró.
2. Para un envío, contrastar destinatario/asunto/cuerpo/PDF con el lote revisado.
3. Crear un marcador durable `ID.dispatched` **antes** de llamar al proveedor;
   si hay incertidumbre, no volver a llamar aunque no exista respuesta local.
4. Ejecutar exactamente la operación permitida usando la cuenta autorizada.
5. Escribir primero un archivo temporal y renombrarlo atómicamente a `ID.response.json`:
   `{"id":"ID","result":{...respuesta real del proveedor...}}`.
   En caso de fallo: `{"id":"ID","error":"provider_failure"}`.

El adaptador espera hasta 90 segundos por respuesta y acepta como máximo 4 MiB.
Los archivos del puente contienen datos personales: no publicarlos ni servirlos por HTTP.
No extraer credenciales del host para fabricar un cliente autónomo. Un transporte OAuth
independiente usa `gmail_native.py` y necesita su propia autorización, nunca credenciales
extraídas del conector de Codex.

## Errores, supresión y reconciliación

Los estados son `prepared`, `sending`, `sent` y `unknown`. Las causas de bloqueo
se auditan y dejan el elemento preparado; no provocan reintentos automáticos.
Un `sending` abandonado también bloquea envíos posteriores de esa cuenta.

Antes de cada envío se refresca el historial Gmail. Se rechazan historiales incompletos
(paginación repetida o más de 20 páginas), cuentas distintas, cuota agotada y evidencia
caducada. Se detiene ante un borrador activo del contacto. Un borrador descartado
(etiquetas DRAFT y TRASH) no bloquea una candidatura nueva. Los demás mensajes en
la papelera siguen examinándose para detectar bajas/rebotes. Bajas/rebotes explícitos
detectados se guardan como supresión; el detector por expresiones es conservador y
no entiende todos los idiomas/casos: sigue siendo necesaria la revisión declarada
en el manifiesto. Las supresiones previas deben importarse en las listas del manifiesto.

Después del envío se comprueban SENT, cuenta, destinatario, ausencia de CC/BCC,
asunto, cuerpo, nombre/tipo/tamaño del PDF y fecha del proveedor. Se verifica el
metadato del adjunto, no su contenido descargado. SENT confirma envío en Gmail,
**no entrega, lectura ni ausencia de un rebote posterior**.

Un timeout de envío/verificación deja `unknown`, detiene el lote y devuelve salida 5.
No lo conviertas en `prepared` ni borres la base para reintentar. Localiza el mensaje
real en Gmail y reconcilia su ID, sin enviar de nuevo:

```sh
uv run --extra gmail radar mail reconcile --batch BATCH_HASH --mail-id MAIL_HASH --message-id PROVIDER_ID --credentials .local/gmail/token.json
```

`mail status` devuelve los IDs de lote, elemento y proveedor bajo `unresolved`. Si no
hay ID confirmado, debe investigarse con Gmail. Esta versión no libera automáticamente
un envío incierto ni garantiza entrega exactamente una vez. Salida 6 indica bloqueo
de preflight; 2 entrada inválida; 3 problema de archivos; 4 almacenamiento/runtime.

La auditoría de correo está en `mail_audit`, atómica con las transiciones. La telemetría
emite eventos JSON con contadores, sin destinatarios. `mail status` muestra estados y
supresiones; las métricas Prometheus de `radar metrics` siguen siendo solo de ofertas.

## Auditar envíos externos sin modificar el historial

Cuando un conector u otro proceso ha enviado mensajes, `status` solo refleja lo que
está en esta outbox. No lo interpretes como un conteo completo de Gmail. Guarda
privadamente los IDs reales en este formato (valores sintéticos de ejemplo):

```json
{"account": "sender@example.com", "message_ids": ["provider-id-1", "provider-id-2"]}
```

```sh
uv run radar mail audit --input .local/provider-ids.json --data-dir .local/mail
```

Lee hasta 500 IDs, limita el archivo a 1 MB y consulta SQLite en modo solo lectura,
con filtro de cuenta. Emite contadores, no direcciones ni IDs. No crea una outbox
ausente, no usa red/IA y no envía ni concilia automáticamente. `untracked` señala IDs
fuera del historial y `ledger_not_sent` estados no confirmados. IDs repetidos en la
entrada no duplican el conteo.

Salida 0: todos los IDs únicos están registrados como `sent` en esta cuenta.
Salida 7: faltan registros confirmados; no es permiso para reenviar. Los otros errores
conservan los códigos generales de la CLI. Un ID escrito en un archivo no demuestra
un envío real: `provider_verified` permanece `false` y `valid_applications` es `null`.
El read-back exacto en Gmail, la entrega y la calidad son verificaciones distintas.
`reconcile` necesita un elemento previamente preparado; esta auditoría no inventa ese
elemento para envíos que el programa nunca gestionó.

`prepare` también deriva a revisión contactos de un resumen agregado que no aparecen
literalmente en su sección original cuando esa misma página es la evidencia declarada.
Las guardas de salario señalan tasas explícitas sin normalizar y contradicciones de
equity-only/sin efectivo. Son controles conservadores y limitados, no validación
universal de contactos/salarios ni sustitutos de revisar instrucciones de postulación.

Para conciliar envíos que no tienen reservas previas, pausar pendientes antiguos y
verificar evidencias originales, consulta [cierre previo de campaña](campaign-closeout.md).
Ahora `mail audit` distingue `ledger_sent` y `external_sent`; `ledger_complete` exige
que cada ID esté cubierto por un envío administrado confirmado o un recibo externo
conciliado, sin doble conteo. La importación externa requiere confirmación explícita.

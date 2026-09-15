# Gmail en Chrome sin IA

Estado: **experimental; no validado de extremo a extremo en Gmail real**.
El puente y las protecciones tienen pruebas sintéticas. Haber inspeccionado Gmail
no demuestra que los selectores de composición, adjuntos y envío funcionen en
todas las cuentas. No se enviaron mensajes reales durante estas pruebas.

## Flujo

```text
Lote revisado + confirmación exacta
                |
Python: filtros / outbox SQLite / cuota / duplicados 48 h
                |
FileConnector -> puente HTTP local autenticado
                |
Extensión Chrome -> controles visibles de Gmail -> original MIME
                |
Validar destinatario, asunto, cuerpo, PDF, tamaño y SHA-256
                |
Confirmar en SQLite, o detener como unknown (sin reenvío)
```

No llama a modelos de IA, no requiere claves de Google Cloud y no lee cookies,
contraseñas ni archivos del perfil de Chrome. Crear y depurar este código con un
asistente sí consume tokens. No confundir cero llamadas a IA en ejecución con
cero coste de desarrollo, electricidad o conexión.

Reutiliza el programa existente, `http.server`, `email`, `hashlib`, SQLite y las
API nativas de extensiones de Chrome. No agrega servicios ni paquetes de pago.
La extensión usa una pestaña persistente, no un service worker para el bucle largo.
No requiere `npm install`: JavaScript sin dependencias, pruebas con Node integrado.

## Configuración en Windows (CMD)

Requisitos: Python del proyecto operativo (`uv run radar --help`), Node 24 para
pruebas de JavaScript, Chrome y la cuenta de Gmail iniciada. Copia sólo comandos,
no el indicador `C:\...>` ni mensajes de error. Cada comando ocupa **una línea**.

1. En Chrome abre `chrome://extensions`, activa **Modo desarrollador**, pulsa
   **Cargar descomprimida** y selecciona la carpeta `chrome-extension` de este repo.
   Revisa sus permisos: sólo Gmail, localhost y ejecución del adaptador local;
   no pide cookies, debugger, todo el historial ni acceso a todos los sitios.
2. Mantén Gmail abierto. Esta primera versión exige Gmail en español o inglés
   y rechaza conversaciones agrupadas. Si aparece
   `disable_conversation_view_then_retry`, desactiva la vista de conversación
   manualmente en Gmail antes de repetir una operación de lectura.
3. En la primera CMD, desde el repositorio:

```bat
uv run python scripts/chrome_bridge.py --account tu-correo@gmail.com
```

El correo del ejemplo debe reemplazarse por la cuenta real. Se crea
`.local/chrome/pairing.json`: contiene un secreto local, **no lo publiques**.
El puerto se asigna automáticamente y cambia al reiniciar. Cada reinicio invalida
el archivo anterior; vuelve a cargarlo en la extensión.

4. Pulsa el icono de la extensión, carga ese archivo, elige la pestaña correcta
   de Gmail y pulsa **Conectar**. No habilites envío para esta prueba.
5. En otra CMD, también desde el repositorio:

```bat
uv run python scripts/chrome_probe.py --account tu-correo@gmail.com
```

La respuesta esperada es `account_matches: true`, `sent: 0`, `llm_calls: 0`.
El probe prueba conexión y cuenta, **no** la composición ni el envío.

Para comprobar también el historial completo de las últimas 48 horas:

```bat
uv run python scripts/chrome_probe.py --account tu-correo@gmail.com --history
```

Sólo `history_complete: true` confirma esa lectura; `account_matches: true` no basta.

### Actualizar la extensión instalada

Tras cambiar su código, pulsa **Recargar** en su tarjeta de `chrome://extensions`.
Cierra la pestaña antigua del trabajador y vuelve a abrirlo desde su icono.
Si el puente se reinició, carga de nuevo `.local/chrome/pairing.json` y conecta.
Recargar la página normal de Gmail no actualiza el código de la extensión.

La versión 0.1.1 reconoce **Resultados siguientes** además de **Siguiente**.
Antes, el botón de búsqueda se confundía con el de la bandeja y provocaba un fallo
de paginación. La causa se perdía al atravesar Chrome, el puente y FileConnector.
Ahora conserva un código y fase permitidos (sin texto de mensajes ni secretos)
en pantalla, respuesta local y log. Por ejemplo, `connector_unknown_pagination`
identifica un problema de interfaz; no indica falta de permisos de Gmail.

La versión 0.1.2 reconoce una pestaña nueva del original aunque no incluya
`openerTabId`. Sólo acepta el mismo dominio, ruta de cuenta e identificador
exacto del mensaje; nunca adopta pestañas anteriores ni una pestaña con otro
origen conocido. Dos coincidencias siguen bloqueando la operación. Los fallos
de este paso indican la fase `read_original` y no ocultan el error inicial al
cerrar una pestaña auxiliar. Este caso se prueba con metadatos sintéticos;
la validación completa en Gmail requiere recargar la extensión instalada.
También reconoce el botón de regreso a resultados que Gmail identifica con
`title` en vez de `aria-label`; se verifica en una prueba independiente.

La versión 0.1.3 muestra su versión en la página del trabajador y diferencia
**Conectando**, **Conectado al puente / Esperando solicitudes**, **Procesando**
y **Detenido**. Antes, pulsar Conectar podía dejar un error anterior visible
durante toda la espera, aunque los controles estuvieran bloqueados por el bucle
activo. Una prueba del módulo real reproduce esa reconexión sin Gmail ni envíos.
El puente registra `worker_connected` con versión y permiso de envío al conectar,
cambiar de versión o volver tras una pausa de más de 30 s; no registra cada poll,
cuentas ni claves. `unknown` significa que el cliente no informó una versión válida.
Conectado al puente no prueba acceso a Gmail: sigue siendo necesario el probe.
Una pestaña antigua debe cerrarse y abrirse desde el icono tras recargar la extensión.

Desde 0.1.4 se muestran por separado **Código** y **Extensión instalada**.
La versión 0.1.5 corrige el bloqueo de conexión que impedía diagnosticar un
desacuerdo entre esas versiones. Ahora permite conectar y ejecutar lecturas;
la pantalla indica **Solo lectura** y deshabilita la casilla de envío.
Además, cualquier solicitud de envío se rechaza con `extension_reload_required`
antes de ejecutar código en Gmail, aunque la casilla se altere o el puente
tenga envíos habilitados. Pruebas del módulo real verifican ambos caminos.
Para enviar, ambas versiones deben coincidir: pulsa **Recargar** en la tarjeta
de Job Radar en `chrome://extensions`, cierra el trabajador y ábrelo desde su
icono. F5 en Gmail o en el trabajador no recarga el manifiesto instalado.

Si Gmail avisa de ventanas emergentes bloqueadas, permite únicamente las de
`mail.google.com` antes de repetir una lectura. Abrir el original con un clic normal
no demuestra que un clic programático pueda abrirlo. Si la pestaña sí aparece,
`original_tab_opener_mismatch` indica un origen de apertura distinto del esperado;
`original_tab_reused` indica que la pestaña ya existía antes de esa operación.
`original_tab_missing` indica que no se observó una coincidencia identificable;
por sí solo no prueba un bloqueo de ventanas emergentes ni falta de permisos.
Esos diagnósticos no autorizan adoptar, leer ni cerrar pestañas rechazadas.

La versión 0.1.6 admite que el abridor del original sea la pestaña de Gmail
o el propio trabajador, identificado con `chrome.tabs.getCurrent()`. No acepta
otros abridores conocidos. Mantiene la exigencia de pestaña nueva, mismo dominio,
ruta de cuenta e identificador exacto del mensaje, y rechaza coincidencias ambiguas.
La regresión reproduce `original_tab_opener_mismatch` en el módulo completo con
Chrome simulado: buscar, abrir, identificar original, leer enlace/MIME y regresar.
Esta prueba no sustituye la validación en Gmail real; si el abridor es otro,
la operación seguirá bloqueada para su diagnóstico.

La versión 0.1.7 elimina la descarga adicional del original. Gmail ya muestra
el MIME completo en el elemento visible `pre#raw_message_text` de la pestaña
nueva y validada de **Mostrar original**, por lo que el trabajador lee ese
contenido directamente, con límite de 8 MiB y validación de `Message-ID` y

La versión 0.1.8 añade `mail draft`: comprueba si ya existe un borrador para el
destinatario, valida el contenido y PDF, y usa exclusivamente **Guardar y cerrar /
Save & close**. Después busca el borrador visible para confirmar que Gmail lo guardó.
No habilita la casilla de envío ni llama al control **Enviar**. Un desacuerdo entre
`Código` y `Extensión instalada` también bloquea la creación de borradores hasta que
se recargue la extensión; una recarga de Gmail no sustituye esa recarga.
`Date`. No pide `debugger`, no ejecuta peticiones separadas, no lee cookies,
historial, storage ni otras pestañas, y no modifica composición o envío. Si el
elemento falta, es ambiguo o no contiene MIME válido, la operación se detiene
sin reintento. La regresión usa un DOM sintético y el flujo completo del
trabajador; la observación en Gmail real confirmó el mismo elemento visible.

Si aparece `disable_conversation_view_then_retry`, todavía hay conversaciones
agrupadas. Esta versión las rechaza antes de descargar originales de esa página;
no las cuenta como correos individuales ni completa un historial parcial.

## Activación de un lote revisado

No hace falta OAuth. Sí hacen falta: extensión conectada, cuenta correcta,
evidencias vigentes, candidatos revisados y un lote ya preparado con `mail prepare`.
No se inventa un lote de 100 ni se utilizan destinatarios de ejemplo para completarlo.

Reinicia el puente con habilitación explícita:

```bat
uv run python scripts/chrome_bridge.py --account tu-correo@gmail.com --enable-send
```

Carga el nuevo archivo de conexión, marca **Permitir envíos del lote confirmado**
y conecta. En la segunda CMD:

```bat
uv run radar mail run --batch HASH_DEL_LOTE --confirm HASH_DEL_LOTE --bridge-dir .local/chrome/bridge --bridge-timeout 300 --daily-limit 500 --data-dir .local/mail-live
```

Usa el hash real devuelto por `mail prepare` en ambos lugares. Conserva siempre
el mismo `--data-dir`: cambiarlo desconecta el historial local anterior.
500 es el máximo configurado en la aplicación, no una garantía de que Gmail
autorice o entregue 500 mensajes. El script no evade límites ni bloqueos de Gmail.

## Controles y límites conocidos

- Un trabajador por extensión, una operación en vuelo y un envío por vez.
  Paralelizar clicks en el mismo buzón produce carreras; no se permite.
- La conexión sólo escucha en `127.0.0.1`, requiere token y origen de extensión,
  valida Host y fija el primer origen autenticado. No acepta peticiones de webs.
  Otros procesos locales con acceso a tus archivos siguen dentro del perímetro
  de confianza: no es aislamiento frente a malware instalado en tu PC.
- Cada solicitud queda reclamada en disco **antes** de entregarla a Chrome.
  Tras una caída no se vuelve a ejecutar; una respuesta perdida detiene el lote.
- Datos y archivos de conexión quedan bajo `.local`, ignorados por Git. Los logs
  imprimen identificador, método, resultado y contador; nunca MIME, token ni CV.
- Se cachea MIME inmutable por ID con límite de memoria. Cada historial se vuelve
  a buscar en Gmail; no se sustituyen las comprobaciones por una caché de consultas.
- Búsqueda limitada a 20 páginas, 20 MiB de resultados y plazo máximo 300 s por
  operación. Si no termina, bloquea; nunca declara un historial parcial como vacío.
- Opt-outs y rebotes de cualquier antigüedad siguen bloqueando. Un enviado antiguo
  por sí solo no bloquea: el periodo de duplicación es 48 horas.
- La fecha verificable mediante la interfaz es RFC `Date`, no `internalDate` de
  la API. Se identifica como `timestamp_source: rfc822_date`; no son equivalentes.
- La lectura MIME se obtiene del contenido visible de **Mostrar original**,
  no desde endpoints privados ni una descarga separada. Las pestañas auxiliares
  propias se cierran.
- Los cambios de interfaz, CAPTCHA, sesión caducada, borrador abierto, diálogos
  ambiguos, conversaciones agrupadas o falta del enlace Ver mensaje detienen el
  trabajo. No se borran borradores existentes ni se sortean controles de Google.
- No muevas ni utilices la pestaña de Gmail durante el trabajo. Detener se aplica
  después de la operación actual; no deshace un correo ya enviado.
- La reconciliación después de reiniciar el trabajador requiere revisar el correo
  real: esta primera versión no abre IDs arbitrarios que no haya observado en
  esa sesión. No borres `.dispatched` para forzar una repetición.

## Pruebas y fuentes

```bat
uv run pytest tests/test_chrome_bridge.py --tb=short -q
node --test tests/chrome_worker.test.mjs
uv run python scripts/quality.py all
```

Las pruebas de protocolo y seguridad son offline; las de HTTP usan sólo loopback.
No son pruebas E2E de Gmail. Los controles de calidad existentes incluyen las
comprobaciones de sintaxis y los tests de JavaScript en local y GitHub Actions.

Fundamentos: [scripts aislados de Chrome](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts)
y [peticiones de red de extensiones](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests).

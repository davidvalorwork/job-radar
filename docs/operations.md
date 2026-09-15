# Operación, errores e historial

[Índice](README.md) · [Uso](usage.md) · [Seguridad](../SECURITY.md)

## Consultas de estado

```sh
uv run radar status --data-dir .local/radar
uv run radar metrics --data-dir .local/radar
```

`status` devuelve `initialized`, `jobs`, `decisions`, `runs` y `audit_events`.
Los mapas incluyen únicamente los estados presentes; no supongas que siempre
aparecerán todas las claves. Una base ausente devuelve contadores vacíos y no se crea.

| Tabla | Información disponible | Límite |
| --- | --- | --- |
| `runs` | UUID, hash de configuración, inicio/fin y estado | El hash no permite reconstruir la configuración |
| `jobs` | Último payload, decisión, perfil, puntuación, motivos y última ejecución que cambió el registro | No conserva todas las versiones del texto |
| `audit_events` | Secuencia, UUID de evento, ejecución, fecha, tipo y atributos | No es un registro inmutable frente a un administrador local |

Los eventos son `run.started`, `job.evaluated`, `run.completed` y `run.failed`.
Cada observación registra `job_key`, fuente, hash del registro, decisión, motivos y
resultado `inserted`/`updated`/`unchanged`. Un registro sin cambios añade auditoría,
pero no actualiza `jobs.last_run_id`. Usa los eventos para conocer observaciones
posteriores. La identidad por URL no resuelve ofertas equivalentes con URLs distintas.

Para investigar con un cliente SQLite, abre la base **en modo de solo lectura**.
Estas consultas no muestran cuerpos de ofertas ni requieren editar tablas:

```sql
SELECT run_id, config_hash, started_at, finished_at, state
FROM runs ORDER BY started_at DESC LIMIT 20;

SELECT sequence, run_id, event_type, occurred_at, attributes
FROM audit_events ORDER BY sequence DESC LIMIT 50;
```

## Fallos y respuesta

| Código de salida | Significado | Qué comprobar |
| --- | --- | --- |
| `0` | Comando completado | No significa que se hayan enviado mensajes |
| `1` | `doctor` detectó runtime incompatible | Python y SQLite del entorno seleccionado |
| `2` | Entrada/configuración/selección inválida; también sintaxis CLI inválida | Ayuda, campos, tipos, límites, IDs y fechas con zona |
| `3` | Error del sistema de archivos | Rutas, permisos y espacio disponible |
| `4` | Error SQLite o de runtime controlado | Versión, esquema, bloqueo de la base y almacenamiento |

Los errores de negocio usan códigos JSON genéricos para no imprimir datos importados.
La CLI no muestra actualmente el detalle del registro inválido ni su número de línea;
los fallos tampoco tienen un campo de causa detallada en `runs`. Conserva los datos
de entrada localmente y reproduce con un archivo pequeño y ficticio para depurar.
No adjuntes datos reales, tokens o bases a incidencias públicas.

No se ocultan todos los errores posibles: interrupciones del proceso y errores de
programación no contemplados pueden terminar fuera del formato JSON. No existe un
reintento automático ni un registro de errores centralizado en este bootstrap.

## Importación parcial o interrumpida

1. Detén nuevas ejecuciones sobre esa entrada y consulta `status`/auditoría.
2. Corrige el JSONL o el problema de almacenamiento sin modificar tablas a mano.
3. Reejecuta el archivo con la configuración prevista; usa el mismo directorio de
   datos si quieres conservar deduplicación e historial.
4. Verifica la nueva ejecución y sus contadores. No marques la anterior como
   completada para ocultar el fallo.

Cada lote confirmado conserva ofertas y eventos juntos. Un error posterior marca
la ejecución como `failed` si todavía se puede escribir. El lote pendiente no se
guarda. Si el proceso muere o también falla la escritura del estado, puede quedar
`running`; no hay leases, detección automática de procesos muertos ni reanudación.
La nueva ejecución tiene otro UUID. Reprocesar no duplica ofertas sin cambios, pero
sí añade observaciones de auditoría; una reevaluación puede actualizar decisiones.

## Telemetría disponible

| Señal | Canal | Contenido |
| --- | --- | --- |
| `ingest.completed` | JSON en stderr | UUID y contadores de registros insertados/actualizados/sin cambios |
| Auditoría de negocio | SQLite | Estado durable y motivos por oferta |
| `radar_jobs{decision=...}` | Salida de `metrics` | Gauge de ofertas persistidas por decisión |
| `radar_audit_events` | Salida de `metrics` | Gauge del total de eventos persistidos |
| `radar_cloud_llm_enabled` | Salida de `metrics` | Gauge fijo a cero en esta versión |

No hay servidor HTTP de métricas, exportador remoto, panel Grafana ni trazas OTel.
Las etiquetas son acotadas; no contienen empresas, correos o URLs. Los contadores de
CPU/memoria de `doctor` son una instantánea, no un monitor ni un benchmark de rendimiento.

## Conservación y copias

La base no está cifrada. Usa un disco local y controla permisos y copias. Para una
copia consistente usa la API de backup de SQLite, o cierra todas las conexiones
antes de copiar los archivos del directorio de datos. No copies únicamente el
archivo principal mientras haya un escritor activo ni borres los archivos WAL/SHM
para resolver bloqueos. No hay comando de purga o política automática de retención.

Una base con versión de esquema desconocida se rechaza; no cambies `user_version`
para forzar su apertura. Aún no existen migraciones entre versiones de datos.

## CI frente a validación local

El workflow está configurado para Windows y Linux; su existencia no prueba una
ejecución exitosa. Si un job no llega a iniciar, revisa sus anotaciones de GitHub
antes de atribuirlo al código. Los problemas de cuenta o disponibilidad del servicio
son externos al runtime offline y no requieren añadir APIs de pago al proyecto.

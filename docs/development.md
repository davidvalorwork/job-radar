# Desarrollo y extensión modular

[Índice](README.md) · [Arquitectura](architecture.md) · [Arquitectura poliglota](polyglot-architecture.md) · [Hoja de ruta](roadmap.md)

## Mapa de responsabilidades

| Área | Responsabilidad | No debe hacer |
| --- | --- | --- |
| `domain/` | Entidades inmutables, clasificación y guardas de contacto | Importar librerías de infraestructura, abrir conexiones o leer configuración |
| `application/` | Casos de uso, planificación y puertos | Importar adaptadores concretos |
| `adapters/inbound/` | Parseo de argumentos CLI | Instanciar base de datos o implementar reglas de negocio |
| `adapters/outbound/` | JSONL, YAML, SQLite, reloj y telemetría | Decidir requisitos del producto fuera del dominio |
| `bootstrap.py` | Construir y conectar adaptadores y casos de uso | Convertirse en un segundo motor de clasificación |

Las rutas son relativas a `src/job_radar/`. Un test AST comprueba las dependencias
permitidas en dominio/aplicación. Es una comprobación de imports explícitos, no una
garantía contra cualquier forma de dependencia dinámica.

## Puertos actuales

Contratos en [application/ports.py](../src/job_radar/application/ports.py):

| Puerto | Operaciones | Contrato esperado |
| --- | --- | --- |
| `Clock` | `now() -> datetime` | Fecha con zona; reloj fijo en pruebas |
| `JobReader` | `read() -> Iterable[Job]` | Entrada incremental; validar y acotar en el adaptador |
| `JobRepository` | `begin_run`, `save_batch`, `finish_run` | Persistir estado y auditoría atómicamente por operación/lote |
| `Telemetry` | `event(name, fields)` | Diagnóstico con IDs/contadores, sin datos sensibles |

Se usan `Protocol`: no es necesario heredar del puerto para satisfacer su interfaz.
`JsonTelemetry` tolera errores de escritura del canal de diagnóstico; un adaptador
nuevo debe definir y probar su comportamiento ante fallos, no suponer que cualquier
excepción será absorbida por el caso de uso.

## Añadir una capacidad

1. Define el comportamiento y su límite de autorización. No habilites red/envío
   mediante un cambio accidental de valores por defecto.
2. Añade entidades o reglas puras solo si el dominio las necesita. Inyecta reloj y
   dependencias para reproducir resultados.
3. Define el puerto mínimo en aplicación y el caso de uso que lo consume. Los
   contratos existentes son síncronos; no introduzcas asyncio solo en un adaptador
   esperando que el orquestador actual lo ejecute en paralelo.
4. Implementa el adaptador con límites, resultados tipados y pruebas de fallos.
5. Conecta en `bootstrap.py` y expón una fase con la CLI si procede.
6. Actualiza configuración, documentación, pruebas y política de migración cuando
   cambie un contrato persistido.

Para el próximo adaptador OpenCLI: comprobar comandos instalados y sesiones
autorizadas; ejecutar argumentos permitidos sin shell; limitar tiempo, bytes,
procesos y uso de cada sesión. Los `command_refs` del catálogo no bastan para ejecutar
comandos a ciegas. Añadir búsqueda no autoriza envío de mensajes.

## Decisiones y límites del diseño

| Decisión | Motivo | Coste o condición para revisarla |
| --- | --- | --- |
| Python + uv | Ecosistema local, contratos tipados y dependencias fijadas | Medir cuellos de botella antes de añadir otro lenguaje |
| Núcleo determinista | Operación sin llamadas a modelos y decisiones explicables | Menos flexibilidad semántica; requiere términos bien configurados |
| SQLite local | Sin servidor adicional y auditoría junto a los datos | Concurrencia de escritura limitada; futuro escritor único |
| JSONL incremental | Intercambio por fases y memoria acotada | Límites de tamaño y normalización explícita |
| CLI por fase | Reutilizar casos de uso desde scripts y orquestadores | No equivale todavía a un scheduler durable |
| Telemetría local mínima | Diagnosticar sin desplegar servicios permanentes | OTel/paneles solo cuando existan workers que observar |
| Adaptación web unificada futura | Evitar un cliente de API distinto por fuente | Capacidades/versiones/sesiones deben verificarse en cada instalación |

No hay benchmark que demuestre saturación eficiente de todos los núcleos. El diseño
futuro combina concurrencia I/O acotada, locks por sesión y procesos CPU solo donde
las mediciones lo justifiquen. No ejecutar procesos ilimitados para aparentar escala.

## Verificación y mantenimiento

```sh
uv run python scripts/quality.py all
```

Consulta [calidad y Git Flow](quality.md) para hooks, comandos por fase, cobertura
mínima del 95%, informes, ramas, títulos de PR y activación de protecciones remotas.

Las pruebas usan datos ficticios y reloj fijo donde la fecha afecta reglas. Cubren
la CLI offline, límites de entrada, validación estricta, prioridades, política de
48 horas, deduplicación, importaciones parciales, rollback conjunto con auditoría
y fronteras de imports. No prueban sesiones reales ni entregas de correos.

CodeGraph es opcional y local: con su CLI instalada, `codegraph init -i` inicializa
el índice; `codegraph index` lo reconstruye y `codegraph status` informa de su estado.
`.codegraph/` está excluido de Git. No publiques índices, bases ni configuraciones
privadas. Mantén `uv.lock` y metadatos coherentes y no instales integraciones como
efecto secundario de una prueba.

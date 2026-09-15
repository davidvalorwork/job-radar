# Documentación de Job Radar

Referencia del estado **0.1.0**: base local, recolección opt-in y envío Gmail directo/asistido,
licencia Apache-2.0 y datos de ejemplo ficticios. Los comandos de la aplicación no
llaman a modelos ni a APIs de pago. La instalación de dependencias sí puede requerir
Internet; el consumo del asistente de desarrollo es independiente.

## Por dónde empezar

| Necesidad | Documento |
| --- | --- |
| Instalar, ejecutar y entender las salidas | [Uso de la CLI](usage.md) |
| Cambiar roles, fuentes, filtros y presupuestos | [Configuración](configuration.md) |
| Consultar historial, errores, métricas y recuperar una importación | [Operación](operations.md) |
| Entender puertos, añadir adaptadores y comprobar cambios | [Desarrollo](development.md) |
| Aplicar formato, lint, tests y Git Flow | [Calidad y workflow de contribución](quality.md) |
| Instalar OAuth, recolectar con caché y usar plantillas sin LLM | [Ejecución sin asistente](autonomous.md) |
| Reducir contexto, revisar fichas acotadas y medir consumo | [Eficiencia de tokens](token-efficiency.md) |
| Preparar, enviar y reconciliar correos desde la CLI | [Envío Gmail](mail.md) |
| Automatizar Gmail en Chrome sin IA ni OAuth propio (experimental) | [Trabajador Chrome](chrome-worker.md) |
| Ver límites entre capas y flujo completo | [Arquitectura](architecture.md), en inglés |
| Integrar React, Angular, Node.js, Django, Python y Lambda sin duplicar lógica | [Arquitectura poliglota](polyglot-architecture.md) |
| Continuar con búsquedas, paralelismo y envío | [Hoja de ruta](roadmap.md), en inglés |
| Proteger datos y contribuir | [Seguridad](../SECURITY.md) y [contribuciones](../CONTRIBUTING.md) |

## Qué funciona y qué no

| Área | Implementado | Pendiente |
| --- | --- | --- |
| Descubrimiento | Catálogo de 27 fuentes, plan a archivo, búsqueda Exa y lectura Jina por rutas Agent Reach | Sesiones autenticadas y extracción de ofertas/contactos |
| Ofertas | Importar JSONL, normalizar URL, clasificar DevOps, Full Stack AI, backend/frontend web y serverless | Extracción web y procedencia completa de evidencias |
| Procesamiento | Recolección paralela acotada, caché SQLite, importación idempotente | Colas con leases, adaptación de workers y extracción completa |
| Filtros | Roles, remoto, antigüedad, idioma y empresas excluidas | Herencia por fuente/página y relajación automática |
| Contactos | Plantillas de hechos revisados, OAuth directo, cola, historial, cuota y espera de 48 horas | Autorizar cada instalación, worker residente, mejor detección de bajas |
| Observabilidad | Auditoría SQLite, log JSON de éxito y métricas de estado | Trazas OpenTelemetry, recolector y paneles |

La cuota local permite hasta 500 destinatarios por 24 horas, sin garantizar que Gmail
permita esa cantidad, disponer de suficientes candidaturas ni entregabilidad. OAuth
directo necesita autorización propia;
solo el transporte legacy necesita un relay autorizado.
Superar el plazo de 48 horas no programa un
reenvío. No hay credenciales ni sesiones personales en los ejemplos.

## Flujo disponible

```text
YAML + catálogo ──> plan --output ──> collect --allow-network ──> caché local

JSONL local ──> validar ──> evaluar reglas ──> lotes SQLite + auditoría
                                                    └──> estado y métricas
```

Planificación e importación son entradas independientes: la salida de `plan` no
contiene ofertas y no se puede pasar directamente a `ingest`. Sí puede pasarse a
`collect`; sus resultados web todavía requieren normalización y revisión.

## Criterio para continuar

El siguiente módulo pendiente es la extracción determinista de ofertas/contactos
con procedencia desde la caché. No añadir un cliente de API por bolsa ni presentar
una página descargada como una candidatura ya verificada.

# Plan: videos más fieles al guion y generación masiva

Estado: primera entrega implementada el 27 de julio de 2026.

## Estado de implementación

La primera entrega de extremo a extremo ya está integrada:

- `VideoPlan` y `ScenePlan` persistentes, alineados con el SRT o con la duración real del audio.
- Enriquecimiento estructurado de acciones, protagonistas, lugares, objetos, exclusiones y continuidad.
- Búsqueda por escena, alternativas y análisis CLIP de fotogramas reales de hasta tres candidatos.
- Selección del mejor intervalo temporal dentro del clip y procedencia guardada en `materials.json`.
- Storyboard previo al render con reproducción del intervalo exacto, puntuación, advertencias, aprobación y re-búsqueda individual.
- Bloqueo y reutilización de escenas aprobadas durante una sustitución.
- Detección de clips ausentes, repetidos, poco relacionados y discontinuidades en `quality-report.json`.
- Previsualización completa de la narración reutilizable sin una segunda llamada TTS.
- Caché persistente de búsquedas de stock, con expiración, escritura atómica y protección de URLs firmadas.
- Cola SQLite durable con leases, recuperación después de reinicios, workers acotados y campañas de hasta 150 tareas.
- Los lotes de YouTube comparten la cola durable y la generación de 84–150 ideas usa bloques adaptativos.
- Ruta FFmpeg de una sola pasada para storyboards compatibles, manteniendo el flujo MoviePy como respaldo.

La distribución hacia varias máquinas queda preparada mediante trabajos idempotentes, IDs de campaña, leases y almacenamiento trazable. El cambio futuro de SQLite a un broker central y de archivos locales a almacenamiento de objetos sigue siendo una fase posterior; no es necesario para operar lotes de 100–150 videos en una estación.

## Diagnóstico actual

La pérdida principal de fidelidad no ocurre al escribir el guion, sino al convertirlo en video:

1. El guion se reduce a una lista plana de búsquedas de stock.
2. Se generan 12 búsquedas aunque la duración y complejidad del guion cambien.
3. Los clips se distribuyen casi por partes iguales, no según la duración real de cada frase o escena.
4. Durante la descarga se pierde la relación entre escena, búsqueda, clip y fragmento narrado.
5. El montaje suele usar fragmentos consecutivos del archivo encontrado, no necesariamente el momento visual más relevante.
6. Si falta metraje, se repiten clips para cubrir el audio sin advertir que la escena perdió calidad.
7. El ranking evalúa principalmente la miniatura del proveedor, no varios fotogramas del clip descargado.
8. Existe soporte interno para objetos requeridos, elementos excluidos y análisis avanzado de clips, pero el flujo normal casi no lo aprovecha.

Para generación masiva, el cuello de botella medido es el render:

- Render de video: promedio aproximado de 420 segundos por ejecución; máximo observado de 1.183 segundos.
- Obtención de materiales: promedio aproximado de 51 segundos.
- Guion, búsquedas, audio y metadatos: normalmente pocos segundos.

Además, los trabajos iniciados desde la interfaz viven en memoria del proceso, tienen pocos trabajadores y no forman una cola durable común con el API y los lotes de YouTube.

## Idea central

Crear un `VideoPlan` estructurado y conservarlo durante toda la cadena:

`guion → escenas temporizadas → candidatos → clips seleccionados → timeline → validación → render`

## Requisitos prioritarios confirmados

Estas mejoras se consideran obligatorias dentro de la hoja de ruta y no simples optimizaciones opcionales:

1. Crear un storyboard estructurado con escenas, tiempos, acciones, objetos, lugares y protagonistas.
2. Sincronizar cada escena con los tiempos reales de la narración.
3. Mantener continuidad de protagonistas, lugares y objetos durante todo el video.
4. Analizar fotogramas reales de los videos candidatos, no solamente las miniaturas de los proveedores.
5. Mostrar una previsualización por escena antes de iniciar el render final.
6. Detectar clips repetidos, material poco relacionado y sustituciones visuales deficientes.
7. Implementar una cola persistente y recuperable para lotes iniciales de 100–150 videos.
8. Reducir las recodificaciones y pasadas de FFmpeg, actualmente el principal cuello de botella.
9. Preparar una arquitectura distribuida que permita crecer posteriormente hacia miles de videos.

La implementación deberá respetar este orden conceptual:

`storyboard fiel → validación visual → render eficiente → cola masiva → distribución`

No se considerará completo el escalado si solo aumenta el límite de videos sin mantener la trazabilidad entre narración, escena y material seleccionado.

Cada escena debería incluir como mínimo:

- Identificador estable.
- Fragmento exacto de narración.
- Tiempo inicial, final y duración.
- Descripción visual de lo que debe verse.
- Consulta de búsqueda principal y consultas alternativas.
- Protagonista, lugar, acción y objetos requeridos.
- Elementos que no deben aparecer.
- Tipo de plano, movimiento y prioridad narrativa.
- Reglas de continuidad con las escenas vecinas.
- Clip elegido, intervalo exacto del clip y puntuación de confianza.

Esto permitiría saber en todo momento qué imagen representa cada parte del guion y reemplazar solamente una escena débil.

## Mejoras de fidelidad

### 1. Planificar escenas usando los tiempos reales del audio

Generar primero el audio y sus marcas de palabras o frases. Dividir el guion en escenas de duración variable, normalmente de 2 a 5 segundos, respetando:

- Cambios de acción, sujeto, lugar o intención.
- Frases que requieren más tiempo visual.
- Pausas y momentos de énfasis.
- Ritmo configurable según el estilo del video.

La cantidad de escenas dejaría de ser un valor fijo: dependería de la duración y densidad narrativa.

### 2. Escribir guiones que puedan representarse visualmente

Agregar una evaluación previa que detecte:

- Frases abstractas difíciles de mostrar con stock.
- Acciones imposibles de encontrar.
- Cambios de protagonista o lugar no intencionales.
- Escenas que necesitan material propio o generado.

El sistema podría proponer una versión visualizable del guion sin cambiar su significado.

### 3. Usar salidas estructuradas del modelo

Solicitar el plan en un esquema validado, no como texto libre. Si faltan campos, escenas o duraciones, repararlo automáticamente antes de buscar material. Esto evita listas incompletas y respuestas ambiguas.

### 4. Buscar varios candidatos por escena

Usar una recuperación escalonada:

1. Buscar ampliamente en todos los proveedores.
2. Filtrar por orientación, resolución, duración y licencia.
3. Rankear miniaturas para descartar candidatos claramente malos.
4. Descargar los mejores candidatos.
5. Analizar varios fotogramas reales de cada video.
6. Seleccionar el intervalo temporal que mejor coincide con la escena.

No basta con elegir el primer resultado ni con juzgar una sola miniatura.

### 5. Seleccionar el mejor fragmento dentro de cada clip

Muestrear el clip cada cierto intervalo y puntuar ventanas de 2 a 5 segundos por:

- Coincidencia con la escena.
- Presencia de objetos y acciones requeridas.
- Ausencia de elementos prohibidos.
- Nitidez, movimiento y estabilidad.
- Seguridad al recortar a formato vertical.
- Rostros y texto cortado.

El montaje usaría el mejor intervalo, en vez del inicio o del siguiente trozo disponible.

### 6. Aplicar restricciones detalladas

Aprovechar y ampliar los campos existentes de objetos requeridos y elementos excluidos. Las reglas deben ser generales, no casos escritos manualmente para unas pocas edades o lugares.

Ejemplo: si la escena pide “una adolescente abre una carta en su habitación”, validar por separado sujeto, edad aproximada, acción, objeto y entorno.

### 7. Mantener continuidad narrativa

Crear una ficha de continuidad por video:

- Descripción estable del protagonista.
- Edad aproximada y apariencia cuando sean relevantes.
- Lugar, época, clima y hora.
- Objetos persistentes.
- Paleta y estilo visual.

El ranking debe penalizar cambios bruscos entre escenas contiguas. Para stock, la continuidad será aproximada; si es indispensable, conviene usar material propio o generación visual consistente.

### 8. Usar una jerarquía explícita de alternativas

Cuando no exista un clip suficientemente fiel:

1. Probar consultas alternativas.
2. Ampliar la búsqueda sin eliminar los requisitos críticos.
3. Usar un plano simbólico compatible, marcado como sustitución.
4. Usar imagen generada o material del usuario con movimiento de cámara.
5. Marcar la escena para revisión.

Nunca sustituir silenciosamente una escena por metraje no relacionado.

### 9. Previsualizar un storyboard antes del render

Mostrar una tarjeta por escena con:

- Narración y tiempos.
- Miniatura o previsualización del fragmento elegido.
- Consulta utilizada.
- Puntuación de fidelidad.
- Motivos de selección o advertencias.
- Acciones para bloquear, reemplazar o volver a buscar solo esa escena.

Así se evita descubrir los errores después de un render de varios minutos.

### 10. Validar el video terminado por escena

Después del montaje, comprobar:

- Correspondencia entre narración y visual.
- Continuidad del protagonista y lugar.
- Clips repetidos.
- Recortes verticales problemáticos.
- Legibilidad de subtítulos.
- Volumen de voz, música y silencios.

Las escenas que no superen el mínimo deberían volver a buscarse y renderizarse de forma incremental, sin reconstruir todo el video.

### 11. Controlar repeticiones y diversidad

Guardar la huella de los clips usados y limitar:

- Repetición dentro del mismo video.
- Repetición entre videos de una campaña.
- Reutilización excesiva de la misma consulta o composición visual.

También conviene alternar tipos de plano y movimientos de forma narrativa, no aleatoria.

### 12. Separar fidelidad de creatividad

Ofrecer perfiles:

- `Estricto`: representa literalmente el guion y rechaza sustituciones dudosas.
- `Equilibrado`: permite recursos simbólicos cuando ayudan al ritmo.
- `Creativo`: acepta metáforas visuales y mayor variación.

Esto evita que una única lógica intente servir a todos los tipos de contenido.

## Mejoras para generación masiva

### 1. Cola durable y unificada

Reemplazar los trabajos en memoria por una cola persistente en SQLite al inicio y, si se distribuye, Redis u otro broker. La misma cola debería atender WebUI, API y lotes de YouTube.

Debe soportar:

- Reinicio sin perder trabajos.
- Reintento idempotente por etapa.
- Pausa, continuación y cancelación.
- Prioridades y límites por campaña.
- Estado y error detallado por video y escena.

### 2. Separar trabajadores por tipo de recurso

Usar grupos independientes para:

- LLM y planificación.
- Descarga de materiales.
- Inferencia visual y ranking.
- Render.
- Subida a plataformas.

El planificador asignaría concurrencia según CPU, GPU, RAM, disco y límites externos. El render no debería bloquear la planificación del siguiente video.

### 3. Solapar etapas entre videos

Mientras se renderiza el video N:

- Descargar materiales del N+1.
- Generar audio y escenas del N+2.
- Preparar guiones del N+3.

Este pipeline aprovecha mejor el equipo sin lanzar demasiados renders simultáneos.

### 4. Catálogo global de materiales

Crear una biblioteca reutilizable con:

- Proveedor, ID, URL y licencia.
- Archivo local y checksum.
- Duración, resolución y orientación.
- Embeddings de varios fotogramas.
- Etiquetas, personas, objetos y lugares detectados.
- Ventanas temporales ya puntuadas.
- Historial de uso por campaña.

Esto reduce búsquedas, descargas e inferencias duplicadas y mejora la diversidad.

### 5. Reanudar todas las etapas

Ampliar el manifiesto actual para guardar:

- `VideoPlan`.
- Candidatos y puntuaciones.
- Descargas completadas.
- Intervalos seleccionados.
- Segmentos normalizados.
- Timeline final.
- Resultado de validación.
- Render y publicación.

Un fallo tardío no debería repetir llamadas al modelo, descargas o análisis ya terminados.

### 6. Reducir recodificaciones

El render es el mayor costo actual. Ideas:

- Combinar recorte, escala, transiciones, audio, música y subtítulos en el menor número posible de pasadas FFmpeg.
- Cachear segmentos normalizados por formato.
- Usar codificación por hardware cuando pase pruebas de calidad y compatibilidad.
- Evitar crear y recodificar archivos temporales por cada operación.
- Renderizar segmentos independientes y concatenarlos cuando sea seguro.

Antes de elegir una optimización, medir calidad, tiempo y tamaño con un conjunto fijo de videos.

### 7. Render incremental

Guardar una salida por escena o grupo pequeño de escenas. Si cambia un clip, subtítulo o transición, volver a renderizar solamente esa parte y reconstruir el contenedor final.

### 8. Presión inversa y límites globales

No crear un hilo independiente sin límite por lote. El planificador debe frenar nuevas tareas cuando:

- El disco temporal esté lleno.
- La memoria o GPU alcance el umbral.
- El proveedor reduzca el ritmo permitido.
- La cola de render tenga demasiada espera.

### 9. Perfiles de producción

- `Calidad`: más candidatos y validación profunda; menor concurrencia.
- `Equilibrado`: validación completa con límites moderados.
- `Masivo`: mayor reutilización de caché y análisis selectivo, conservando un umbral mínimo de fidelidad.

El modo masivo no debe significar “sin control de calidad”.

### 10. Campañas en vez de listas sueltas

Tratar 100, 150 o más videos como una campaña:

- Reglas de estilo compartidas.
- Presupuesto de costo y tiempo.
- Control de temas y títulos repetidos.
- Exclusión de clips ya usados.
- Prioridades.
- Programación de publicación separada de la producción.
- Dashboard paginado, filtros y reintentos parciales.

### 11. Escalado distribuido posterior

Cuando una sola máquina ya esté optimizada, permitir trabajadores remotos con:

- Cola central.
- Almacenamiento de objetos compartido.
- Capacidades declaradas por trabajador.
- Etapas idempotentes.
- Arrendamiento y recuperación de tareas abandonadas.

Distribuir antes de reducir las recodificaciones solo multiplicaría el costo del diseño actual.

## Orden recomendado de implementación

### Fase 0 — Integración selectiva del proyecto original

No fusionar ni reemplazar el fork completo con `harry0703/MoneyPrinterTurbo`. Las ramas modifican simultáneamente archivos críticos como `task.py`, `material.py`, `video.py` y `webui/Main.py`; una actualización general podría eliminar o degradar las funciones personalizadas.

Portar manualmente y validar estas mejoras:

1. Correcciones de seguridad de Pixabay:
   - No registrar API keys ni credenciales del proxy.
   - Detectar Cloudflare Challenge.
   - Distinguir límites `429`, errores HTTP y respuestas no JSON.
2. Caché persistente de búsquedas de materiales:
   - Conservar proveedor, URL, duración, `thumbnail_url`, consulta y metadatos públicos.
   - No guardar enlaces firmados o credenciales de Coverr.
   - Volver a calcular la puntuación CLIP; no reutilizar una puntuación obsoleta.
   - Aplicar expiración y escritura atómica.
3. Procedencia de materiales:
   - Guardar proveedor, ID del recurso, página pública, autor, resolución y consulta.
   - Integrar esta información en `TaskManifest` y posteriormente en cada escena del storyboard.
4. Previsualización completa y reutilizable de la narración:
   - Estimar duración antes de buscar materiales.
   - Reutilizar el audio y las marcas temporales si la voz, el texto y los parámetros no cambiaron.
   - Evitar llamadas TTS duplicadas.
5. Limpieza deduplicada de archivos temporales:
   - Eliminar cada ruta una sola vez.
   - Ignorar archivos que ya no existan.
   - Registrar errores reales de permisos o disco.

Conservar del fork:

- Ranking visual CLIP y búsquedas combinadas entre proveedores.
- Objetos requeridos, elementos excluidos y validaciones estrictas.
- Doce términos ordenados mientras se sustituye la lista plana por el storyboard.
- Integraciones y lotes de YouTube y TikTok.
- Generación ampliada de ideas.
- Recuperación mediante manifiestos.
- Medición de uso de API.
- Rendimiento adaptativo de CPU, GPU, red y render.
- Correcciones personalizadas de Streamlit.

No adoptar literalmente:

- El trabajador WebUI del original con una sola tarea concurrente y estado únicamente en memoria.
- La versión original completa de `material.py`, porque perdería metadatos y ranking visual del fork.
- La versión original completa de `task.py` o `video.py`.
- La reducción de búsquedas ordenadas de doce a ocho.
- Cualquier caché que descarte `thumbnail_url`, porque impediría el ranking CLIP.

Criterio para completar esta fase: obtener las mejoras de seguridad, caché, procedencia, narración y limpieza sin perder ninguna prueba o función personalizada del fork.

### Fase 1 — Fundamento de fidelidad

- Definir `VideoPlan`, `ScenePlan` y `SelectedClip`.
- Crear escenas con tiempos reales del audio.
- Conservar `scene_id` y metadatos hasta el timeline.
- Usar objetos requeridos, exclusiones y consultas alternativas.
- Seleccionar ventanas reales dentro de los clips.

Resultado esperado: cada segmento del guion tiene una representación visual trazable.

### Fase 2 — Storyboard y control de calidad

- Previsualización por escena.
- Bloquear, reemplazar y volver a buscar escenas.
- Puntuación de fidelidad y continuidad.
- Detección de clips repetidos.
- Validación posterior y reparación selectiva.

Resultado esperado: los errores visuales se detectan antes o sin repetir el render completo.

### Fase 3 — Producción durable

- Cola persistente común.
- Manifiesto completo por etapas.
- Trabajadores separados por recurso.
- Pipeline solapado entre videos.
- Backpressure y recuperación tras reinicio.

Resultado esperado: lotes de 100–150 videos pueden ejecutarse durante horas o días sin depender de una sesión abierta.

### Fase 4 — Rendimiento de render

- Benchmark reproducible.
- Menos pasadas FFmpeg.
- Caché de segmentos.
- Render incremental.
- Codificación por hardware validada.

Resultado esperado propuesto: reducir el promedio de render entre 30 % y 50 % respecto de la base actual, sin degradar la salida.

### Fase 5 — Campañas y distribución

- Biblioteca global de activos y embeddings.
- Diversidad entre videos.
- Presupuestos, ETA y métricas por campaña.
- Trabajadores remotos si la demanda lo justifica.

Resultado esperado: crecer de cientos a miles de videos sin cambiar el modelo de datos.

## Métricas recomendadas

Fidelidad:

- Porcentaje de escenas aprobadas sobre fotogramas reales.
- Cobertura temporal del guion.
- Escenas con sustitución simbólica o sin material.
- Continuidad entre escenas.
- Repetición de clips dentro del video y de la campaña.
- Valoración humana en un conjunto de guiones de prueba.

Escala:

- Segundos de render por minuto final.
- Tiempo total y costo por video.
- Aciertos de caché.
- Descargas e inferencias evitadas.
- Reintentos por etapa.
- Uso máximo de CPU, GPU, RAM y disco.
- Videos terminados por hora.

## Criterios de aceptación iniciales

- Toda frase o bloque narrativo tiene una escena y un intervalo del timeline.
- No queda ningún hueco visual no explicado mayor a un segundo.
- Las escenas estrictas no cambian protagonista, acción u objeto crítico sin advertencia.
- Los clips repetidos se detectan antes del render.
- La cola sobrevive a un reinicio y continúa desde la última etapa válida.
- Un lote de 100–150 videos mantiene recursos acotados y no duplica trabajos.
- Una escena rechazada puede reemplazarse sin regenerar el proyecto completo.
- Las mejoras de rendimiento se comparan contra la base observada de aproximadamente 420 segundos por render.

## Primera entrega aconsejada

No empezar por aumentar solamente el límite numérico. La primera entrega debería ser un prototipo de extremo a extremo para un video:

1. Guion y audio.
2. `VideoPlan` con tiempos.
3. Tres o más candidatos por escena.
4. Ranking de fotogramas reales.
5. Selección del mejor intervalo.
6. Storyboard verificable.
7. Timeline que conserve la trazabilidad.

Después de validar que esa salida es más fiel, conectar el mismo modelo de datos a la cola masiva. Así la escala multiplica un flujo correcto en lugar de multiplicar errores genéricos.

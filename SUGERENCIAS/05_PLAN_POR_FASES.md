# Plan por fases

## Fase 0 — Preparar el terreno

Objetivo: que el proyecto actual sea una base confiable.

- Corregir la ubicacion/lectura de `upload_post_*`.
- Confirmar el contrato actual de Upload-Post con una cuenta de prueba.
- Agregar YouTube a las plataformas solo cuando su OAuth este conectado.
- Probar TikTok y YouTube con privacidad segura.
- Confirmar caption, descripcion, hashtags y señalizacion de contenido IA.
- Mantener la suite de 195 pruebas en verde.
- Medir tiempo, CPU, GPU, memoria y disco de diez renders consecutivos.

Criterio de salida: diez renders validos y diez subidas de prueba, sin duplicados y con URL registrada.

## Fase 1 — Semiautomatica

Objetivo: automatizar todo excepto la aprobacion final.

- IA propone temas.
- Sistema genera y valida.
- Tu recibes video, titulo y caption en Telegram/Discord.
- Apruebas o descartas.
- El sistema publica y verifica.

Duracion recomendada: una semana, dos videos al dia.

Esta fase descubre errores editoriales que las pruebas tecnicas no ven.

## Fase 2 — Autonoma con red de seguridad

Objetivo: publicar temas de bajo riesgo sin intervencion.

- Lista blanca de pilares.
- Maximo dos publicaciones diarias.
- Publicacion automatica solo si todas las reglas pasan.
- Temas de seguridad/finanzas dudosos van a revision humana.
- Pausa automatica tras tres fallos consecutivos.
- Resumen diario con URLs y errores.

Criterio de salida: dos semanas sin duplicados, silencios, archivos rotos o publicaciones fuera de horario.

## Fase 3 — Optimizacion

Objetivo: aprender del canal.

- Recoger metricas a 24 h y 7 dias.
- Comparar pilares, ganchos, duracion y voz.
- Subir a 3-4 videos diarios solo si la audiencia responde bien.
- Generar el banco semanal usando datos, no intuicion aislada.
- Programar horas por plataforma.

## Fase 4 — Escala opcional

Solo si hay evidencia:

- segundo canal o segundo idioma;
- Redis y workers separados;
- almacenamiento externo;
- panel editorial;
- APIs oficiales para reducir dependencia de Upload-Post;
- pruebas visuales mas avanzadas.

No construiria esta fase anticipadamente. Una cuenta, SQLite y una cola local pueden llegar muy lejos.

## Politica de reintentos propuesta

- Idea rechazada: elegir otra; no “forzar” la misma.
- LLM/TTS/stock temporal: 3 intentos con espera creciente.
- Render: 2 intentos; despues revision.
- Subida: 3 intentos con la misma idempotency key.
- Error de autenticacion, cuota o politica: no reintentar en bucle; pausar esa plataforma y alertar.
- Error de una plataforma: las otras conservan su resultado; no volver a publicarlas.

## Horario sugerido para Chile

Usar `America/Santiago`, no un UTC fijo, para respetar cambios horarios. Empezaria probando dos ventanas separadas, por ejemplo almuerzo y noche. Las horas definitivas deben salir de las analiticas de audiencia, no de una regla universal.


# Diagnostico del proyecto actual

## Lo que ya esta resuelto

El proyecto esta mucho mas cerca de la automatizacion de lo que parece:

- Tiene WebUI, CLI y API FastAPI.
- `POST /videos` crea trabajos en segundo plano.
- `GET /tasks/{task_id}` permite consultar progreso y recuperar rutas de salida.
- Genera guion, terminos de busqueda, voz, subtitulos, materiales y video final.
- Genera metadatos sociales separados para TikTok, Instagram Reels y YouTube Shorts.
- Usa Pexels y Pixabay a la vez, ajuste vertical `cover`, coincidencia de materiales con el guion y codificacion `h264_nvenc`.
- Tiene una integracion propia con Upload-Post y una funcion para consultar el estado de una subida.
- La suite actual pasa: 195 pruebas, 7 omitidas, ejecutadas en modo UTF-8.

La configuracion activa usa OpenAI con `gpt-4.1-mini`, voz `es-MX-DaliaNeural-Female`, formato 9:16 en los trabajos observados y subtitulos inferiores. Las diez carpetas historicas estan marcadas manualmente como “Ya subido”, una pista clara de que el seguimiento de publicacion sigue siendo humano.

## Lo que falta para que sea realmente autonomo

1. **Inventor de temas con memoria.** El flujo empieza con `video_subject`; no existe un agente que genere ideas ni que compruebe duplicados.
2. **Planificador editorial.** No hay calendario, franjas horarias, pilares de contenido ni limite diario por plataforma.
3. **Control de calidad.** Que exista `final-1.mp4` no demuestra que tenga audio, duracion correcta, texto legible o escenas relacionadas.
4. **Publicacion verificable.** La subida se inicia, pero no se consulta despues hasta obtener URL final o error definitivo.
5. **Reintentos seguros.** No se envia una clave de idempotencia; repetir una peticion tras un timeout puede duplicar publicaciones.
6. **Estado persistente.** Redis esta desactivado. La cola y parte del estado viven en memoria y no son una base editorial duradera.
7. **Metricas y aprendizaje.** No se recuperan vistas, retencion, likes o comentarios para decidir los siguientes temas.
8. **Limpieza.** `storage/cache_videos` tiene 618 archivos y aproximadamente 6,15 GB; `storage/tasks` suma unos 809 MB. A produccion continua crecera sin limite.

## Hallazgos importantes antes de automatizar

### Configuracion del publicador desalineada

Las opciones `upload_post_*` estan dentro de `[ui]` en `config.toml`, pero `UploadPostService` las busca en `config.app`. En el estado actual el servicio no vera esos valores. Ademas, el publicador se instancia una sola vez al importar el modulo, por lo que cambios de configuracion durante la ejecucion pueden no reflejarse hasta reiniciar.

Actualmente:

- `upload_post_enabled = false`
- `upload_post_auto_upload = false`
- Plataformas configuradas: TikTok e Instagram; YouTube no esta incluido.

### Metadatos incompletos por plataforma

Para YouTube se genera titulo, descripcion y etiquetas. Para TikTok/Instagram el flujo automatico usa principalmente `video_subject` como titulo; no ensambla de forma explicita el caption y hashtags generados. Tampoco envia `is_aigc` para TikTok, aunque Upload-Post lo admite.

La documentacion actual de Upload-Post usa el campo global `description` para YouTube, mientras el codigo envia `youtube_description`. Hay que probar el contrato real de la cuenta/API antes de produccion.

### Exito de render no equivale a exito de publicacion

Aunque una subida falle, el trabajo de video termina marcado como completo. Los resultados de subida quedan anexados, pero no existe una cola de reparacion ni una alarma. `check_status()` existe, pero no encontre llamadas desde el resto del proyecto.

### Riesgo si se expone la API

Las dependencias de autenticacion de los routers estan comentadas y CORS permite cualquier origen. Docker publica los puertos solo en `127.0.0.1`, lo cual reduce el riesgo local; no se debe abrir el puerto 8080 a Internet sin autenticacion, limites y TLS.

### Concurrencia demasiado optimista para empezar

La configuracion admite cinco tareas concurrentes y cien en cola. En un solo PC, cinco renders simultaneos con TTS, descargas, MoviePy y NVENC pueden competir por CPU, GPU, disco y memoria. Para el piloto usaria una sola tarea de render; despues mediria antes de subir a dos.

## Conclusion

El motor creativo/render esta listo para ser orquestado. No hace falta reemplazarlo ni montar n8n. La prioridad es construir una capa de operaciones confiable alrededor, y corregir/probar el tramo de publicacion antes de darle autonomia.


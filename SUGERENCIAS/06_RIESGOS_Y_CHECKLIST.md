# Riesgos y checklist de activacion

## Limites y politicas actuales

### TikTok

TikTok indica que clientes Direct Post no auditados solo publican contenido privado; tambien aplica limites diarios por creador, tipicamente alrededor de 15 segun sus pautas. Upload-Post afirma operar una integracion aprobada, pero la cuenta conectada y el plan deben probarse realmente.

Fuentes:

- <https://developers.tiktok.com/doc/content-sharing-guidelines>
- <https://docs.upload-post.com/api/upload-video/>

### YouTube

Los proyectos de API no verificados pueden tener subidas restringidas a privado hasta superar auditoria. La API permite `status.containsSyntheticMedia`. YouTube exige divulgar contenido alterado/sintetico realista; usar IA solo para idea, guion, titulo o subtitulos no siempre exige la etiqueta, pero escenas realistas inventadas si pueden exigirla.

Fuentes:

- <https://developers.google.com/youtube/v3/docs/videos/insert>
- <https://developers.google.com/youtube/v3/docs/videos>
- <https://support.google.com/youtube/answer/14328491?hl=es-US>

## Riesgos operativos

- **Duplicados:** timeout ambiguo sin idempotencia.
- **Falsa finalizacion:** render completo aunque la publicacion falle.
- **Disco lleno:** actualmente hay casi 7 GB entre cache y tareas.
- **Cuenta desconectada:** tokens OAuth revocados o expirados.
- **Proveedor agotado:** cuota de LLM, TTS o banco de videos.
- **Material pobre:** terminos correctos pero clips genericos o incongruentes.
- **Repeticion:** misma historia reescrita muchas veces.
- **Desinformacion:** relatos presentados como hechos reales.
- **Seguridad:** API local sin autenticacion si se abre a Internet.
- **Dependencia:** Upload-Post cambia precios, campos o disponibilidad.

## Checklist antes del primer piloto

- [ ] Las claves no se guardan en repositorios ni aparecen en logs.
- [ ] `upload_post_*` es leido desde la seccion correcta.
- [ ] YouTube esta conectado y probado en privado/no listado.
- [ ] TikTok esta conectado y probado en `SELF_ONLY` o cuenta de prueba.
- [ ] El caption real aparece completo en TikTok.
- [ ] Titulo, descripcion y etiquetas aparecen correctamente en YouTube.
- [ ] La declaracion IA se envia cuando corresponde.
- [ ] Cada subida usa una clave de idempotencia estable.
- [ ] Se consulta el estado hasta obtener URL o error definitivo.
- [ ] Un fallo de YouTube no vuelve a publicar en TikTok.
- [ ] Existe un boton/archivo de pausa global.
- [ ] Solo hay un render concurrente durante el piloto.
- [ ] Hay umbral de disco y limpieza automatica con politica de retencion.
- [ ] El sistema no publica si el MP4 no supera validacion tecnica.
- [ ] Los temas sensibles requieren revision.
- [ ] Hay limite diario independiente por plataforma.
- [ ] Las horas usan `America/Santiago`.
- [ ] Tres fallos consecutivos pausan la plataforma y generan alerta.
- [ ] Existe copia de la base de estado y del archivo editorial.
- [ ] La suite de pruebas sigue en verde.

## Politica de retencion sugerida

- Conservar para siempre: guion, parametros, metadatos, URL, IDs y metricas.
- Conservar 30-90 dias: videos finales publicados.
- Conservar 7 dias: renders fallidos y archivos combinados intermedios.
- Cache de stock: limite por tamaño o antiguedad, conservando un indice para evitar reutilizacion excesiva.
- Nunca borrar un archivo mientras exista una subida pendiente.

## Interruptor de emergencia

El supervisor debe comprobar una bandera de pausa antes de generar y antes de publicar. Pausar publicaciones no debe cancelar un render a medio terminar; simplemente deja el resultado en estado `LISTA` para revisarlo despues.


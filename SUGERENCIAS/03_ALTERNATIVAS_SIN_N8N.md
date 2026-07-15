# Alternativas sin n8n

## Opcion A — Supervisor local + Programador de tareas de Windows

**Mi recomendacion.**

Ventajas:

- Acceso directo a GPU, MP4 y carpetas locales.
- Muy poco mantenimiento visual.
- No hay que subir archivos grandes a un orquestador antes de publicarlos.
- Control total sobre reintentos, historial y limpieza.

Coste: requiere implementar una pequena capa propia mas adelante. Es la opcion mas simple a largo plazo, aunque no sea “cero codigo”.

## Opcion B — Windmill o Activepieces autohospedado

Sirven si quieres panel, historial y trabajos programados sin el lienzo de n8n. Se ejecutarian localmente y llamarian a la API de MoneyPrinterTurbo.

Ventajas:

- Cron, reintentos y observabilidad listos.
- Mas comodo para cambiar horarios.

Desventajas:

- Otro servicio que mantener.
- El flujo complejo sigue necesitando estado y decisiones claras.
- Hay que resolver acceso seguro a archivos locales.

## Opcion C — Make.com, Pipedream o similar

Buenos para idea, calendario, notificaciones y llamadas HTTP. Menos atractivos para el render local porque el MP4 vive en tu PC y un servicio cloud no puede verlo sin tunel, URL temporal o subida intermedia.

Los usaria solo como “cerebro remoto” y mantendria el render/publicador local. Esto agrega dependencia externa y puntos de fallo, por lo que no es mi primera opcion.

## Opcion D — Solo Upload-Post como calendario de publicacion

Upload-Post ya ofrece `scheduled_date`, cola, webhooks, estado y analiticas. El supervisor podria generar varios videos en lote y entregarlos a su cola.

Es una buena simplificacion: el PC produce; Upload-Post decide la hora exacta de salida. Aun necesitas memoria de ideas, control de calidad y reconciliacion de resultados.

Segun su sitio al 4 de julio de 2026, el plan gratis incluye 10 subidas al mes pero no TikTok. TikTok requiere plan pagado; Basic aparece desde USD 16/mes facturado anualmente, con subidas ilimitadas sujetas a limites de cada plataforma. Verificar precio antes de contratar: <https://www.upload-post.com/>.

## Opcion E — APIs oficiales directas

### YouTube

Es viable y elimina el intermediario. Requiere proyecto de Google Cloud, OAuth y manejo de subidas reanudables. Los proyectos de API no verificados pueden dejar los videos en privado hasta superar una auditoria. Documentacion: <https://developers.google.com/youtube/v3/docs/videos/insert>.

### TikTok

Es la ruta mas trabajosa. Los clientes no auditados publican en modo privado y la API exige consentimiento/flujo de usuario conforme a sus pautas. TikTok ademas aplica topes de publicacion por creador. Documentacion: <https://developers.tiktok.com/doc/content-sharing-guidelines>.

Para una sola cuenta personal, conservar Upload-Post tiene mejor relacion esfuerzo/resultado, siempre que sus pruebas reales sean buenas.

## Opcion F — Automatizacion de navegador

Playwright/Selenium simulando clics en TikTok Studio o YouTube Studio.

No la recomiendo como ruta principal: cambia la interfaz, caducan sesiones, aparecen CAPTCHA y puede incumplir condiciones o activar defensas. Solo la consideraria como respaldo manual asistido, nunca como sistema desatendido.

## Comparacion rapida

| Opcion | Comodidad | Fiabilidad local | Dependencia externa | Veredicto |
|---|---:|---:|---:|---|
| Supervisor local | Media | Alta | Baja | Mejor ajuste |
| Windmill/Activepieces | Alta | Media/alta | Baja | Buena alternativa |
| Make/Pipedream | Alta | Media | Alta | Util para cerebro, no render |
| Upload-Post queue | Alta | Alta | Alta | Excelente complemento |
| APIs oficiales | Baja | Alta al madurar | Media | Segunda etapa |
| Navegador automatizado | Media | Baja | Alta | Evitar |


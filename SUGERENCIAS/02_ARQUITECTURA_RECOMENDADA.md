# Arquitectura recomendada

## Idea central

Separar **producir** de **decidir y operar**.

MoneyPrinterTurbo conserva una responsabilidad: convertir un tema y parametros en un video. El supervisor se ocupa de todo lo que requiere memoria, calendario, criterio, recuperacion y publicacion.

## Flujo completo

1. El reloj despierta al supervisor.
2. Comprueba pausa global, limite diario, espacio en disco y que no haya otro render activo.
3. El agente de ideas propone varios temas dentro de los pilares del canal.
4. El filtro elimina duplicados, afirmaciones dudosas, temas sensibles y conceptos con poco material visual.
5. Un critico puntua gancho, claridad, potencial visual, originalidad y riesgo.
6. Se guarda la idea ganadora antes de renderizarla.
7. Se crea el trabajo mediante `POST /videos` o CLI.
8. Se consulta el estado hasta `complete` o `failed`, con timeout.
9. Se valida el MP4: existe, pesa mas que un minimo, tiene video/audio, resolucion vertical y duracion esperada.
10. Se generan metadatos independientes para TikTok y YouTube.
11. Se sube primero en modo seguro y con una clave de idempotencia.
12. Se consulta el estado de Upload-Post o se recibe su webhook hasta obtener resultado por plataforma.
13. Se guardan URL, ID, caption, hora y estado de cada publicacion.
14. Horas despues se leen metricas y se alimenta el siguiente ciclo editorial.

## Estados que debe guardar

Cada contenido debe avanzar por una maquina de estados, nunca por nombres de carpeta:

`IDEA -> APROBADA -> RENDERIZANDO -> VALIDANDO -> LISTA -> SUBIENDO -> PUBLICADA`

Rutas laterales:

- `RECHAZADA`: mala idea o riesgo editorial.
- `REINTENTAR_RENDER`: fallo temporal del motor o proveedor.
- `REINTENTAR_SUBIDA`: render bueno, publicacion fallida.
- `REVISION_HUMANA`: incoherencia, tema sensible o demasiados fallos.
- `ABANDONADA`: supera el maximo de intentos.

Este detalle evita el error clasico de volver a generar un video bueno solo porque fallo TikTok.

## Componentes

### Supervisor local

Un proceso pequeño, independiente de Streamlit. Debe poder reiniciarse sin olvidar nada. El Programador de tareas de Windows puede iniciarlo al encender el PC y comprobarlo periodicamente.

### Base SQLite

Suficiente para una cuenta y un solo equipo. Tablas conceptuales:

- `ideas`: tema, pilar, hash semantico, puntuacion y motivo de descarte.
- `jobs`: task_id, parametros, estado, intentos y rutas de salida.
- `publications`: plataforma, request_id, post_id, URL y estado.
- `metrics`: vistas, likes, comentarios y fecha de lectura.
- `events`: historial legible de cada decision/error.

### Cola unica de render

Una sola tarea al principio. La cola editorial puede contener muchas ideas, pero no debe lanzar cinco renders simultaneos por el mero hecho de que la configuracion lo permite.

### Publicador desacoplado

La publicacion no deberia ocurrir dentro del mismo bloque que renderiza. Asi se puede:

- reintentar TikTok sin regenerar;
- publicar horarios distintos por plataforma;
- usar captions distintos;
- poner YouTube privado y TikTok publico;
- detener publicaciones sin detener la produccion.

### Guardia de calidad

Dos capas:

- **Tecnica:** archivo reproducible, H.264/AAC, 9:16, audio presente, duracion razonable, sin fotogramas negros prolongados.
- **Editorial:** guion coherente, gancho temprano, sin datos inventados presentados como hechos, caption correcto y escenas suficientemente relacionadas.

La capa editorial puede comenzar con reglas y una segunda llamada a IA. El analisis visual avanzado se puede agregar despues; no debe bloquear el piloto.

### Alertas

Telegram o Discord con mensajes solo para:

- tres fallos consecutivos;
- cuenta desconectada;
- disco por debajo del umbral;
- publicacion rechazada;
- resumen diario;
- botones conceptuales `Pausar`, `Reanudar` y `No publicar este trabajo`.

## Frecuencia recomendada

- Semana 1: 2 videos diarios, subida privada/no listada.
- Semana 2: 2 videos diarios publicos si todo funciona.
- Semanas 3-4: 3 o 4 diarios, separados varias horas.
- Despues: ajustar segun metricas, no segun la capacidad maxima del PC.

El reloj puede despertar cada 30 minutos para revisar la cola, pero eso no significa publicar cada 30 minutos. Separar “frecuencia de control” de “frecuencia de publicacion” es una de las mejores decisiones de diseño aqui.


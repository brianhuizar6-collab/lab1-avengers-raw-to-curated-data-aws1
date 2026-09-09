# Resumen visual automático de reuniones de Teams

Arquitectura de referencia para un pipeline 100% local que genera un resumen del contenido visual de reuniones grabadas, sin audio, sin APIs externas y sin conexión a internet.

---

## 1. Contexto y requisitos

### Objetivo

Generar un resumen automático y cronológico de lo que se **mostró en pantalla** durante una reunión de Microsoft Teams: slides, pantalla compartida, documentos, gráficas y aplicaciones.

### Restricciones

| Restricción | Implicación de diseño |
|---|---|
| Video ya descargado, no sesión en vivo | Se puede procesar en batch, sin latencia estricta |
| Sin audio | El resumen describe lo mostrado, nunca lo dicho ni lo decidido |
| 100% local, sin nube | Todos los modelos son open-weight y corren en la misma máquina |
| ~5 reuniones al día | Volumen bajo: no requiere orquestación distribuida |
| Latencia aceptable: minutos | Permite serializar el trabajo en una sola cola |
| Minimizar cómputo | El diseño se organiza como un embudo de reducción en cascada |

### Principio rector

> El costo por imagen crece un orden de magnitud en cada etapa. Cada filtro debe ejecutarse en el punto más barato posible.

Un descarte por hash perceptual cuesta microsegundos. El mismo descarte hecho después de invocar al modelo de visión ya te costó segundos de cómputo.

---

## 2. Vista general del embudo

| Etapa | Entrada | Salida | Costo relativo |
|---|---|---|---|
| 0. Ingesta | archivo de video | job en cola | nulo |
| 1. Muestreo con ffmpeg | ~108.000 frames (60 min) | ~3.600 frames | bajo |
| 2. Recorte ROI + hash perceptual | 3.600 frames | 200–400 frames | muy bajo |
| 3. Segmentación + keyframe canónico | 200–400 frames | 40–80 imágenes | bajo |
| 4. OCR + descripción con VLM | 40–80 imágenes | JSON estructurado | **alto** |
| 5. Deduplicación semántica | JSON | JSON reducido | bajo |
| 6. Síntesis map-reduce con LLM | JSON | resumen final | medio |
| 7. Entrega y retención | resumen | Markdown / HTML | nulo |

La reducción de ~108.000 a ~50 imágenes es de tres órdenes de magnitud, y ocurre casi por completo en etapas que cuestan microsegundos por frame.

---

## 3. Etapa 1 — Extracción eficiente de frames

No decodifiques el video desde Python. Deja que ffmpeg haga el trabajo con su decodificador optimizado y escriba a disco solo lo necesario.

```bash
ffmpeg -hwaccel auto -i reunion.mp4 \
  -vf "fps=1,scale=1280:-2:flags=lanczos" \
  -q:v 3 -start_number 0 \
  frames/f_%06d.jpg
```

### Decisiones clave

- **`fps=1` dentro del filtro, no después.** ffmpeg sigue decodificando el stream completo (inevitable con codecs inter-frame como H.264), pero no paga escalado, codificación JPEG ni I/O por los 108.000 frames.
- **Escalar a 1280px de ancho.** Suficiente para OCR de slides y reduce mucho el costo posterior. Por debajo de ~1000px el texto de bullets pequeños empieza a fallar.
- **El nombre del archivo codifica el timestamp.** Con `fps=1` el índice *es* el segundo de la reunión. El eje cronológico sale gratis.
- **JPEG calidad 3, no PNG.** Con 3.600 frames por reunión, la I/O empieza a competir con el cómputo.

### Modo rápido opcional

```bash
ffmpeg -skip_frame nokey -i reunion.mp4 -vsync vfr -vf "scale=1280:-2" frames/k_%06d.jpg
```

Decodifica solo I-frames: 5–10x más rápido. El encoder inserta un I-frame en cada cambio de escena (útil), pero el espaciado en grabaciones de Teams es irregular y puede pasar 10 segundos sin insertar ninguno. Sirve como modo configurable, no como default.

---

## 4. Etapa 2 — Descarte de frames redundantes

Aquí está el 90% del ahorro. Cascada de filtros, del más barato al más caro.

### Filtro 0 — Recorte de la región de contenido (ROI)

**La optimización de mayor impacto.** Las grabaciones de Teams tienen layout predecible: la pantalla compartida ocupa la región principal, las cámaras de los participantes están en tiles laterales o inferiores.

Si no recortas, cada parpadeo, gesto o movimiento de cabeza cuenta como "cambio" y la deduplicación colapsa.

**Opción estática:** coordenadas fijas por layout. Frágil ante cambios de UI de Teams, pero trivial y exacto.

**Opción dinámica:** calcula la varianza temporal por bloque sobre 30–60 frames de muestra.

- Tiles de webcam → varianza alta y continua
- Región de slides → varianza cero durante largos periodos, con saltos abruptos

Detecta el rectángulo de baja entropía temporal y recórtalo. Más robusto y se ejecuta una sola vez por video.

### Filtro 1 — Hash perceptual

dHash de 64 bits sobre la ROI en escala de grises reducida a 9x8. Compara con el frame anterior:

- Distancia de Hamming ≤ 4 → mismo contenido, descartar

Elimina de golpe todos los frames de una slide estática, que es la mayoría absoluta. Costo: microsegundos por frame.

Librería: `imagehash` sobre Pillow, o implementación propia con NumPy (~30 líneas).

### Filtro 2 — Magnitud del cambio, no solo su existencia

El hash detecta cambios pero no distingue "apareció un cursor" de "cambió la slide".

```python
diff = cv2.absdiff(gray_prev, gray_curr)
mask = (diff > 25).astype(np.uint8)
mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5)))  # mata ruido puntual
ratio = mask.sum() / mask.size
```

| `ratio` | Interpretación |
|---|---|
| < 0.02 | Ruido: cursor, caret parpadeando, artefactos de compresión |
| 0.02 – 0.15 | Contenido incremental: build de PowerPoint, scroll de documento |
| > 0.15 | Cambio de slide o de aplicación |

Los umbrales se calibran con grabaciones reales, pero ese es el orden de magnitud.

### Filtro 3 — Estabilidad temporal

Un cambio solo cuenta si **persiste**. Exige que el contenido nuevo se mantenga estable al menos 2–3 segundos antes de aceptarlo como segmento. Elimina automáticamente:

- Transiciones y fundidos de PowerPoint (frames intermedios borrosos)
- El momento en que alguien cambia de ventana o busca un archivo
- Scroll rápido a través de un documento

### Filtro 4 — ¿Vale la pena?

Antes de gastar cómputo pesado, descarta frames sin información: pantalla en negro, vista de galería sin compartición, pantalla de "esperando a que otros se unan".

Heurística: varianza de intensidad muy baja, **o** cero regiones de texto detectadas por el detector rápido de PaddleOCR (decenas de milisegundos, mucho más barato que el reconocedor completo).

### Selección del frame canónico

De cada segmento estable no tomes el primero (puede caer en la transición) sino el más nítido:

```python
score = cv2.Laplacian(gray, cv2.CV_64F).var()
```

Elige el máximo dentro de la ventana estable.

### Salida de esta etapa

No es una lista de imágenes, es una lista de **segmentos**:

```json
{"t_inicio": 872, "t_fin": 1104, "duracion": 232, "frame": "frames/f_000913.jpg"}
```

La duración es información valiosa: una slide que estuvo 12 minutos se discutió a fondo; una que estuvo 8 segundos fue transición. Ese peso se le pasa después al LLM.

---

## 5. Etapa 4 — OCR y descripción visual

Usa **dos motores**, no uno. OCR y comprensión visual resuelven problemas distintos.

### OCR: PaddleOCR

Estándar de facto para pipelines autoalojados en producción.

- **PP-OCRv6** (junio 2026): tres tiers — tiny (1.5M), small (7.7M), medium (34.5M parámetros). El tier medium supera a VLMs de gran escala en tareas de OCR con solo 34.5M de parámetros.
- Aceleración reportada de 5.2x en CPU vía **OpenVINO**.
- **PP-StructureV3** añade análisis de layout: distingue títulos, tablas y bloques de texto, y exporta a Markdown — exactamente el formato que quieres alimentarle al LLM.

**Alternativas** si las dependencias de Paddle dan problemas (queja legítima y recurrente): **docTR** o **Surya**. En benchmarks recientes docTR alcanza precisión de carácter comparable a los mejores VLMs sobre texto impreso limpio, que es precisamente este caso de uso.

### Descripción visual: Qwen3-VL

Familia open-weight líder para esta tarea.

- **Qwen3-VL-8B**: 69.6 MMMU, 96.1 DocVQA, ~6 GB en Q4, licencia Apache 2.0.
- **Qwen3-VL-4B**: pierde ~2 puntos de MMMU y ~1 de DocVQA frente al 8B, ~3.5 GB en Q4.
- GGUF oficiales en 2B, 4B, 8B y 32B, con el modelo de lenguaje y el encoder visual (`mmproj`) como archivos separados, cuantizables por separado.
- Compatible con llama.cpp, Ollama, CPU, CUDA, Metal y SYCL.

**Servidor:** vLLM si tienes GPU y quieres batching; llama.cpp / Ollama si priorizas simplicidad operativa o corres en CPU.

### La combinación que ahorra cómputo

**No le des la imagen desnuda al VLM.** Corre OCR primero y pásale el texto extraído como contexto en el prompt. El VLM deja de gastar capacidad transcribiendo (donde además es peor que el OCR dedicado) y se enfoca en lo que solo él puede hacer: interpretar el diagrama, leer la tendencia de la gráfica, identificar qué aplicación se muestra.

### Ruteo condicional

Si el frame es una slide de puro texto con OCR de alta confianza y densidad alta, **sáltate el VLM por completo**. Esta sola regla elimina típicamente 40–60% de las llamadas al modelo pesado.

### Prompt al VLM

Pide JSON estricto:

```
Contexto OCR: {texto_ocr}

Devuelve SOLO JSON, sin markdown:
{"tipo": "slide|documento|codigo|grafica|hoja_calculo|navegador|otro",
 "titulo": "...",
 "resumen": "1-2 frases",
 "elementos_visuales": ["..."],
 "aporta_info_nueva": true|false}
```

---

## 6. Etapas 5 y 6 — Deduplicación semántica y síntesis

### Merge semántico

Las builds incrementales de PowerPoint generan 4 segmentos casi idénticos de la misma slide.

Compara el texto OCR consecutivo con similitud de tokens (Jaccard o `rapidfuzz`). Si supera ~0.85, fusiona los segmentos y quédate con la variante más completa — normalmente la última, que tiene más texto.

Recorta típicamente otro 20–30%.

### Map-reduce con LLM

| Nivel | Qué hace |
|---|---|
| **Map** | Cada segmento ya tiene su nota estructurada de la etapa 4. No requiere LLM adicional. |
| **Reduce 1** | Agrupa segmentos en bloques temáticos (ventanas de 10 min, o rupturas por baja similitud entre segmentos consecutivos) y genera un párrafo por bloque. |
| **Reduce 2** | Síntesis global a partir de los párrafos, no de los frames. |

**Por qué importa:** 60 segmentos con OCR completo pueden ser 40k tokens. Un modelo local de 8B con contexto largo degrada mucho antes de llenar su ventana nominal. Reducir en dos niveles mantiene cada llamada por debajo de ~8k tokens, donde la calidad es estable.

### Formato de salida

1. **Resumen ejecutivo** (5–8 líneas)
2. **Línea de tiempo** con `[00:14:32] Título de la slide` y miniatura enlazada
3. **Documentos y aplicaciones mostrados** durante la reunión
4. **Datos numéricos y tablas** extraídos textualmente por el OCR

### Regla dura del prompt

> Nada de inferir intención ni conclusiones.

Sin audio no sabes qué se dijo sobre la slide. El resumen describe lo que se mostró, no lo que se decidió. Instruye explícitamente al modelo a no inventar contexto conversacional, y ancla cada afirmación a un timestamp.

---

## 7. Arquitectura de producción

### Orquestación

Con 5 reuniones al día no necesitas Kubernetes ni Airflow.

- **Watcher** sobre un directorio de entrada (`watchdog`, o cron cada 5 minutos)
- **Cola persistente**: SQLite con tabla de jobs y estados es suficiente y no añade un servicio más que mantener. Redis + RQ si prefieres herramientas estándar.
- **Un solo worker con acceso al modelo, en serie.** Dos jobs concurrentes compitiendo por memoria es la causa número uno de OOM en este tipo de sistemas. Las etapas 1 y 2 (CPU puro) sí pueden paralelizarse.
- **Modelos residentes.** Arranca vLLM / llama-server / Ollama como servicio persistente y evita 30–60 segundos de carga por reunión.

### Checkpointing por etapa

Cada etapa escribe su salida a disco y marca su estado en la base. Si el VLM falla en el frame 37, reanudas desde ahí, no desde el principio. Con jobs de varios minutos, esto se paga solo la primera vez que algo se cae.

### Idempotencia

La clave del job es el hash del archivo de video. Reprocesar el mismo video no duplica trabajo ni resultados.

### Estado en disco

```
/data/{meeting_id}/
  video.mp4
  frames/          # temporal, se borra al terminar la etapa 2
  keyframes/       # se conservan, van en el reporte
  extractions.json
  summary.md
```

Una hora de video a 1 fps son ~2–4 GB de JPEGs temporales. Sin política de limpieza llenas el disco en semanas. Purga `frames/` al cerrar la etapa 2 y aplica retención (30–90 días) sobre el resto.

### Observabilidad mínima

Registra por reunión:

- Frames muestreados
- Sobrevivientes de cada filtro
- Keyframes finales
- Tiempo por etapa

Si la tasa de reducción se dispara o se desploma (por ejemplo, Teams cambió el layout y tu ROI quedó mal), lo ves en esa métrica antes de que alguien se queje del resumen.

---

## 8. Perfil A — Con GPU

**Hardware:** GPU de 16–24 GB (RTX 4090, 3090, A4000). Sobra.

| Etapa | Tiempo (60 min de video) |
|---|---|
| Extracción ffmpeg | 30–90 s |
| Filtros CPU (3.600 frames) | 20–40 s |
| OCR (~50 imágenes) | 10–20 s |
| VLM (~25–50 imágenes) | 60–180 s |
| Síntesis LLM | 30–60 s |
| **Total** | **~3–6 min** |

Cinco reuniones al día son menos de 40 minutos de cómputo total. Cabe con enorme holgura en una sola máquina.

---

## 9. Perfil B — CPU sola, 16 GB de RAM

Sí funciona. El pipeline ya era casi todo CPU: las etapas 1 y 2 nunca usaron GPU. Lo que cambia es la etapa de modelos.

### Presupuesto de RAM

| Componente | RAM |
|---|---|
| SO + Python + OpenCV + ffmpeg | 2.5–3 GB |
| OCR (PP-OCRv6 small en ONNX/OpenVINO) | 0.5–1 GB |
| VLM Qwen3-VL-4B Q4_K_M + mmproj | 3.5–4.5 GB |
| KV cache y tokens de imagen | 1–1.5 GB |
| Buffers de frames en vuelo | 1 GB |
| **Total** | **~9–11 GB** |

Quedan ~5 GB de holgura, que es lo correcto: en cuanto tocas swap con un modelo cuantizado, la velocidad se cae por un acantilado y el job pasa de 20 minutos a dos horas.

**La restricción dominante no es el cómputo, es que no puedes tener dos modelos cargados a la vez.**

### Un solo modelo para las dos tareas

No necesitas un VLM para describir imágenes *y* un LLM aparte para resumir. Qwen3-VL es un modelo de lenguaje con un encoder visual encima: si le quitas la imagen, funciona como LLM normal.

- **Etapa 4** (describir keyframes): mismo modelo, con imagen
- **Etapa 6** (síntesis): mismo modelo, solo texto

Un `llama-server` cargado una vez, residente todo el día, sirviendo ambos tipos de petición por el endpoint OpenAI-compatible. Cero recargas, cero picos de RAM.

```bash
llama-server \
  -hf Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M \
  -c 8192 -t 8 --mlock --jinja --port 8080
```

- `-t` al número de **núcleos físicos**, no lógicos. Con hyperthreading activo suele ir más lento.
- `--mlock` evita que el SO pagine los pesos.
- Deja el `mmproj` en **Q8_0** y el LLM en **Q4_K_M**: el encoder visual es pequeño y su calidad de lectura de texto sí se degrada al cuantizarlo agresivamente.

**Recomendación de tamaño:** el **4B**. El 8B entra en Q4 (~6 GB) pero deja poco margen y es casi el doble de lento en CPU sin ganar tanto. Si la CPU es débil (4 núcleos, portátil), baja al 2B y compénsalo apoyándote más en el OCR.

### Tiempos reales — CPU de 8 núcleos moderna

| Etapa | Con GPU | CPU sola |
|---|---|---|
| ffmpeg a 1 fps | 30–90 s | 4–8 min |
| Filtros (pHash, diff, ROI) | 20–40 s | 40–90 s |
| OCR (~40 imágenes) | 10–20 s | 40–90 s |
| VLM (**por imagen**) | 2–4 s | **40–90 s** |
| Síntesis final | 30–60 s | 3–5 min |

El VLM es el problema, y es un problema de **prefill**: una imagen de 1280px se convierte en 1.000–2.000 tokens visuales, y ese prefill en CPU va a decenas de tokens por segundo. Con 50 keyframes son 40–60 minutos solo en esa etapa.

### Los tres ajustes que lo hacen viable

**1. Baja la resolución que ve el VLM.** El ajuste de mayor impacto. El OCR necesita 1280px; el VLM no — solo tiene que decir "es una gráfica de barras de ingresos trimestrales con tendencia ascendente". Redimensiona a **768px de lado mayor** antes de la llamada. Los tokens visuales caen a la mitad o menos y el tiempo por imagen baja a 20–35 s. Conserva los JPEG de 1280px para el reporte y el OCR; el downscale es solo para el modelo.

**2. Ruteo OCR-primero mucho más agresivo.** En el perfil con GPU era una optimización; aquí es obligatorio.

> Si el OCR devuelve texto denso con alta confianza, el VLM ni se entera de esa imagen.

Le pasas el Markdown de `PP-StructureV3` directo al resumen final. Solo van al VLM los frames donde el OCR devuelve poco texto **pero** la imagen tiene alta entropía visual — la firma exacta de un diagrama, una gráfica o un screenshot de aplicación. En reuniones normales de slides esto deja **5 a 15 llamadas al VLM por reunión**, no 50.

**3. Mueve la deduplicación semántica *antes* del VLM.** En CPU no puedes permitirte procesar cuatro variantes de la misma slide para luego tirar tres. Corre OCR sobre todos los keyframes (es barato), fusiona con `rapidfuzz` lo que supere 0.85 de similitud, y recién entonces decide qué manda al VLM.

Añade un **tope duro de 20 llamadas al VLM por reunión**, priorizando los segmentos de mayor duración. Si una imagen no estuvo ni 30 segundos en pantalla, casi nunca vale su costo.

**Resultado:** 12–20 minutos por reunión. Cinco reuniones al día son 1–1.5 horas de CPU en cola serializada. No es "unos minutos después de que termine la reunión", pero sí es "el resumen está listo antes de que alguien lo abra".

### Ajustes operativos adicionales para CPU

- **`nice` / `ionice` el worker.** Si la máquina hace algo más, un pipeline de OCR a plena carga la vuelve inusable.
- **Contexto limitado a 8192.** El KV cache en CPU cuesta RAM y el prefill largo es lento. El map-reduce en dos niveles pasa de recomendable a obligatorio: cada llamada por debajo de ~6k tokens.
- **OpenVINO como backend del OCR.** La aceleración de 5.2x reportada para PP-OCRv6 en CPU se siente mucho aquí.
- **Purga agresiva.** Borra `frames/` en cuanto termina la etapa 2, no al final del job.

### Nota sobre hardware

Si en algún momento puedes meter una GPU modesta, es la mejor relación costo-beneficio de todo el sistema. Una RTX 3060 de 12 GB usada ronda los 250 USD y convierte la etapa del VLM de 15 minutos a menos de 1. Nada de la arquitectura cambia: mismo modelo, mismo `llama-server`, solo añades `-ngl 99`.

Si la máquina es un **Mac con Apple Silicon**, olvida lo anterior sobre lentitud: la memoria unificada hace que un M-series con 16 GB corra el 8B a velocidad de GPU discreta, porque llama.cpp usa Metal.

---

## 10. Por qué el embudo no pierde contexto

La reducción funciona porque el video de una reunión es **redundante por naturaleza, no denso**. Una slide típica permanece 2–5 minutos en pantalla: son 120–300 frames idénticos que aportan exactamente la misma información que uno solo. Lo que descartas no es información, es repetición.

### Los tres tipos de pérdida posible

| Riesgo | Cobertura |
|---|---|
| **Contenido de menos de 1 segundo** (tooltip, flash) | Se pierde con el muestreo a 1 fps. Aceptable: si duró menos de un segundo, nadie en la reunión lo leyó tampoco. |
| **Cambios sutiles pero significativos** (una celda que cambia en un Excel) | Umbral adaptativo por tipo de contenido: si el segmento anterior fue clasificado como `hoja_calculo` o `codigo`, baja el umbral de área a 0.005 para ese segmento. |
| **Contenido revelado progresivamente** (builds de PowerPoint) | El filtro de área lo captura como cambio incremental; el merge semántico se queda con la versión final más completa, que contiene todo. |

### El orden es lo que abarata

Los filtros que descartan más volumen (el hash perceptual elimina ~95% de los frames) son también los más baratos. Los modelos que cuestan segundos por imagen solo ven las 40–80 imágenes que ya pasaron por todo lo demás.

---

## 11. Resumen del stack

| Función | Herramienta | Notas |
|---|---|---|
| Decodificación y muestreo | **ffmpeg** | `fps=1` + `scale` en el filtro |
| Visión clásica y filtros | **OpenCV + NumPy** | absdiff, morfología, Laplaciano |
| Hash perceptual | **imagehash** (o NumPy propio) | dHash 64 bits |
| OCR | **PaddleOCR PP-OCRv6** + PP-StructureV3 | backend OpenVINO en CPU |
| Alternativas de OCR | **docTR**, **Surya** | si Paddle da problemas de dependencias |
| Descripción visual | **Qwen3-VL** 4B / 8B | GGUF oficiales, mmproj separado |
| Servidor de inferencia | **llama.cpp** (CPU) / **vLLM** (GPU) | endpoint OpenAI-compatible |
| Síntesis final | **el mismo Qwen3-VL**, sin imagen | evita cargar un segundo modelo |
| Similitud de texto | **rapidfuzz** | merge de builds incrementales |
| Cola y estado | **SQLite** (o Redis + RQ) | un worker serializado |
| Watcher | **watchdog** o cron | ingesta del directorio de entrada |

---

## 12. Orden de implementación sugerido

1. **Etapas 1 y 2** — ffmpeg + filtros. Definen la calidad de todo lo que viene después. Mide la tasa de reducción sobre 3–5 grabaciones reales antes de seguir.
2. **Calibración de ROI y umbrales** con esas mismas grabaciones. No uses los valores del documento a ciegas.
3. **OCR** con PP-StructureV3 y salida a Markdown. En este punto ya tienes un resumen útil de texto, sin ningún modelo generativo.
4. **Ruteo OCR-vs-VLM** y llamadas al modelo de visión.
5. **Síntesis map-reduce** y formato de salida.
6. **Orquestación, checkpointing y retención.** Al final, cuando el pipeline ya funciona a mano.

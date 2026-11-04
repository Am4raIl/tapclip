# replaycam

Mantem os **ultimos 30 segundos de video da camera apenas na RAM** e exporta
para `.mp4` no instante em que um gatilho dispara (Enter no teclado, GPIO de
um Raspberry Pi, mensagem MQTT de um ESP32 ou um POST HTTP).

Nada e escrito em disco enquanto o replay nao e solicitado.

---

## Como funciona

```
 [trigger]   Enter / GPIO / MQTT / HTTP
                  |
                  v
          queue.Queue(maxsize=1)      <- drop-oldest, nunca acumula
                  |
 [capture]   cap.read() -> resize -> ring.push()        NUNCA toca em disco
                  |                     |
                  |              RingBuffer (deque + 1 lock)
                  |                     |
                  |              list(dq)   ~0,01 ms p/ 900 frames
                  |                     |
 [writer]  <-- dequeue -- VideoWriter.write() ... release()   UNICA thread em disco
```

Tres decisoes que sustentam o resto:

**1. A janela e por TEMPO, nao por contagem de frames.** A receita obvia
(`deque(maxlen=fps * seconds)`) so funciona se a camera entregar exatamente o
FPS pedido. Cameras mentem: voce pede 30 fps, ela entrega 15, e o buffer passa a
segurar **60 segundos** de tempo corrido — exportando a 30 fps o replay sai em
camera lenta. Aqui `maxlen` e so um teto e a janela e mantida por
`perf_counter()`, entao sempre valem N segundos REAIS, mesmo com drop de frames.

**2. Uma unica thread escreve em disco.** `cv2.VideoWriter` nao e thread-safe.
Concentrar toda escrita em uma thread elimina a classe inteira de bugs de
corrida. A fila e limitada a 1 com drop-oldest: apertar Enter cinco vezes
durante um save nao cria cinco threads, e o log diz qual pedido foi
substituido.

**3. O snapshot e barato.** `list(deque)` copia so ponteiros, nao pixels:
**0,010 ms para 900 frames** (medido). Por isso a thread de captura quase nunca
espera o lock, e a gravacao em disco nao a atrasa de forma mensuravel — ver a
tabela de medicoes.

## Requisitos

- Python 3.10+ (validado em **3.14.3**)
- `opencv-python==4.14.0.94` (fixado: wheel `cp37-abi3`, instala limpo no 3.14;
  o 5.x do PyPI tem quebras de API em `VideoWriter`)
- **Nao precisa de FFmpeg no PATH.**

```bash
pip install -r requirements.txt
```

## Uso

No Windows, use o launcher. Ele corta o ruido vermelho do OpenH264 (8 linhas
para 2) e repassa todos os argumentos:

```bat
replaycam.cmd --list-cameras     :: sonda as cameras (so leitura)
replaycam.cmd --self-test        :: valida ring + writer SEM camera
replaycam.cmd                    :: 30 s em RAM, janela aberta, aperte Enter
replaycam.cmd --no-preview       :: sem janela (so console)
```

O mesmo funciona em PowerShell (`.\replaycam.cmd --self-test`) e em bash, se voce
tiver o `cmd` no PATH. Sem o launcher, `python -m replaycam ...` funciona igual,
so que com o log nativo do OpenCV aparecendo.

A janela abre sozinha, mostrando a **camera em resolucao plena** (nao o
conteudo reduzido do buffer), com o status sobreposto: resolucao, FPS medido,
frames capturados, RAM em uso e quanto da janela retroativa ja esta coberto.

- **Enter** salva o replay. **ESC** sai.
- **Arrastar o slider** muda o tempo retroativo ao vivo, sem reiniciar.

O arquivo sai como `replays/replay_AAAAMMDD_HHMMSS.mp4`. Nome repetido no
mesmo segundo ganha sufixo `_1`, `_2` — um replay nunca e sobrescrito.

### Opcoes principais

| Opcao | Default | Para que serve |
|---|---|---|
| `--device` / `-d` | `0` | indice da camera |
| `--backend` | `auto` | `dshow` abre mais rapido no Windows; `msmf` e o default do SO |
| `--width` `--height` | `0` `0` (nativo) | o que voce **pede** ao driver. `0` = modo nativo |
| `--fps` | `30` | FPS de captura pedido |
| `--buf-width` `--buf-height` | `640` `480` | o que fica **na RAM** (alavanca de memoria) |
| `--seconds` | `30` | janela de tempo (ajustavel tambem pelo slider) |
| `--export-fps` | igual a `--fps` | FPS do arquivo gravado |
| `--outdir` | `replays` | destino |
| `--fourcc` | automatico | forca um codec (ex.: `mp4v`) |
| `--trigger` | `keyboard` | gatilhos ativos, separados por virgula |
| `--auto-trigger` | `0` (off) | dispara sozinho apos N s, para testar sem a mao na tecla (veja a nota abaixo) |
| `--no-preview` | preview ligada | fecha a janela de video |
| `--max-buffer-mib` | `1024` | teto de RAM do slider de tempo retroativo |
| `--verbose` | off | mostra o log nativo do OpenCV/FFmpeg |

`--width/--height` e `--buf-width/--buf-height` sao separados de proposito: voce
captura em 1080p e guarda 640x480 no buffer, gastando ~790 MiB em vez de ~2,5 GB.

## Medicoes reais

Camera integrada do PC, Windows 11, Python 3.14, `opencv-python 4.14.0.94`,
com `--width 1280 --height 720` (o driver aceitou) -> buffer 640x480:

| Metrica | Valor medido |
|---|---|
| FPS efetivamente capturado | **29,99 – 30,03** (pedido 30) |
| Frames perdidos durante um save de 953 ms | **0** |
| `list(deque)` com 900 frames | **0,010 ms** |
| RAM do buffer (880 frames = 29,3 s) | **773,4 MiB** |
| RSS de pico do processo | **908 MiB** |
| Tempo para gravar 29,3 s de video | **953 ms** (3,3% da duracao) |
| Arquivo gerado | 29,33 s, 880 frames, 640x480, H.264 real, 32,5 MiB |
| `read()` falhados em 31 s | **0** |
| Frames evictados apos encher | 9 (buffer circular rodando) |
| Warm-up ate o 1o frame | 0,24 – 1,8 s |

30 s cheios sao 900 frames = **791 MiB**; o 773,4 MiB acima e o mesmo buffer um
pouco antes de encher.

Modo nativo (`--width 0 --height 0`, o default) e a janela com slider:

| Metrica | Valor medido |
|---|---|
| Camera encontrada por `--list-cameras` | indice `0`, `real=640x480 @ 30,0 fps` |
| Custo de `--list-cameras` (2 cameras testadas) | **2,04 s** |
| Save pela janela (Enter na preview) | H.264 real, 2,7 s, 2,4 MiB |
| Estabilidade em ~10 s com save no meio | `falhas_de_read=0`, FPS ~30 |
| Frames evictados apos encher | 754 (o circular realmente esta rodando) |
| Ruido do OpenH264 por execucao | 8 linhas direto, **2 linhas** via `replaycam.cmd` |

> **Aviso esperado no stderr.** O `opencv-python` nao embarca a DLL do OpenH264,
> entao o backend FFMPEG loga `OpenH264 / Unable to create encoder` e falha. O
> OpenCV cai no **Media Foundation**, que encode H.264 de verdade no Windows
> 10+. As mensagens parecem erro, mas sao inofensivas — o log `codec real=h264`
> confirma o que foi gravado.
>
> Isso **nao** se resolve com `cv2.utils.logging.setLogLevel`: o texto vem do
> `av_log` do FFmpeg em C, direto no stderr. O que funciona e o
> `replaycam.cmd`, que define `OPENCV_FFMPEG_LOGLEVEL=QUIET` e
> `OPENCV_LOG_LEVEL=SILENT` no ambiente **antes** do processo nascer. As 2 linhas
> que sobram sao do carregador da libopenh264 e nao tem como tira-las sem baixar
> a DLL.

---

# 1. Como ajustar FPS e resolucao para garantir estabilidade

### A conta que decide tudo

```
RAM do buffer = segundos x fps x largura x altura x 3 bytes
```

Com 3 canais BGR, para 30 s:

| Buffer | Frames | RAM |
|---|---|---|
| 1920x1080 | 900 | **5,2 GiB** — inviavel |
| 1280x720 | 900 | **2,5 GB** — comeca a paginar |
| 640x480 | 900 | **829 MB** (791 MiB) — o padrao deste projeto |
| 480x360 | 900 | 466 MB |
| 320x240 | 900 | 207 MB |
| 640x480 em JPEG q85 | 900 | ~36 MB (porem: +2–4 ms de CPU/frame e dupla compressao) |

**Regra pratica: baixe `--buf-width/--buf-height` antes de baixar `--fps`.**
Metade dos pixels custa muito menos qualidade por MB do que metade da taxa de
frames. O codigo avisa sozinho se o total passar de 1,5 GiB.

### Resolucao e tempo retroativo sao o mesmo recurso

Como a conta acima e multiplicacao, resolucao e tempo competem diretamente. O
mesmo orcamento de RAM compra tempos muito diferentes:

| Buffer | 5 s | 10 s | 30 s |
|---|---|---|---|
| 640x480 (0,88 MiB/frame) | 132 MiB | 264 MiB | 791 MiB |
| 1280x720 (2,64 MiB/frame) | 396 MiB | 791 MiB | 2,4 GiB |
| 1920x1080 (5,93 MiB/frame) | 890 MiB | 1,8 GiB | 5,2 GiB |

O slider da janela respeita `--max-buffer-mib` (default `1024`), entao o teto
**calculado** aparece na propria interface: com buffer 640x480 ele vai a ~38 s;
com 1920x1080, a ~5 s. E por isso que o limite e explicito em vez de um crash:
a restricao fica visivel antes de voce escolher.

### 1080p: as duas alavancas sao independentes

`--width/--height` e o que a **camera** entrega; `--buf-width/--buf-height` e o
que fica **na RAM**. A janela mostra o primeiro; o replay sai no segundo. Para
ver a camera em 1080p e ainda ter 30 s de replay, capture em 1080p e grave em
640x480:

```bat
replaycam.cmd --width 1920 --height 1080 --buf-width 640 --buf-height 480
```

Se quiser o replay tambem em 1080p, o tempo retroativo cai para poucos segundos
— e o slider mostra isso sozinho.

Dois avisos honestos sobre 1080p nesta maquina:

- O default e `--width 0 --height 0` (modo nativo), e aqui o driver escolhe
  **640x480** — nao a maior resolucao disponivel. Modo nativo nao e modo maximo.
- Pedir `1920x1080` explicitamente fez o driver devolver `1280x720` nesta
  camera. Antes de investir em 1080p, rode `--list-cameras` e confira se a
  camera realmente anuncia esse modo; o limite pode estar no driver do Windows,
  e nao no OpenCV.

### FPS: a camera vai mentir, e o codigo avisa

O defeito silencioso mais perigoso deste sistema e a camera entregar um FPS
diferente do pedido. Por isso a captura **mede** o FPS real pelos timestamps e
compara com o pedido a cada 2 s, avisando se divergir mais de 10%:

```
AVISO: FPS efetivo 26,9 difere do pedido 30,0 (0,90x). Exportar a 30 fps
       deixa o replay em camera lenta. Ajuste --export-fps para 26,9 ...
```

Dois detalhes que custa uma medicao para acertar:

- **A janela de medicao so comeca no primeiro frame.** Incluir o warm-up do
  driver (medido em ate 1,8 s aqui) dava 26,9 fps quando a taxa real era 29,8 —
  um aviso falso que mandaria mexer num `--export-fps` que estava certo.
  **Aviso falso e pior que nenhum aviso.**
- **Confie no `measured_fps`, nunca em `CAP_PROP_FPS`.** No MSMF essa propriedade
  costuma vir 0. A mesma desconfianca vale para largura/altura: a resolucao real
  vem do `frame.shape`, e o codigo avisa quando o driver rebaixa o modo
  (pediu 1920x1080, recebeu 1280x720 — aconteceu nesta maquina).

**`--export-fps` deve ser igual ao `--fps`.** Divergir produz slow-motion
(exportar menos fps do que o de captura) ou fast-forward. O codigo avisa se voce deixar passar.

### Ordem de configuracao que importa

1. **Peça FPS antes da resolucao.** Em DSHOW, pedir a resolucao primeiro faz o
   driver refazer o modo do dispositivo e as vezes voltar com outro FPS. E o
   codigo ja faz nessa ordem (`capture.py:open_camera`).
2. **`CAP_PROP_BUFFERSIZE = 1`** corta a latencia interna do driver, entregando
   o frame mais recente em vez de um atrasado. DSHOW respeita; MSMF ignora.
3. **`cv2.setNumThreads(1)`** — OpenCV com varias threads internas disputa CPU com
   o seu loop e aumenta a jorca (frame time) da captura.
4. **Tente `--backend dshow`**: abre mais rapido que MSMF e costuma evitar varias
   das’ quirks de formato. Compare com `--list-cameras`.
5. **`INTER_AREA` no downscale** — o filtro correto para minificacao (faz media
   ponderada, evita serrilhado). Usar `INTER_LINEAR` aqui produz aliasing
   visivel.

### Se o computador nao acompanhar

O log `[status]` mostra `medido=` o tempo todo. Se ele ficar abaixo do pedido,
a CPU esta virando o gargalo. Nesta ordem: reduza `--buf-width/--buf-height`,
desligue `--no-preview`, reduza `--seconds`. Baixar `--fps` e o ultimo recurso,
porque custa movimento.

---

# 2. Como trocar o Enter por um botao fisico, GPIO ou ESP32

**Nao e preciso mexer em mais nada.** O ABC `Trigger` e a costura do sistema, e
a regra e uma so:

> um gatilho chama `on_fire(source)` e nao faz mais nada.

Quem decide o que fazer com o replay e a `cli`, que enfileira o pedido na fila
da thread de escrita. Como o caminho de gravacao e identico para todos, da para
combinar varios gatilhos ao mesmo tempo: `--trigger keyboard,gpio`.

Para adicionar o seu, implemente `_read_loop` e chame `self.fire()`:

```python
class MeuTrigger(Trigger):
    name = "meu-gatilho"

    def _read_loop(self) -> None:          # bloqueia ate stop_event
        while not self.stop_event.is_set():
            if botao_foi_pressionado():
                self.fire()
            time.sleep(0.02)
```

E registre em `build_triggers` (`cli.py`). referencias prontas para copiar estao
comentadas no fim de `replaycam/triggers.py`. Nao vao no codigo de proposito:
nao da para testar GPIO de Raspberry Pi nem MQTT de ESP32 nesta maquina, e
codigo de hardware nao testado e pior do que codigo ausente.

### Raspberry Pi — botao em GPIO

```python
class GpioButtonTrigger(Trigger):
    name = "gpio"

    def __init__(self, on_fire, pin=17, pull_up=True, **kw):
        super().__init__(on_fire, **kw)
        self.pin, self.pull_up = pin, pull_up

    def _read_loop(self) -> None:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.IN,
                   pull_up_down=GPIO.PUD_UP if self.pull_up else GPIO.PUD_DOWN)
        try:
            while not self.stop_event.is_set():
                if GPIO.input(self.pin) == (not self.pull_up):
                    self.fire()                        # o debounce segura o chaco
                self.stop_event.wait(0.005)            # 5 ms: latencia boa, CPU baixa
        finally:
            GPIO.cleanup()
```

Instalar: `pip install RPi.GPIO`. Prefira este polling a `add_event_detect` — a
callback do RPi.GPIO roda na mesma GIL e pode atrasar a captura.

**O debounce (0,35 s por padrao) mora no gatilho, nunca no writer.** Botao
mecanico quica e pode gerar varias bordas em milissegundos; filtrar la em
dentro da thread de escrita desperdicaria o isolamento de threads.

### ESP32 — botao pela rede, via MQTT

```python
class MqttTrigger(Trigger):
    name = "mqtt"

    def __init__(self, on_fire, host="localhost", port=1883, topic="cam/trigger", **kw):
        super().__init__(on_fire, **kw)
        self.host, self.port, self.topic = host, port, topic

    def _read_loop(self) -> None:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        def on_connect(cl, _u, _f, reason_code, _p=None):
            if reason_code == 0:
                cl.subscribe(self.topic)

        def on_message(_cl, _u, msg):
            if msg.payload.decode(errors="ignore").strip() in ("1", "on", "press"):
                self.fire()

        client.on_connect, client.on_message = on_connect, on_message
        client.connect(self.host, self.port, keepalive=30)
        client.loop_start()                    # nao bloqueia esta thread
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(0.5)
        finally:
            client.loop_stop()
            client.disconnect()
```

Instalar: `pip install paho-mqtt` (ou `pip install -e ".[mqtt]"`). No ESP32:

```cpp
if (digitalRead(BOTAO_PIN) == LOW) client.publish("cam/trigger", "1");
```

`fire()` so enfileira — a gravacao acontece na thread de escrita, entao uma
mensagem MQTT lenta nunca segura a captura. Use `retain=false` para nao
disparar um replay antigo quando o broker reiniciar.

### HTTP — um POST de qualquer lugar

```python
class HttpTrigger(Trigger):
    name = "http"

    def __init__(self, on_fire, host="0.0.0.0", port=8080, path="/trigger", **kw):
        super().__init__(on_fire, **kw)
        self.address, self.port, self.path = host, port, path

    def _read_loop(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        trigger = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                        # noqa: N802 (nome imposto)
                if self.path == trigger.path:
                    trigger.fire()
                    self.send_response(202)
                else:
                    self.send_response(404)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        ThreadingHTTPServer((self.address, self.port), Handler).serve_forever()
```

Testar: `curl -X POST http://localhost:8080/trigger`. `ThreadingHTTPServer` da
cada request em uma thread, para um POST lento nao travar os outros.

**Seguranca:** em LAN confiavel e aceitavel. Exposto na internet, exija um
header de token — qualquer um com a URL poderia disparar gravacoes e encher seu
disco.

---

## Limitacoes conhecidas

- **O codec FourCC e um pedido, nao uma garantia.** Medido aqui: `avc1`, `H264`,
  `X264` e ate um `ZZZZ` sem sentido produzem um arquivo **byte a byte
  identico** (25.825 B, lido de volta como `h264`). O FFMPEG loga "tag not
  found" e cai no codec padrao; so que o MSMF assume e encode H.264 de verdade.
  Nao existe forma de detectar pela saida que voce pediu uma tag invalida. Por
  isso a validacao confere o que importa — **o arquivo decodifica, tem a
  contagem de frames certa e a duracao certa** — e o log reporta o codec real.
- **Sem controle de bitrate.** `cv2.VideoWriter` nao expoe bitrate/qualidade, e
  o Media Foundation usa um preset de qualidade alta: os 29 s a 640x480
  ocuparam 32,5 MiB (~9 Mbit/s), bem acima do necessario. Para controlar
  bitrate de verdade, grave com FFmpeg (`imageio-ffmpeg` traz um binario
  estatico) em vez de `cv2.VideoWriter`.
- **A tecla so e lida com o console ou com a janela em foco.** `msvcrt`/`termios`
  leem o console do processo e o `cv2.waitKey` le a janela do OpenCV; os dois
  chamam o mesmo gatilho. Se voce clicar numa terceira janela, o Enter deixa de
  chegar. Para hotkey global, instale `pynput` (`pip install -e ".[hotkey]"`) e
  use um `pynput.Listener` no lugar de `KeyboardEnterTrigger`.
- **Duas linhas vermelhas do OpenH264 nao tem jeito.** O `opencv-python` nao
  embarca a DLL do OpenH264, entao o FFMPEG tenta H.264, falha e o Media
  Foundation assume. O `replaycam.cmd` reduz o ruido de 8 linhas para 2
  (`OPENCV_FFMPEG_LOGLEVEL=QUIET` + `OPENCV_LOG_LEVEL=SILENT`); as 2 restantes
  vem do carregador da libopenh264, que escreve no stderr antes de qualquer
  nivel de log valer. So somem baixando a DLL. Detalhe importante: essas
  variaveis **so funcionam se estiverem no ambiente quando o processo nasce** —
  definir `os.environ` de dentro do Python nao tem efeito, porque a biblioteca
  nativa le o ambiente no carregamento. E por isso que o silenciamento mora num
  `.cmd` e nao no codigo.
- **Sem audio.** O `cv2.VideoCapture` em modo So-Imagem nao captura som.
- **O replay sai na resolucao do buffer, nao da captura.** Nao ha upscale para a
  resolucao nativa na exportacao (e o codigo avisa se voce pedir isso). A
  janela de preview, essa sim, mostra a captura em resolucao plena.
- **O slider so responde a arrasto de mouse.** O trackbar do OpenCV nao devolve
  as setas do teclado para o `waitKey`; arraste com o botao.
- **Um `.mp4` por disparo.** Nao ha rotacao/limpeza automatica do `--outdir`.

## Testando manualmente

Do mais seguro ao mais realista. Rode sempre do diretorio do projeto. Os
comandos de `replaycam.cmd` valem `python -m replaycam` sem o launcher — a
diferenca e so o ruido do OpenH264.

**1. Sem câmera e sem disco — valida ring buffer, concorrência e writer**

```bash
python -m replaycam --self-test
```

Imprime `janela temporal`, `RAM contabilizada`, `concorrencia`, o codec gravado e
a duracao lida de volta do `.mp4`. Sai com codigo 0 se tudo passou, 1 se nao.
Nao deixa arquivo para tras.

**2. Descobrir o indice da camera**

```bash
python -m replaycam --list-cameras
```

Se voltar vazio: **Settings > Privacy & security > Camera > Camera access = ON**.

**3. Ver o caminho de gravacao completo, sem apertar nada**

```bash
python -m replaycam --seconds 5 --auto-trigger 7
```

Dispara sozinho em 7 s e grava de verdade. Bom para tirar a duvida "o Enter esta
funcionando?" antes de se preocupar com o teclado.

> Curto de proposito: `--auto-trigger 7` com `--seconds 5` grava os ~4,3 s que
> estavam no buffer no instante do disparo. Isso e a semantica correta do ring
> buffer (o replay e o que estiver em memoria agora, ate N segundos). Para ver os
> 5 s completos, use `--auto-trigger 10`.
>
> E note que `--auto-trigger` **dispara o save, mas nao encerra o programa** —
> ele e um gatilho, nao um "rode por N segundos". Depois do arquivo aparecer o
> app continua aberto, esperando Enter, porque foi pedido que ele gravasse, nao
> que saisse. Para um teste que termina sozinho, envie Ctrl+Break (ou ESC, se a
> preview estiver ligada) depois que o `SALVO` aparecer.

**4. A janela de video (o modo normal, ja ligado por default)**

```bat
replaycam.cmd
```

A janela abre sozinha. Arraste o **slider** para mudar o tempo retroativo (o teto
aparece no proprio rotulo, calculado pelo seu orcamento de RAM), e aperte
**Enter** na janela. **ESC** fecha. Se preferir o console, clique nele e aperte
Enter ali tambem — os dois caminhos chamam o mesmo `on_fire`.

**5. Encarando a RAM de verdade (30 s a 640x480 = 791 MiB)**

Espere aparecer `[pronto] buffer cheio`, aperte Enter e confira o
`SALVO via keyboard: ...`. Com 30 s de espera, use `--auto-trigger 35` se nao
quiser ficar olhando — e lembre que ele so dispara o save: o app continua
aberto, e voce encerra com Ctrl+Break (ou ESC na preview).

Medido nesta maquina nesse regime exato: 900 frames lidos de verdade, 30,00 s,
640x480, 32,2 MiB, `falhas_de_read=0`, 195 frames evictados, e a cena no fim do
arquivo diferente da do comeco — ou seja, o circular estava descartando o mais
antigo e o replay saiu com os 30 s cheios, nao com os 30 s que por acaso estavam
na memoria no instante do disparo.

### conferir o arquivo gerado

```bash
# so ver o que apareceu
dir replays

# a forma concludente: ler de volta e comparar com o esperado
python -c "import cv2; c=cv2.VideoCapture(r'replays\replay_AAAAMMDD_HHMMSS.mp4'); n=c.get(cv2.CAP_PROP_FPS); f=c.get(cv2.CAP_PROP_FRAME_COUNT); print(f'{f:.0f} frames, {f/n:.2f}s'); print('abre:', c.read()[0]); c.release()"
```

Esperado com os defaults: **900 frames, ~30,00 s, 640x480**, e `abre: True`.
Se `abre: False`, o arquivo esta corrompido — e o codigo teria descartado, entao
isso indicaria um arquivo de outra execucao.

### Se algo parecer errado

| Sintoma | Causa quase certa |
|---|---|
| Enter nao faz nada | O foco esta numa terceira janela. O Enter e lido pela janela do video **e** pelo console; nas duas funciona. |
| O slider nao muda com as setas | O trackbar do OpenCV so responde a arrasto de mouse, nao a teclado. Arraste. |
| A janela nao abriu | Sessao sem desktop (SSH, servico). O app avisa e segue funcionando pelo console. |
| Replay em camera lenta | `--export-fps` diferente de `--fps`. O codigo avisa no inicio. |
| Replay curto (poucos segundos) | Disparou antes do buffer encher. Normal nas primeiras execucoes. |
| Mais de 2 linhas vermelhas de OpenH264 | Voce nao usou o `replaycam.cmd`. Use o launcher (8 linhas viram 2). |
| `Nenhuma camera encontrada` | Permissao de camera do Windows desligada. |
| `FALHOU: buffer vazio` | Disparou antes do primeiro frame (warm-up de ate ~1,8 s). |
| Processo nao encerra com Ctrl+C | Use Ctrl+Break; os dois estao tratados. |

---

## Testes automatizados

```bash
python -m pytest tests/ -q     # 78 testes, ~11 s
python -m pyflakes replaycam tests
```

Cobrem a aritmetica da janela temporal (incluindo o caso em que a camera
entrega 15 fps num buffer de 30 fps), `snapshot` sob push concorrente, a
contabilidade exata de RAM, `set_seconds` re-criando a deque sob lock, a cadeia
de fallback de codec (injetando falha, e nao com tag invalida — ver a limitacao
acima), debounce de gatilho, o contrato de `Config`, a sondagem de cameras com
`probe` injetado, o fallback de preview sem display, e a camada de preview: os
pixels do overlay (inclusive legibilidade em cena clara e escura) e o
`RetroController` (teto vindo da RAM, clamp, debounce de arrasto, projecao de
memoria).

O teto de RAM tambem e testado como propriedade, nao como caso: para varias
resolucoes e orcamentos, o que o app reserva tem de caber no orcamento. Isso
pegou um bug real — o teto era um teto que nao limitava, porque converter um
limite em bytes para um numero inteiro de frames arredondava para cima
(1024 MiB em 1080p viravam 1026 MiB) e o slider arredondava 2,67 s para 3 s
(534 MiB contra um orcamento de 512 MiB). Agora o teto e calculado em frames,
com arredondamento para baixo.

O `--self-test` cobre o caminho completo sem camera — util porque o jeito mais
comum de a camera falhar no Windows e permissao de privacidade, o que mascara
bug de codigo de verdade.

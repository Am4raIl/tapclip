"""Thread de captura da camera.

Esta thread e a DONA EXCLUSIVA da `cv2.VideoCapture`. Nenhuma outra thread
toca no dispositivo, o que evita a classe de bugs em que duas leituras
concorrentes do driver MSMF/DSHOW corrompem o stream ou travam a camera.

O loop e deliberadamente enxuto - `read()`, um `resize` condicional, um `push`.
Nao ha escrita em disco, nao ha lock de disco, nao ha log por frame. Tudo o que
custa tempo (gravacao de arquivo, preview) vive em outra thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from .ringbuffer import RingBuffer

BACKENDS: dict[str, int] = {
    "auto": cv2.CAP_ANY,
    "any": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
}


@dataclass
class CaptureStats:
    """Telemetria da captura, usada para o aviso de FPS divergente."""

    opened: bool = False
    backend: str = ""
    # Resolucao real, vista no frame. E a fonte confiavel.
    actual_width: int = 0
    actual_height: int = 0
    # O que a camera DECLARA. No MSMF costuma vir 0 - ver `refresh_properties`.
    reported_width: int = 0
    reported_height: int = 0
    requested_fps: float = 0.0
    reported_fps: float = 0.0
    measured_fps: float = 0.0
    frames: int = 0
    failed_reads: int = 0
    resizes: int = 0
    started_at: float = 0.0
    first_frame_at: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def uptime(self) -> float:
        """Tempo desde a abertura da camera. Inclui o warm-up do driver."""
        return time.perf_counter() - self.started_at if self.started_at else 0.0

    @property
    def stream_uptime(self) -> float:
        """Tempo desde o PRIMEIRO frame, ou seja, a taxa efetiva de verdade.

        Medido em ~1,8 s de diferenca nesta maquina: o warm-up do MSMF e
        grande o bastante para distorcer qualquer media de FPS que o inclua.
        """
        if not self.first_frame_at:
            return 0.0
        return time.perf_counter() - self.first_frame_at

    @property
    def buffer_width(self) -> int:
        return self.actual_width

    @property
    def buffer_height(self) -> int:
        return self.actual_height

    def describe(self) -> str:
        """Linha de log honesta: separa o que a camera promete do que ela entrega."""
        promised = (f"{self.reported_width}x{self.reported_height}@{self.reported_fps:g}"
                    if self.reported_width else "? (driver MSMF nao reporta)")
        return (f"{self.actual_width}x{self.actual_height} real, "
                f"declarado {promised}, medido {self.measured_fps:g} fps "
                f"(pedido {self.requested_fps:g})")


# Quantas falhas CONSECUTIVAS de abertura encerram a sondagem. Dois e o ponto
# certo: se os indices 1 e 2 falharam, e porque nao ha indice 3 - e o caso
# comum, ja que o FFMPEG expoe menos devices que os indices que tentamos. Um
# acerto zera o contador, entao layouts esparses ( cameras em 0 e 2) continuam
# sendo descobertos. O parametro existe para os testes poderem exercitar o
# corte sem camera nenhuma.
PROBE_MISS_LIMIT = 2


def _probe_camera(
    idx: int, api: int, width: int = 0, height: int = 0, fps: float = 0.0
) -> dict[str, object] | None:
    """Abre UM indice em modo somente-leitura e descreve o que ele entrega.

    Devolve `None` se o indice nao abriu. Nao forca resolucao nenhuma: pedir
    320x240 e o que fazia esta sondagem mentir, reporting um modo que a camera
    nunca usaria. O que se pede e o MESMO modo do app -- com 0, o modo nativo
    --, e o que volta e medido no `frame.shape`, e nao nas propriedades do
    driver, que no MSMF costumam vir 0x0 e racy (ver `refresh_properties`).

    `width`/`height` sao aplicados de verdade, como na captura normal. Antes
    eles eram so um rotulo em `list_cameras` e nunca chegavam aqui, entao
    `--list-cameras --width 1920 --height 1080` anunciava um modo que nao era
    o que a camera usaria -- exatamente o numero que se quer checar antes de
    investir num modo novo.
    """
    cap = cv2.VideoCapture(idx, api)
    try:
        if not cap.isOpened():
            return None
        # BUFFERSIZE=1 so corta latencia; nao altera resolucao nem formato.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if width > 0 and height > 0:
            # `set` e um pedido, nao uma garantia: o driver pode rebaixar. Por
            # isso o que vale para o log e o `frame.shape` abaixo.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps > 0:
            cap.set(cv2.CAP_PROP_FPS, fps)
        ret, frame = cap.read()
        declared_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        declared_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        # `frame.shape` e a fonte confiavel: as propriedades do driver no MSMF
        # costumam vir 0x0 e racy (ver `refresh_properties`).
        real = None if (not ret or frame is None) else (int(frame.shape[1]), int(frame.shape[0]))
        return {
            "index": idx,
            "width": real[0] if real else declared_w,
            "height": real[1] if real else declared_h,
            "declared_width": declared_w,
            "declared_height": declared_h,
            "fps": round(cap.get(cv2.CAP_PROP_FPS), 2),
            "fourcc": int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("latin-1", "replace"),
            "frame_size": real,
        }
    finally:
        cap.release()


def list_cameras(
    indices: range = range(0, 6),
    backend: str = "auto",
    width: int = 0,
    height: int = 0,
    miss_limit: int = PROBE_MISS_LIMIT,
    fps: float = 0.0,
    probe: "Callable[[int, int], dict[str, object] | None] | None" = None,
) -> list[dict[str, object]]:
    """Sonda indices de camera em modo SOMENTE LEITURA e devolve o que existe.

    Nao grava nada em disco. Se a lista vier vazia no Windows, quase sempre e
    Setting > Privacy > Camera desligado.

    `width`/`height` sao aplicados ao driver de verdade, como na captura normal,
    e o que a lista mostra e a resolucao REAL lida do frame. `0` = modo nativo.
    `probe` existe para injetar um substituto nos testes, e por isso aceita a
    assinatura curta de dois argumentos.
    """
    api = BACKENDS.get(backend, cv2.CAP_ANY)
    if probe is not None:
        open_one: Callable[[int, int], dict[str, object] | None] = probe
    else:
        open_one = lambda i, _api: _probe_camera(i, _api, width, height, fps)
    found: list[dict[str, object]] = []
    misses = 0
    for idx in indices:
        info = open_one(idx, api)
        if info is None:
            misses += 1
            if misses >= miss_limit:
                break
            continue
        misses = 0
        info.setdefault("requested", f"{width}x{height}" if width and height else "nativo")
        found.append(info)
    return found


class CameraCapture(threading.Thread):
    """Le frames da camera e empurra para o RingBuffer ate `stop_event` ser setado."""

    def __init__(
        self,
        ring: RingBuffer,
        stop_event: threading.Event,
        device: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: float = 30.0,
        buf_width: int = 640,
        buf_height: int = 480,
        backend: str = "auto",
        on_error: object = None,
    ) -> None:
        super().__init__(name="capture", daemon=True)
        self.ring = ring
        self.stop_event = stop_event
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.buf_width = buf_width
        self.buf_height = buf_height
        self.backend = backend
        self.on_error = on_error
        self.stats = CaptureStats(requested_fps=fps)
        # Slot de 1 frame para a preview, em resolucao de CAPTURA (antes do
        # downscale). Sem isto a preview mostraria o conteudo do ring, que ja
        # foi reduzido para `buf_*` - e apareceria 640x480 mesmo com a camera
        # em 1080p. E a diferenca entre "ver a camera" e "ver o replay".
        self._latest: np.ndarray | None = None
        self._latest_lock = threading.Lock()

    # ------------------------------------------------------- preview full-res

    def publish_latest(self, frame: np.ndarray) -> None:
        """Publica o frame em resolucao plena para a preview ler.

        Chamado ANTES de `_maybe_resize`, que devolve um array novo - a
        referencia publicada continua apontando para o frame original, intacto.
        Segurar referencia (sem `.copy()`) e seguro pelo mesmo motivo do
        `RingBuffer.push`: ver "Ownership dos arrays numpy".
        """
        with self._latest_lock:
            self._latest = frame

    def latest_frame(self) -> np.ndarray | None:
        """Ultimo frame publicado, ou `None` antes do primeiro frame chegar."""
        with self._latest_lock:
            return self._latest

    # ------------------------------------------------------------------ helpers

    def open_camera(self) -> cv2.VideoCapture:
        """Abre a camera e aplica as propriedades NA ordem que o driver respeita.

        Ordem importa: em DSHOW, pedir FPS antes da resolucao evita que o driver
        refaca o modo do dispositivo e volte com um FPS diferente do pedido.
        """
        api = BACKENDS.get(self.backend, cv2.CAP_ANY)
        cap = cv2.VideoCapture(self.device, api)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.device, cv2.CAP_ANY)
            if not cap.isOpened():
                cap.release()
                raise RuntimeError(
                    f"nao consegui abrir a camera {self.device}. "
                    "Rode `python -m replaycam --list-cameras`; no Windows verifique "
                    "Settings > Privacy & security > Camera."
                )

        # FPS primeiro, depois resolucao, depois buffer minimo. Resolucao 0
        # significa "nao pede nada": o driver fica no modo nativo. E o default
        # porque o app reduz tudo para `buf_*` de qualquer jeito - pedir 1080p
        # para depois jogar fora 3 em cada 4 pixels so gasta banda de USB e CPU.
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if self.width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # BUFFERSIZE=1 corta a latencia interna do driver (DSHOW respeita,
        # MSMF ignora). Entrega o frame mais recente em vez de um atrasado.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.stats.opened = True
        self.stats.backend = self.backend
        self.refresh_properties(cap)
        return cap

    def refresh_properties(self, cap: cv2.VideoCapture) -> None:
        """Atualiza as propriedades declaradas, SEM aceitar leitura de zero.

        Medido nesta maquina (camera integrada, backend MSMF): a primeira
        leitura pode devolver 0x0 e 0 fps, porque o driver so preenche essas
        propriedades depois que o stream de fato comeca. Como a leitura logo no
        primeiro frame corre para o lado racista, o codigo:

          -         so sobrescreve quando o valor lido e diferente de zero, para nao
            sobrescrever um dado bom com um 0 transitorio;
          - e chamada de novo a cada janela de 2 s, o que faz a leitura se
            auto-corrigir sozinha.

        A resolucao REAL continua vindo de `note_frame_shape` e o FPS real dos
        timestamps - essas duas nao dependem do driver mentir.
        """
        fps = round(cap.get(cv2.CAP_PROP_FPS), 2)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if fps > 0:
            self.stats.reported_fps = fps
        if width > 0 and height > 0:
            self.stats.reported_width = width
            self.stats.reported_height = height

    def note_frame_shape(self, frame: np.ndarray) -> None:
        """Registra a resolucao REAL entregue pela camera, vista no frame.

        Esta e a unica fonte confiavel: se pedirmos 1920x1080 e a camera
        entregar 1280x720, e aqui que isso aparece.

        So gera AVISO quando houve pedido explicito (`width > 0`). No modo
        nativo nao ha pedido para comparar - o driver escolheu, e escolher e o
        comportamento esperado, nao um desvio.
        """
        if not self.stats.actual_width:
            self.stats.actual_height, self.stats.actual_width = frame.shape[:2]
            if self.width > 0 and (self.stats.actual_width, self.stats.actual_height) != (self.width, self.height):
                self.stats.warnings.append(
                    f"camera entregou {self.stats.actual_width}x{self.stats.actual_height} "
                    f"em vez de {self.width}x{self.height} pedidos: o driver fixou em outro modo "
                    f"(confirme com --list-cameras se {self.width}x{self.height} e suportado)."
                )

    def _maybe_resize(self, frame: np.ndarray) -> np.ndarray:
        """Reduz o frame ao tamanho do buffer, se preciso.

        INTER_AREA e o filtro correto para MINIFICACAO (faz media ponderada dos
        pixels, evita serrilhado/aliasing). So redimensiona se a differenca
        justificar o custo; a maioria das cameras ja entrega perto do alvo.
        """
        h, w = frame.shape[:2]
        if w == self.buf_width and h == self.buf_height:
            return frame
        shrinking = self.buf_width <= w and self.buf_height <= h
        interp = cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
        self.stats.resizes += 1
        return cv2.resize(frame, (self.buf_width, self.buf_height), interpolation=interp)

    def _check_fps_drift(self, frames: int, elapsed: float) -> None:
        """Compara o FPS efetivamente entregue com o pedido e avisa se divergir.

        E o defeito silencioso mais perigoso deste sistema: se a camera entrega
        15 fps quando voce pediu 30, um buffer dimensionado por contagem de
        frames seguraria 60 s de tempo real e a exportacao a 30 fps sairia em
        camera lenta. A deteccao aqui transforma isso num aviso visivel.

        IMPORTANTE: esta janela so comeca a contar depois do PRIMEIRO frame.
        Medir desde a abertura daria um numero errado - na medicao real deste
        projeto a camera levou ~1,8 s para entregar o primeiro frame, e uma
        janela iniciada na abertura reportava 26,9 fps quando o taxa real era
        29,8 fps. Um aviso falso aqui e pior que nenhum aviso, porque manda o
        usuario mexer em um `--export-fps` que estava certo.
        """
        if elapsed < 2.0 or frames < 2:
            return
        measured = frames / elapsed
        self.stats.measured_fps = round(measured, 2)
        requested = self.fps
        if requested <= 0:
            return

        drift = abs(measured - requested) / requested
        if drift > 0.10:
            self.stats.warnings.append(
                f"FPS efetivo {measured:.1f} difere do pedido {requested:.1f} "
                f"({measured / requested:.2f}x). Exportar a {requested:g} fps deixa o "
                f"replay {'acelerado' if measured > requested else 'em camera lenta'}. "
                f"Ajuste --export-fps para {measured:.1f}, ou reduza a resolucao / "
                f"verifique o modo da camera."
            )

    # --------------------------------------------------------------------- run

    def run(self) -> None:
        cv2.setNumThreads(1)  # contencao de CPU: 1 thread de OpenCV por processo
        try:
            cap = self.open_camera()
        except RuntimeError as exc:
            self.stats.warnings.append(str(exc))
            if callable(self.on_error):
                self.on_error(exc)
            return

        self.stats.started_at = time.perf_counter()
        # A janela de medicao de FPS so comeca no primeiro frame, nao na
        # abertura: o warm-up da camera (medido em ~1,8 s aqui) contaminaria
        # a primeira janela e geraria um aviso de FPS falso. Ver
        # `_check_fps_drift`.
        window_start: float | None = None
        window_frames = 0
        got_first_frame = False
        last_resize_log = 0.0

        try:
            while not self.stop_event.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    self.stats.failed_reads += 1
                    # Nao chama waitKey nem sleep aqui: um read que falha e
                    # instantaneo, e dormir so desperdicaria a janela do buffer.
                    continue

                if not got_first_frame:
                    # Agora o driver ja inicializou: as propriedades valem algo.
                    self.refresh_properties(cap)
                    got_first_frame = True
                    self.stats.first_frame_at = time.perf_counter()
                    window_start = self.stats.first_frame_at

                self.note_frame_shape(frame)
                # Publica ANTES do resize: e o unico momento em que o frame
                # ainda esta em resolucao de captura.
                self.publish_latest(frame)
                frame = self._maybe_resize(frame)
                self.ring.push(frame, time.perf_counter())
                self.stats.frames += 1
                window_frames += 1

                now = time.perf_counter()
                if window_start is not None and (now - window_start) >= 2.0:
                    # Reamostra as propriedades a cada janela: no MSMF a
                    # primeira leitura pode vir 0, e uma leitura so nao se
                    # recupera sozinha. Custo: 3 getters de 2 em 2 segundos.
                    self.refresh_properties(cap)
                    self._check_fps_drift(window_frames, now - window_start)
                    window_frames = 0
                    window_start = now

                # Log de resize amortizado: em 30 fps o log por frame vira ruido
                # e custa stdout lock, que e exatamente o tipo de coisa que
                # segura a thread de captura.
                if self.stats.resizes != last_resize_log and now - window_start > 1.0:
                    last_resize_log = self.stats.resizes
        finally:
            cap.release()
            if self.stats.first_frame_at and not self.stats.measured_fps:
                self.stats.measured_fps = round(
                    self.stats.frames / max(self.stats.stream_uptime, 1e-6), 2
                )

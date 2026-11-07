"""Orquestracao: junta captura, ring buffer, gatilho e escrita em disco.

## Topologia das threads

    [trigger]  Enter -> queue(maxsize=1, drop-oldest)
                          |
    [capture]  cap.read() -> resize -> ring.push()      nunca toca disco
                          |
                     RingBuffer (deque + 1 lock)
                          |
                     list(dq)  ~0.01 ms
                          |
    [writer]  <-- dequeue -- VideoWriter ... release()  unica thread em disco

Por que uma fila de tamanho 1 em vez de uma thread nova por disparo: o
`VideoWriter` nao e thread-safe, entao Concentrar toda escrita em UMA thread e
o que torna o codigo seguro. Com `maxsize=1` e drop-oldest, apertar Enter
cinco vezes seguidas durante um save nao cria cinco threads nem cinco
gravacoes concorrentes - no maximo um save pendente, e o usuario recebe um log
dizendo qual pedido foi substituido.
"""

from __future__ import annotations

import math
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .capture import CameraCapture, list_cameras
from .config import Config, config_from_args
from .ringbuffer import RingBuffer
from .triggers import KeyboardEnterTrigger, Trigger
from .writer import (
    DEFAULT_FOURCC_CHAIN,
    VideoExporter,
)


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


# O `opencv-python` nao embarca a DLL do OpenH264, entao o backend FFMPEG
# falha e o OpenCV cai no Media Foundation, que encode H.264 de verdade no
# Windows 10+. Essas linhas vermelhas SAO o fallback funcionando.
#
# O default baixa o log do OpenCV (`writer.silence_expected_ffmpeg_h264_fallback`),
# o que reduz o ruido de 5 para 2 linhas. As 2 restantes vem do carregador da
# libopenh264 e nao tem como silencia-las por configuracao - ver a nota de
# `--verbose`, que explica isso e mostra o log nativo inteiro.
FFMPEG_NOISE_NOTE = (
    " verbose  : log nativo do OpenCV/FFmpeg LIGADO.\n"
    "            O 'opencv-python' nao embarca a DLL do OpenH264, entao o FFMPEG\n"
    "            falha e o OpenCV cai no encoder H.264 nativo do Windows, que\n"
    "            funciona. As 2 linhas do carregador ('Failed to load OpenH264\n"
    "            library') nao podem ser silenciadas: so somem baixando a DLL em\n"
    "            github.com/cisco/openh264/releases. O log 'codec real=' confirma\n"
    "            o que foi gravado."
)

PREVIEW_KEYS_HELP = "Enter=salvar  ESC=sair  arraste o slider para o tempo retroativo"

PREVIEW_WINDOW = "replaycam"


def draw_overlay(canvas, lines: list[str], hint: str) -> None:
    """Escreve o texto de status sobre a preview, no lugar.

    Cada linha e desenhada DUAS vezes: primeiro com contorno preto grosso, e
    depois em branco por cima. Sem o contorno o texto some em cena clara - que
    e justamente o caso de uso (a camera apontada para uma parede).

    A espessura do texto e 2, e nao 1 por um motivo medido: com 1, a
    antialiasing faz o glifo nunca passar de 250 num fundo 250, e o texto
    branco fica invisivel sobre fundo claro - aparecia so o contorno. Com 2 o
    nucleo do glifo e solido e o texto aparece em cena clara E escura.

    Funcao de modulo, e nao closure, para poder ser testada de verdade: um
    `cv2.putText` que nunca foi executado nao tem cobertura nenhuma.
    """
    for i, text in enumerate(lines):
        y = 22 + i * 20
        cv2.putText(canvas, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
    y = canvas.shape[0] - 12
    cv2.putText(canvas, hint, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(canvas, hint, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 2, cv2.LINE_AA)


class RetroController:
    """Slider da preview que reajusta o tempo retroativo em tempo real.

    O teto do slider NAO e um numero chutado: vem de `Config.max_retro_seconds`,
    que divide o orcamento de RAM (`--max-buffer-mib`) pelo custo por segundo do
    buffer. E o que impede arrastar para 60 s num buffer de 1080p, que
    exigiria 5,2 GiB. A restricao aparece na propria tela em vez de virar
    travamento.

    O `on_change` do `createTrackbar` dispara a cada pixel arrastado, e o
    `set_seconds` reconstrói a deque. Por isso o debounce: sem ele, um arrasto
    de 2 s refaria a deque dezenas de vezes, prendendo o lock que a thread de
    captura usa a cada frame.
    """

    DEBOUNCE = 0.25

    def __init__(self, ring: RingBuffer, cfg: Config) -> None:
        self.ring = ring
        # `floor`, nao `round`: o trackbar so aceita inteiros, e arredondar 2,67
        # para 3 estouraria o teto que o proprio slider existe para respeitar
        # (3 s a 1080p = 534 MiB contra um orcamento de 512 MiB). O `min` com
        # `max_frames` e a segunda rede de seguranca, caso o teto caia em 0.
        self.max_seconds = max(1, int(math.floor(cfg.max_retro_seconds)))
        self.max_seconds = min(self.max_seconds, cfg.max_frames)
        self.initial = max(1, min(self.max_seconds, int(round(cfg.effective_seconds))))
        self._per_frame = max(1, cfg.bytes_per_frame)
        self._fps = cfg.fps
        self._pending = self.initial
        self._last_applied = self.initial
        self._last_change = 0.0

    def _projected_mib(self, seconds: float) -> float:
        """RAM que o buffer ocuparia se enchesse com essa janela.

        Depende de `seconds` de verdade: e a projecao do que o usuario esta
        escolhendo no slider, nao do que o buffer tem hoje.
        """
        frames = max(1, int(round(self._fps * seconds)))
        return frames * self._per_frame / (1024 * 1024)

    def install(self) -> None:
        cv2.createTrackbar(
            f"retroativo (s) [max {self.max_seconds}]",
            PREVIEW_WINDOW,
            self.initial,
            self.max_seconds,
            self._on_change,
        )

    def _on_change(self, value: int) -> None:
        # So registra a intencao; a aplicacao real acontece em `poll`, no laco
        # da preview, depois do debounce.
        self._pending = max(1, int(value))
        self._last_change = time.perf_counter()

    def poll(self) -> None:
        """Aplica o valor pendente se o usuario parou de arrastar."""
        if self._pending == self._last_applied:
            return
        if time.perf_counter() - self._last_change < self.DEBOUNCE:
            return
        target = self._pending
        self._last_applied = target
        try:
            self.ring.set_seconds(float(target))
        except (RuntimeError, ValueError) as exc:
            print(f" [slider] nao consegui mudar o retroativo: {exc}")
            return
        st = self.ring.stats()
        print(f" [slider] tempo retroativo -> {target}s "
              f"(teto {st.capacity} frames, ~{self._projected_mib(target):.0f} MiB, "
              f"agora {st.count} frames / {st.span_seconds:.1f}s)")


def _print_header(cfg: Config) -> None:
    cap_mode = f"{cfg.width}x{cfg.height}" if (cfg.width and cfg.height) else "nativo"
    print("=" * 68)
    print(" replaycam - ring buffer em RAM")
    print("=" * 68)
    print(f" captura   : device={cfg.device} backend={cfg.backend} "
          f"{cap_mode}@{cfg.fps:g}   (buffer {cfg.buf_width}x{cfg.buf_height})")
    print(f" qualidade : preset {cfg.quality}   "
          f"(buffer {cfg.buf_width}x{cfg.buf_height}, "
          f"orcamento {cfg.max_buffer_mib:g} MiB)")
    if cfg.mirror_preview:
        print(" espelho   : horizontal (preview e MP4)")
    # Mostra o tempo REALMENTE usado. Se o pedido foi cortado pelo teto de RAM,
    # a diferenca e dita na cara: silenciar isso seria mentir sobre a memoria
    # que o processo vai reservar.
    if cfg.seconds_clamped:
        print(f" buffer    : {cfg.buf_width}x{cfg.buf_height} por "
              f"{cfg.effective_seconds:.0f}s "
              f"(~{cfg.effective_capacity} frames, ~{cfg.effective_buffer_mib:.0f} MiB)")
        print(f"             --seconds {cfg.seconds:g}s foi cortado pelo teto de "
              f"{cfg.max_buffer_mib:g} MiB")
    else:
        print(f" buffer    : {cfg.buf_width}x{cfg.buf_height} por {cfg.seconds:g}s "
              f"(~{int(round(cfg.fps * cfg.seconds))} frames, ~{cfg.estimated_buffer_mib():.0f} MiB)")
    print(f" saida     : {cfg.outdir}/  export_fps={cfg.effective_export_fps:g}")
    print(f" gatilhos  : {', '.join(cfg.triggers)}")
    if cfg.preview:
        print(f" preview   : janela ligada (--no-preview para fechar). {PREVIEW_KEYS_HELP}")
    for w in cfg.validate():
        print(f" AVISO: {w}")
    if cfg.verbose:
        print(FFMPEG_NOISE_NOTE)
    print("=" * 68)


# --------------------------------------------------------------------- self-test


def run_self_test(cfg: Config) -> int:
    """Valida ring buffer + writer SEM camera. Retorna 0 se tudo passou.

    Existe porque o caminho mais comum de falha em Windows e a camera estar
    bloqueada por privacidade/permissao, o que mascara bugs reais de codigo.
    """
    outdir = Path(cfg.outdir)
    print("[self-test] simulando captura (sem camera)...")
    if cfg.verbose:
        print(FFMPEG_NOISE_NOTE)
    failures: list[str] = []
    BGR_SHAPE = (64, 48)  # (largura, altura) dos frames sinteticos dos testes

    # 1. RingBuffer respeita a janela temporal mesmo com FPS muito abaixo do pedido.
    ring = RingBuffer.for_duration(fps=30.0, seconds=2.0)  # 60 frames de teto
    t0 = time.perf_counter()
    for i in range(150):  # 150 frames a 30 fps = 5 s de tempo -> buffer deve segurar 2 s
        ring.push(np.zeros((cfg.buf_height, cfg.buf_width, 3), np.uint8), t0 + i / 30.0)
    st = ring.stats()
    if abs(st.span_seconds - 2.0) > 0.15:
        failures.append(f"janela temporal errada: {st.span_seconds:.2f}s (esperado ~2.0s)")
    print(f"[self-test] janela temporal: {st.span_seconds:.2f}s em {st.count}/{st.capacity} frames")
    if st.nbytes <= 0:
        failures.append("contagem de bytes ficou em zero")
    if _fmt_bytes(st.nbytes) == "0.0 B":
        failures.append("bytes do buffer nao foram contabilizados")
    print(f"[self-test] RAM contabilizada: {_fmt_bytes(st.nbytes)}")

    # 2. Snapshot nao pode levantar RuntimeError durante push concorrente.
    ring2 = RingBuffer.for_duration(30.0, 2.0)
    errors: list[str] = []

    def pusher() -> None:
        try:
            for i in range(400):
                ring2.push(np.zeros((48, 64, 3), np.uint8), time.perf_counter())
                time.sleep(0.001)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pusher: {exc!r}")

    threads = [threading.Thread(target=pusher) for _ in range(3)]
    for th in threads:
        th.start()
    snapshots = 0
    while any(th.is_alive() for th in threads):
        # A copia local e o que a thread writer faz; percorre-la aqui e o
        # que transformaria um bug de lock em RuntimeError de "deque mutated".
        for frame in ring2.snapshot():
            assert frame.shape == (BGR_SHAPE[1], BGR_SHAPE[0], 3)
        snapshots += 1
        time.sleep(0.001)  # sem isto o teste vira busy-spin e queima um nucleo
    for th in threads:
        th.join()
    if errors:
        failures.extend(errors)
    if snapshots < 5:
        failures.append(f"poucos snapshots durante concorrencia ({snapshots})")
    print(f"[self-test] concorrencia: {snapshots} snapshots sem erro")

    # 3. Writer precisa produzir um .mp4 com a duracao correta.
    n_frames = 60
    fps = 30.0
    size = (cfg.buf_width // 2 * 2, cfg.buf_height // 2 * 2)
    # Frames com movimento real: um arquivo de frames pretos identicos pode
    # comprimir tao forte que o tamanho nao serve como prova de nada.
    frames = []
    for i in range(n_frames):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        frame[:, (i * 7) % size[0]] = 255
        frames.append(frame)

    exporter = VideoExporter(
        outdir=outdir,
        fourcc_chain=(cfg.fourcc,) if cfg.fourcc else DEFAULT_FOURCC_CHAIN,
    )
    result = exporter.export(frames, fps)
    if not result.ok:
        failures.append(f"writer falhou: {result.error}")
    else:
        codec = result.actual_fourcc or "?"
        print(
            f"[self-test] writer: {result.path.name}"
            f" ({_fmt_bytes(result.size_bytes)}"
            f", ~{result.bitrate_mbps:.1f} Mbps"
            f", fourcc pedido={result.requested_fourcc} real={codec}"
            f", {result.frames} frames"
            f", {result.elapsed_seconds * 1000:.0f} ms)"
        )
        if result.frames != n_frames:
            failures.append(f"gravou {result.frames} frames, esperado {n_frames}")
        dur = exporter.probe_duration(result.path)
        expected = n_frames / fps
        if abs(dur - expected) > 0.25:
            failures.append(f"duracao lida do arquivo {dur:.2f}s != {expected:.2f}s esperada")
        else:
            print(f"[self-test] duracao lida de volta do .mp4: {dur:.2f}s (ok)")
        result.path.unlink(missing_ok=True)

    print("-" * 68)
    if failures:
        print(f"[self-test] FALHOU ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("[self-test] OK - ring buffer, concorrencia e writer validados.")
    return 0


# ------------------------------------------------------------------- list cameras


def run_list_cameras(cfg: Config) -> int:
    print(f" sondando cameras (backend={cfg.backend})...")
    cams = list_cameras(backend=cfg.backend, width=cfg.width, height=cfg.height)
    if not cams:
        print(" Nenhuma camera encontrada.")
        print(" Se for Windows: Settings > Privacy & security > Camera > Camera access = ON.")
        return 1
    buffer_mode = f"{cfg.buf_width}x{cfg.buf_height}"
    for c in cams:
        mode = f"{c['width']}x{c['height']}"
        # `real` vem do frame; `declarado` vem do driver, que no MSMF costuma
        # mentir (0x0) ou ficar para tras. Divergir entre os dois e o sinal de
        # que o modo pedido nao foi atendido.
        real = c["frame_size"]
        detail = f"real={mode} @ {c['fps']} fps"
        if c.get("declared_width") and (c["declared_width"], c["declared_height"]) != (c["width"], c["height"]):
            detail += f"  (driver declarou {c['declared_width']}x{c['declared_height']})"
        # Importante: `real` e o que a CAMERA entrega, nao o que sera gravado.
        # Se a captura e maior que o buffer, o replay sai em `buffer_mode`, e
        # dizer o contrario levaria a fechar a conta de RAM com o numero errado.
        downscale = (c["width"], c["height"]) != (cfg.buf_width, cfg.buf_height)
        tail = f"  -> buffer {buffer_mode}" if downscale else "  (buffer igual a captura)"
        print(f" [{c['index']}] {detail}  pedido={c.get('requested')}  frame={real}{tail}")
    print(f" {len(cams)} camera(s) encontrada(s). Use: -d <indice>")
    print(f" 'real' e o que a camera entrega. O replay sai em {buffer_mode} (--buf-*), "
          "que e o que ocupa RAM.")
    if cfg.width and cfg.height and any(
        (c["width"], c["height"]) != (cfg.width, cfg.height) for c in cams
    ):
        # Situacao que mais importa num plano de 1080p: o modo pedido nao existe.
        print(f" ATENCAO: voce pediu {cfg.width}x{cfg.height} e nenhuma camera "
              "entregou esse modo. O driver rebaixou; o limite e do driver do SO,")
        print("          nao do OpenCV. Nao da para forcar por software.")
    return 0


# --------------------------------------------------------------------- build/gatilho


def build_triggers(cfg: Config, on_fire) -> list[Trigger]:
    """Instancia os gatilhos pedidos. E o unico lugar que conhece o registro.

    Para adicionar um gatilho novo: escreva a subclasse em `triggers.py` e
    registre aqui. Nada mais no sistema precisa mudar.
    """
    registry = {"keyboard": KeyboardEnterTrigger}
    built: list[Trigger] = []
    for name in cfg.triggers:
        if name == "keyboard":
            built.append(KeyboardEnterTrigger(on_fire, debounce=cfg.debounce))
            continue
        factory = registry.get(name)
        if factory is None:
            raise ValueError(f"gatilho desconhecido: {name!r} (disponiveis: {', '.join(registry)})")
        built.append(factory(on_fire, debounce=cfg.debounce))
    return built


# ------------------------------------------------------------------------ runtime


def run(cfg: Config) -> int:
    _print_header(cfg)
    # NOTA: nao chamamos `silence_expected_ffmpeg_h264_fallback` aqui de
    # proposito. Medido: ela roda DEPOIS que o `cv2` ja foi importado no topo
    # deste modulo, e a lib do OpenCV le `OPENCV_LOG_LEVEL` /
    # `OPENCV_FFMPEG_LOGLEVEL` uma vez so, no carregamento. Mudar
    # `os.environ` daqui nao tem efeito nenhum no processo atual - e o default
    # `verbose=False` do writer acabava mutando o ambiente global sem
    # silenciar nada. O silenciamento que funciona vive no `replaycam.cmd`, que
    # define as variaveis antes do processo nascer. Fica em `writer.py` para o
    # caso de um subprocesso futuro precisar dela.

    # O ring nasce JA dentro do orcamento de RAM. Sem isto, `--seconds 30` com
    # buffer de 1080p alocaria 5,2 GiB na partida, apesar do aviso: o teto
    # restringia o slider mas nao a construcao inicial. `effective_seconds` e a
    # unica autoridade; header, ring e slider leem o mesmo numero.
    ring = RingBuffer.for_duration(fps=cfg.fps, seconds=cfg.effective_seconds)
    stop_event = threading.Event()
    save_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)

    exporter = VideoExporter(outdir=cfg.outdir, prefix=cfg.prefix)
    if cfg.fourcc:
        exporter = VideoExporter(outdir=cfg.outdir, fourcc_chain=(cfg.fourcc,), prefix=cfg.prefix)
    try:
        exporter.ensure_outdir()
    except OSError as exc:
        print(f" ERRO: nao consegui criar {cfg.outdir}: {exc}")
        return 1

    # ---- writer thread: a UNICA que instancia VideoWriter ----
    def writer_loop() -> None:
        while not stop_event.is_set():
            try:
                source = save_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            # Snapshot logo no inicio: assim o conteudo corresponde ao instante
            # do gatilho, e nao a um instante depois.
            frames = ring.snapshot()
            res = exporter.export(frames, cfg.effective_export_fps)
            if res.ok:
                print(f" SALVO via {source}: {res.path}  "
                      f"({_fmt_bytes(res.size_bytes)}, {res.duration_seconds:.1f}s, "
                      f"~{res.bitrate_mbps:.1f} Mbps, "
                      f"fourcc={res.actual_fourcc or '?'}, {res.elapsed_seconds * 1000:.0f} ms)  "
                      f"buffer tinha {len(frames)} frames")
            else:
                print(f" FALHOU via {source}: {res.error}")
            save_queue.task_done()

    writer = threading.Thread(target=writer_loop, name="writer", daemon=True)
    writer.start()

    # ---- capture thread ----
    capture = CameraCapture(
        ring=ring, stop_event=stop_event, device=cfg.device, width=cfg.width,
        height=cfg.height, fps=cfg.fps, buf_width=cfg.buf_width,
        buf_height=cfg.buf_height, backend=cfg.backend,
        mirror_preview=cfg.mirror_preview,
        on_error=lambda exc: print(f" ERRO de captura: {exc}"),
    )
    capture.start()

    # ---- gatilho(s) ----
    def on_fire(source: str) -> None:
        # Put nowait com drop-oldest: um burst de Enter durante um save
        # substituivel o pedido pendente em vez de acumular trabalho.
        try:
            save_queue.put_nowait(source)
        except queue.Full:
            try:
                save_queue.get_nowait()
                save_queue.task_done()
                save_queue.put_nowait(source)
                print(" (disparo durante um save: fila mantida em 1, pedido antigo substituido)")
            except (queue.Empty, queue.Full):
                pass

    triggers: list[Trigger] = []
    try:
        triggers = build_triggers(cfg, on_fire)
    except ValueError as exc:
        print(f" ERRO: {exc}")
        stop_event.set()
        return 1
    for trg in triggers:
        trg.start()

    # ---- gatilho automatico, so para teste ----
    auto_thread: threading.Thread | None = None
    if cfg.auto_trigger > 0:
        def auto_loop() -> None:
            # `Event.wait()` devolve True se o evento foi setado e False no
            # timeout normal - ao contrario do que o nome sugere. Entao o
            # "alguem pediu para parar?" e o True.
            if stop_event.wait(cfg.auto_trigger):
                return  # encerrou antes da hora
            st = ring.stats()
            print(f" [auto-trigger] disparando sozinho apos {cfg.auto_trigger:g}s "
                  f"({st.count} frames no buffer)")
            on_fire("auto")

        auto_thread = threading.Thread(target=auto_loop, name="auto-trigger", daemon=True)
        auto_thread.start()

    # ---- status periodico ----
    stop_stats = threading.Event()

    def stats_loop() -> None:
        shown = False
        while not stop_stats.wait(cfg.stats_interval):
            if cfg.stats_interval <= 0:
                return
            st = ring.stats()
            print(f" [status] {st.count}/{st.capacity} frames  {_fmt_bytes(st.nbytes)}  "
                  f"janela={st.span_seconds:.1f}/{st.target_seconds:.0f}s  "
                  f"medido={ring.measured_fps():.1f} fps  capturado={capture.stats.frames}")
            if st.is_full and not shown:
                shown = True
                print(f" [pronto] buffer cheio ({_fmt_bytes(st.nbytes)}). "
                      f"{'Enter' if 'keyboard' in cfg.triggers else 'dispare um gatilho'} para gravar.")
                for w in capture.stats.warnings:
                    print(f" [aviso] {w}")

    stats_thread: threading.Thread | None = None
    if cfg.stats_interval > 0:
        stats_thread = threading.Thread(target=stats_loop, name="stats", daemon=True)
        stats_thread.start()

    # ---- preview: janela de video + slider do tempo retroativo ----
    # Roda em thread propria porque `imshow`/`waitKey` bloqueiam, e o custo de
    # blit precisa ficar fora do hot loop de captura.
    preview_stop = threading.Event()
    retro = RetroController(ring, cfg)
    window_ok = threading.Event()

    def _overlay_lines() -> list[str]:
        st = ring.stats()
        cap = capture.stats
        return [
            f"{cap.actual_width}x{cap.actual_height}  {cap.measured_fps:.1f} fps"
            f"   captura {cap.frames}",
            f"buffer {st.count}/{st.capacity} frames  {_fmt_bytes(st.nbytes)}"
            f" / {cfg.max_buffer_mib:g} MiB",
            f"retroativo {st.span_seconds:.1f}s de {st.target_seconds:.0f}s"
            f"{'  [cheio]' if st.is_full else ''}",
        ]

    def preview_loop() -> None:
        try:
            cv2.namedWindow(PREVIEW_WINDOW, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(PREVIEW_WINDOW, 960, 600)
            retro.install()
        except Exception as exc:  # noqa: BLE001
            # Sessao sem desktop (SSH, servico, WSL): a preview e um extra, nao
            # um requisito. Derrubar o app inteiro por causa dela seria errado -
            # a gravacao funciona igual pelo console.
            print(f" [preview] indisponivel ({exc}); seguindo sem janela. Use o console.")
            preview_stop.set()
            return
        window_ok.set()

        try:
            while not preview_stop.is_set():
                retro.poll()  # aplica o slider, com debounce
                # O frame de CAPTURA, nao o do buffer: e a diferenca entre ver a
                # camera em 1080p e ver o replay reduzido em `buf_*`.
                frame = capture.latest_frame()
                if frame is None:
                    time.sleep(0.01)
                    continue
                canvas = frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)
                # Escala so para CABER na janela. `buf_*` nunca e consultado
                # aqui, entao a preview mostra a resolucao que a camera entregou.
                max_w, max_h = 960, 540
                if canvas.shape[1] > max_w or canvas.shape[0] > max_h:
                    scale = min(max_w / canvas.shape[1], max_h / canvas.shape[0])
                    canvas = cv2.resize(
                        canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                    )
                canvas = canvas.copy()  # o overlay escreve; o frame original e intocado
                draw_overlay(canvas, _overlay_lines(), PREVIEW_KEYS_HELP)
                cv2.imshow(PREVIEW_WINDOW, canvas)
                key = cv2.waitKey(1) & 0xFF
                if key == 27:  # ESC fecha
                    # Sair de verdade, e nao so fechar a janela: `preview_stop`
                    # sozinho deixaria a captura, o ring e o console vivos sem
                    # ninguem explicando por que o app nao morreu. O `stop_event`
                    # global e o mesmo que Ctrl+C usa, entao o caminho de
                    # encerramento (writer, release, codigo de saida) e o
                    # testado. A thread da preview so nao se joina aqui, para
                    # nao dar join nela mesma.
                    stop_event.set()
                    break
                # Enter e' tratado aqui E no console porque a janela do OpenCV tem o foco
                # do teclado: enquanto ela estiver em frente, o `msvcrt` do
                # gatilho de console nao ve a tecla nenhuma. Sem este
                # tratamento, o Enter simplesmente nao faria nada - e o usuario
                # culparia o gatilho. Ambos os caminhos chamam o mesmo
                # `on_fire`, entao o comportamento e identico.
                if key in (13, 10):  # Enter / Return
                    on_fire("preview-enter")
        except Exception as exc:  # noqa: BLE001
            print(f" [preview] encerrada por erro ({exc}); a gravacao segue pelo console.")
        finally:
            try:
                cv2.destroyAllWindows()
            except Exception:  # noqa: BLE001
                pass

    preview_thread: threading.Thread | None = None
    if cfg.preview:
        preview_thread = threading.Thread(target=preview_loop, name="preview", daemon=True)
        preview_thread.start()

    # ---- sinalizacao: SIGINT/SIGTERM (e SIGBREAK) finalizam tudo limpo ----
    def handle_signal(signum, _frame) -> None:
        print("\n encerrando...")
        stop_event.set()
        stop_stats.set()
        preview_stop.set()

    # SIGBREAK so existe no Windows (Ctrl+Break no console). Sem ele, o
    # usuario que aperta Ctrl+Break em vez de Ctrl+C ve o processo morto sem
    # o VideoWriter ser liberado - e o .mp4 em andamento fica sem header.
    signals = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    for sig in signals:
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass  # nao estamos na thread principal (ex.: testes)

    key = "Enter" if "keyboard" in cfg.triggers else "gatilho"
    print(f" rodando. Aperte {key} para salvar os ultimos {cfg.effective_seconds:g}s. Ctrl+C para sair.\n")

    # ---- espera ativa ----
    try:
        while not stop_event.is_set():
            stop_event.wait(0.25)
            if not capture.is_alive():
                # A thread de captura morreu. Sem este tratamento o processo
                # ficaria parado para sempre, ainda "rodando", sem gravar nada
                # - e o usuario sem nenhuma pista do porque (cabo desconectado,
                # camera ocupada por outro app, driver caiu).
                if capture.stats.warnings:
                    for w in capture.stats.warnings:
                        print(f" {w}")
                else:
                    print(" a captura parou inesperadamente (câmera desconectada, "
                          "ocupada por outro programa ou driver caiu).")
                stop_event.set()
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)

    # ---- teardown ----
    stop_event.set()
    stop_stats.set()
    preview_stop.set()
    for trg in triggers:
        trg.stop(timeout=0.5)
    capture.join(timeout=2.0)
    if stats_thread is not None:
        stats_thread.join(timeout=1.0)
    if preview_thread is not None:
        preview_thread.join(timeout=1.0)
        try:
            cv2.destroyAllWindows()
        except Exception:  # noqa: BLE001
            pass
    if auto_thread is not None:
        auto_thread.join(timeout=0.5)

    # Da tempo ao writer de terminar um save ja em andamento antes de sair,
    # senao um .mp4 fica sem header e nao abre.
    writer.join(timeout=10.0)
    if writer.is_alive():
        print(" writer nao terminou a tempo; o .mp4 em andamento pode estar incompleto.")
    else:
        st = ring.stats()
        print(f" finalizado. capturado={capture.stats.frames} frames, "
              f"buffer={st.count} frames ({_fmt_bytes(st.nbytes)}), "
              f"evictados={st.dropped}, falhas_de_read={capture.stats.failed_reads}")
        print(f" camera: {capture.stats.describe()}")
        for w in capture.stats.warnings:
            print(f" AVISO: {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    cfg = config_from_args(argv)
    if cfg.list_cameras:
        return run_list_cameras(cfg)
    if cfg.self_test:
        return run_self_test(cfg)
    try:
        return run(cfg)
    except KeyboardInterrupt:
        print("\n interrompido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

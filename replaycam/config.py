"""Configuracao e parsing de linha de comando."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import TypeVar

# Ordem de preference de backend. 'auto' = CAP_ANY, que o OpenCV resolve por
# Device (indice) ou por nome.
BACKEND_CHOICES = ("auto", "any", "dshow", "msmf")
TRIGGER_CHOICES = ("keyboard",)
QUALITY_CHOICES = ("standard", "high")


@dataclass(frozen=True, slots=True)
class QualityPreset:
    """Resolucao de captura/buffer e orcamento de RAM de um preset de qualidade.

    O preset nao mexe no codec: no Windows o H.264 do Media Foundation usa um
    bitrate proporcional a `fps * largura * altura`. Subir a resolucao aumenta a
    qualidade e o tamanho do arquivo ao mesmo tempo, sem depender de FFmpeg.
    """

    width: int
    height: int
    buf_width: int
    buf_height: int
    max_buffer_mib: float


# `standard` preserva o comportamento historico do app (buffer 640x480 e 1 GiB
# de orcamento). `high` usa o modo maximo comum das webcams (720p) e um
# orcamento que comporta 30 s nessa resolucao (~2,3 GiB reais).
QUALITY_PRESETS: dict[str, QualityPreset] = {
    "standard": QualityPreset(
        width=0, height=0,
        buf_width=640, buf_height=480,
        max_buffer_mib=1024.0,
    ),
    "high": QualityPreset(
        width=1280, height=720,
        buf_width=1280, buf_height=720,
        max_buffer_mib=2560.0,
    ),
}


@dataclass
class Config:
    """Parametros de execucao. Montada pelo argparse, mas usavel direto em codigo."""

    device: int = 0
    backend: str = "auto"
    quality: str = "standard"
    # 0 = modo nativo do driver. E o default porque o app reduz tudo para
    # `buf_*` de qualquer jeito: pedir 1080p para descartar 3 de cada 4 pixels
    # so gasta banda de USB e CPU. Passe --width/--height para fixar um modo.
    width: int = 0
    height: int = 0
    fps: float = 30.0

    # As duas alavancas de RAM. buf_* e o que fica no buffer circular;
    # width/height e o que a camera entrega. Ver README para a conta exata.
    buf_width: int = 640
    buf_height: int = 480
    seconds: float = 30.0

    outdir: str = "replays"
    export_fps: float | None = None  # None = usa config.fps
    fourcc: str | None = None        # None = cadeia de fallback automatica
    prefix: str = "replay"

    triggers: tuple[str, ...] = ("keyboard",)
    trigger_key: str = "\r"
    debounce: float = 0.35

    preview: bool = True   # preview ligada por padrao: e o modo de uso principal
    list_cameras: bool = False
    self_test: bool = False
    auto_trigger: float = 0.0  # >0 dispara sozinho apos N s (teste sem mao na tecla)
    stats_interval: float = 5.0
    verbose: bool = False       # nao silencia o log nativo do FFmpeg
    # Teto de RAM do slider de tempo retroativo. Acima de ~1,5 GiB o Windows
    # comeca a paginar e a captura perde frames - o defeito silencioso que o
    # ring buffer por tempo existe para evitar.
    max_buffer_mib: float = 1024.0

    extra: dict[str, object] = field(default_factory=dict)

    @property
    def effective_export_fps(self) -> float:
        return self.export_fps if self.export_fps else self.fps

    def estimated_buffer_bytes(self) -> int:
        """RAM maxima do buffer. O pipeline de video e BGR (3 canais)."""
        capacity = max(1, int(round(self.fps * self.seconds)))
        return capacity * self.buf_width * self.buf_height * 3

    def estimated_buffer_mib(self) -> float:
        return self.estimated_buffer_bytes() / (1024 * 1024)

    @property
    def bytes_per_frame(self) -> int:
        """Bytes de UM frame no buffer. O pipeline e BGR (3 canais)."""
        return self.buf_width * self.buf_height * 3

    @property
    def max_frames(self) -> int:
        """Teto de FRAMES que cabem no orcamento — a unidade que a RAM gasta.

        O arredondamento e para BAIXO de proposito. Um teto em segundos e
        fracionario: com 1920x1080, 1024 MiB daria 172,6 frames, e arredondar
        esse numero para frames inteiros (em qualquer direcao) estoura o
        orcamento — 173 frames sao 1026 MiB, 2 MiB a mais do que o usuario
        autorizou. Arredondar para baixo em frames e depois converter para
        segundos garante que o teto nunca e ultrapassado. O teto em segundos
        abaixo deriva daqui, e nao o contrario.
        """
        budget = max(1.0, self.max_buffer_mib) * 1024 * 1024
        per_frame = max(1, self.bytes_per_frame)
        return max(1, min(90_000, int(budget // per_frame)))

    @property
    def max_retro_seconds(self) -> float:
        """Teto do slider de tempo retroativo, dado o orcamento de RAM.

        E o que impede arrastar o slider para 60 s num buffer de 1080p (5,2 GiB).
        O limite e calculado, nao chutado, entao a restricao aparece na propria
        tela: 640x480 vai a ~38 s, 1080p a ~5 s.
        """
        return max(1.0, min(300.0, self.max_frames / max(1.0, self.fps)))

    @property
    def effective_seconds(self) -> float:
        """Janela de tempo REALMENTE usada, ja dentro do orcamento de RAM.

        `--seconds` e o pedido; isto e o que vai acontecer. Sao a mesma coisa
        enquanto o pedido couber no teto, e diferentes quando nao couber: pedir
        30 s com buffer de 1080p (5,2 GiB) comecaria com 5,2 GiBMENOS alocados
        se o ring fosse montado com o valor cru. Todo mundo que precisa do
        numero tem que ler daqui, senao o header, o log e o slider divergem.
        """
        return max(1.0, min(self.seconds, self.max_retro_seconds))

    @property
    def seconds_clamped(self) -> bool:
        """True se `--seconds` foi cortado pelo teto de RAM."""
        return self.seconds > self.max_retro_seconds

    @property
    def effective_capacity(self) -> int:
        """Frames que o ring realmente vai ter, ja com o teto aplicado.

        `min` com `max_frames` em vez de so arredondar: mesmo que
        `effective_seconds` venha de um teto fracionario, o numero de frames
        nunca passa do orcamento.
        """
        return max(1, min(self.max_frames, int(round(self.fps * self.effective_seconds))))

    @property
    def effective_buffer_mib(self) -> float:
        """RAM que o ring realmente vai reservar.

        Diferente de `estimated_buffer_mib`, que responde "o que voce pediu" e
        existe para disparar o aviso de orcamento estourado.
        """
        return self.effective_capacity * self.bytes_per_frame / (1024 * 1024)

    def validate(self) -> list[str]:
        """Retorna uma lista de AVISOS (nao erros): coisas que funcionam, mas vao te morder."""
        warnings: list[str] = []
        mib = self.estimated_buffer_mib()
        # O preset `high` foi escolhido justamente para gastar mais RAM e manter
        # 30 s em 720p. Nao adianta avisar sobre o que o usuario acabou de pedir;
        # o aviso generico volta a aparecer se ele estourar o orcamento do preset
        # (por exemplo, aumentando --seconds ou a resolucao do buffer).
        high_budget = QUALITY_PRESETS["high"].max_buffer_mib
        if mib > 1536 and not (self.quality == "high" and mib <= high_budget):
            warnings.append(
                f"buffer estimado em {mib:.0f} MiB de RAM. Acima de ~1.5 GiB o sistema "
                "comeca a paginar e a captura perde frames. Reduza --buf-width/--buf-height "
                "ou --seconds."
            )
        if self.effective_export_fps != self.fps:
            warnings.append(
                f"--export-fps {self.effective_export_fps} difere do --fps de captura "
                f"{self.fps}: o replay vai sair acelerado ou em camera lenta. "
                "Normalmente ambos devem ser iguais."
            )
        # So compara quando houve resolucao pedida: com `0` (nativo) qualquer
        # buffer pareceria "maior que a captura", que e um aviso falso.
        if self.width > 0 and self.height > 0 and (
            self.buf_width > self.width or self.buf_height > self.height
        ):
            warnings.append(
                f"--buf-*{self.buf_width}x{self.buf_height} e MAIOR que a captura "
                f"{self.width}x{self.height}: sera feito upscale, gastando CPU para "
                "inventar pixels. Normalmente o buffer e menor que a captura."
            )
        if self.seconds > self.max_retro_seconds:
            warnings.append(
                f"--seconds {self.seconds:g} passa do teto de {self.max_retro_seconds:.0f}s "
                f"que o orcamento de {self.max_buffer_mib:g} MiB permite para "
                f"{self.buf_width}x{self.buf_height}. Acima de ~1,5 GiB o sistema comeca "
                f"a paginar e a captura perde frames. Use --max-buffer-mib para assumir "
                f"o risco, ou reduza --seconds / a resolucao do buffer. "
                f"ATENCAO: o buffer vai comecar em {self.effective_seconds:.0f}s mesmo assim."
            )
        if self.fps * self.seconds > 4000:
            warnings.append(
                f"janela de {self.seconds:g}s a {self.fps:g} fps = "
                f"{int(self.fps * self.seconds)} frames no buffer. Confirme que a RAM aguenta."
            )
        return warnings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="replaycam",
        description=(
            "Mantem os ultimos N segundos de video da camera APENAS na RAM e "
            "exporta para .mp4 quando um gatilho dispara."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    dev = p.add_argument_group("dispositivo")
    dev.add_argument("--device", "-d", type=int, default=0, help="indice da camera (0 = primeira)")
    dev.add_argument("--backend", choices=BACKEND_CHOICES, default="auto", help="API de captura do OpenCV")
    # `SUPPRESS` nas opcoes que o preset de qualidade pode definir: assim o
    # `config_from_args` sabe distinguir "nao informado" de um valor explicito,
    # e `--quality high --buf-width 640` respeita o 640 do usuario.
    dev.add_argument("--width", type=int, default=argparse.SUPPRESS,
                     help="largura de captura pedida ao driver (0 = modo nativo; "
                          "preset standard: nativo, preset high: 1280)")
    dev.add_argument("--height", type=int, default=argparse.SUPPRESS,
                     help="altura de captura pedida ao driver (0 = modo nativo; "
                          "preset standard: nativo, preset high: 720)")
    dev.add_argument("--fps", type=float, default=30.0, help="FPS de captura pedido ao driver")

    buf = p.add_argument_group("buffer em RAM")
    buf.add_argument("--buf-width", type=int, default=argparse.SUPPRESS,
                     help="largura dos frames no buffer (RAM; standard: 640, high: 1280)")
    buf.add_argument("--buf-height", type=int, default=argparse.SUPPRESS,
                     help="altura dos frames no buffer (RAM; standard: 480, high: 720)")
    buf.add_argument("--seconds", type=float, default=30.0, help="janela de tempo mantida em RAM")

    out = p.add_argument_group("saida")
    out.add_argument("--quality", choices=QUALITY_CHOICES, default="standard",
                     help="preset de qualidade: standard=640x480/1 GiB, "
                          "high=1280x720/2,5 GiB")
    out.add_argument("--outdir", default="replays", help="diretorio de destino dos .mp4")
    out.add_argument("--export-fps", type=float, default=None, help="FPS do arquivo gravado (default: igual ao --fps)")
    out.add_argument("--fourcc", default=None, help="forca um FourCC (ex: mp4v). Default: fallback automatico")
    out.add_argument("--prefix", default="replay", help="prefixo do nome do arquivo")

    trg = p.add_argument_group("gatilho")
    trg.add_argument("--trigger", default="keyboard", help="gatilhos ativos, separados por virgula")
    trg.add_argument("--trigger-key", default="\\r", help="caractere que dispara (default: Enter)")
    trg.add_argument("--debounce", type=float, default=0.35, help="intervalo minimo entre disparos (s)")

    misc = p.add_argument_group("outros")
    misc.add_argument("--no-preview", dest="preview", action="store_false", default=True,
                      help="fecha a janela de video (por default ela abre, e o modo de uso principal)")
    misc.add_argument("--list-cameras", action="store_true", help="sonda as cameras e sai")
    misc.add_argument("--self-test", action="store_true", help="testa ring buffer + writer sem camera")
    misc.add_argument("--auto-trigger", type=float, default=0.0,
                      help="dispara o gatilho sozinho apos N segundos (0 = off). "
                           "Para testar o caminho de gravacao sem depender do teclado.")
    misc.add_argument("--stats-interval", type=float, default=5.0, help="intervalo do log de status (s, 0 = off)")
    misc.add_argument("--max-buffer-mib", type=float, default=argparse.SUPPRESS,
                      help="teto de RAM do slider de tempo retroativo "
                           "(standard: 1024, high: 2560). Acima de ~1536 MiB o "
                           "sistema pode paginar e a captura perde frames")
    misc.add_argument("--verbose", action="store_true",
                      help="nao silencia o log nativo do FFmpeg (diagnostico de codec)")

    return p


def config_from_args(argv: list[str] | None = None) -> Config:
    ns = build_parser().parse_args(argv)
    # O default do argparse e a SEQUENCIA de dois caracteres backslash+r, e o
    # PowerShell/cmd tambem entrega dois caracteres quando o usuario digita
    # `--trigger-key \r`. Nos dois casos queremos decodificar para o caractere
    # de controle real (CR). `\r` (um caractere) passa direto, sem alteracao.
    key = ns.trigger_key
    if isinstance(key, str) and "\\r" in key:
        key = key.replace("\\r", "\r")

    preset = QUALITY_PRESETS[ns.quality]

    T = TypeVar("T")

    def pick(name: str, fallback: T) -> T:
        """Valor explicito da CLI ou o valor do preset quando a opcao falta."""
        return getattr(ns, name, fallback)

    return Config(
        device=ns.device,
        backend=ns.backend,
        quality=ns.quality,
        width=pick("width", preset.width),
        height=pick("height", preset.height),
        fps=ns.fps,
        buf_width=pick("buf_width", preset.buf_width),
        buf_height=pick("buf_height", preset.buf_height),
        seconds=ns.seconds,
        outdir=ns.outdir,
        export_fps=ns.export_fps,
        fourcc=ns.fourcc,
        prefix=ns.prefix,
        triggers=tuple(t.strip() for t in ns.trigger.split(",") if t.strip()),
        trigger_key=key,
        debounce=ns.debounce,
        preview=ns.preview,
        list_cameras=ns.list_cameras,
        self_test=ns.self_test,
        auto_trigger=ns.auto_trigger,
        stats_interval=ns.stats_interval,
        verbose=ns.verbose,
        max_buffer_mib=pick("max_buffer_mib", preset.max_buffer_mib),
    )

"""Exportacao do snapshot do buffer para .mp4 em disco.

Este modulo detem a unica `cv2.VideoWriter` do processo. `VideoWriter` NAO e
thread-safe, e a thread de escrita e a unica a instancia-la, o que elimina a
classe inteira de bugs de corrida na escrita.

## O que foi APRENDIDO testando codecs no Windows (leia antes de mexer aqui)

Medicoes reais nesta maquina (opencv-python 4.14, sem FFmpeg no PATH):

    pedido     tamanho    fourcc lido de volta
    avc1       25825 B    h264
    H264       25825 B    h264
    X264       25825 B    h264
    ZZZZZ      25825 B    h264     <-- lixo Accepted!
    mp4v       45419 B    FMP4

Duas conclusoes que mudaram o design deste modulo:

1. **Um FourCC invalido NAO falha.** O backend FFMPEG loga "tag not found" e
   cai silenciosamente no codec padrao do container; so que o OpenCV sem FFmpeg
   proprio passa para o backend MSMF, que encode H.264 de verdade. Resultado:
   `ZZZZ` gera um arquivo byte-a-byte identico ao `avc1`. Nao existe nenhuma
   forma de distinguir "pedi avc1 e recebi h264" de "pedi lixo e recebi h264"
   olhando o arquivo de saida. Por isso `_select_fourcc` NAO valida a tag: ela
   pede, e depois valida o que realmente importa - que o arquivo decodifica,
   tem a contagem de frames certa e a duracao certa.

2. **H.264 depende do Media Foundation, nao do OpenH264.** O `opencv-python`
   nao embarca o openh264 DLL, entao o H.264 aqui vem do encoder nativo do
   Windows 10+. Em maquinas sem MSMF (ou em build sem esse backend) o `avc1`
   falha de vez e o `mp4v` e a saida. Por isso a cadeia de fallback existe.

O que a validacao FAZ, e o que ela nao faz:

    FAZ:     rejeita codec que abre e nao escreve (arquivo < 1 KiB);
             rejeita codec que escreve mas produz arquivo ilegiveis;
             rejeita codec que perde frames ou grava duracao errada;
             reporta na log qual codec o arquivo REALMENTE tem.
    NAO FAZ: nao consegue detectar que voce pediu uma tag sem sentido.

O `CodecError` só e levantado quando TODA a cadeia falha essa validacao.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2

# Ordem de preferencia: H.264 primeiro (melhor compressao e o mais aceito por
# players/celulares), mp4v como ultimo recurso garantido.
DEFAULT_FOURCC_CHAIN: tuple[str, ...] = ("avc1", "H264", "X264", "mp4v")

# Um probe que grava um frame de 640x480 precisa passar deste tamanho. Abaixo
# disso o container foi criado mas nao recebeu dados.
MIN_VALID_BYTES = 1024

# ## Reduzindo o ruido do fallback H.264
#
# O `opencv-python` nao embarca a DLL do OpenH264, entao o FFMPEG falha e o
# OpenCV cai no Media Foundation, que encode H.264 de verdade. Medido aqui, um
# `VideoWriter` avc1:
#
#     configuracao                                  linhas de ruido
#     nenhuma                                        8
#     FFMPEG_LOGLEVEL=QUIET + LOG_LEVEL=SILENT       2
#     FFMPEG_SKIP_LOG_CALLBACK=1                     8   (nao faz nada)
#
# IMPORTANTE: as duas variaveis so funcionam se estiverem no ambiente QUANDO O
# PROCESSO NASCE. Definir `os.environ[...]` de dentro do Python nao tem efeito
# nenhum - medido: 8 linhas tanto antes quanto depois do `import cv2` - porque a
# biblioteca nativa le o ambiente no carregamento, e nao a cada chamada. Por isso
# quem faz isso e o launcher `replaycam.cmd`, e nao este modulo. daqui para
# frente `silence_expected_ffmpeg_h264_fallback` so manipula o ambiente de
# PROCESSOS FILHOS, o que e o que ela consegue fazer de verdade.
#
# As 2 linhas restantes ("Failed to load OpenH264 library" e a URL do GitHub)
# sao do carregador da libopenh264, que escreve no stderr antes de qualquer
# nivel de log passar a valer. Nao ha configuracao que as remova: a unica forma
# e baixar a DLL.
FFMPEG_LOG_ENVS = ("OPENCV_FFMPEG_LOGLEVEL", "OPENCV_LOG_LEVEL")
FFMPEG_LOG_VALUES = {"OPENCV_FFMPEG_LOGLEVEL": "QUIET", "OPENCV_LOG_LEVEL": "SILENT"}


def silence_expected_ffmpeg_h264_fallback(verbose: bool = False) -> bool:
    """Baixa o log do OpenCV/FFmpeg para os PROCESSOS FILHOS desta chamada.

    Nao afeta o proprio processo: ver a nota do modulo sobre env por processo.
    Existe para que um processo filho aberto pelo app nao speque o mesmo ruido.
    Devolve `True` se chegou a mudar algo.
    """
    if verbose:
        return False
    changed = False
    for name in FFMPEG_LOG_ENVS:
        if not os.environ.get(name):
            os.environ[name] = FFMPEG_LOG_VALUES[name]
            changed = True
    return changed


def native_ffmpeg_hint() -> str:
    """Instrucao de como ver o log nativo, para um erro de codec de verdade."""
    return (
        "Para ver o erro nativo do FFmpeg, rode sem o launcher "
        "(replaycam.cmd), que e quem define essas variaveis."
    )


class CodecError(RuntimeError):
    """Nenhum FourCC da cadeia produziu um arquivo que decodifica."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Resultado de uma gravacao, para logging e para a fila de disparo."""

    ok: bool
    path: Path | None
    frames: int
    fps: float
    size_bytes: int
    requested_fourcc: str | None
    actual_fourcc: str | None
    duration_seconds: float
    elapsed_seconds: float
    error: str | None = None


def build_filename(when: datetime | None = None, prefix: str = "replay") -> str:
    """`replay_YYYYMMDD_HHMMSS.mp4`, com sufixo se ja existir arquivo igual."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}.mp4"


def unique_path(outdir: Path, when: datetime | None = None, prefix: str = "replay") -> Path:
    """Resolve colisao de nome sem sobrescrever um replay ja gravado."""
    base = build_filename(when, prefix)
    candidate = outdir / base
    if not candidate.exists():
        return candidate
    stem = base[:-4]  # remove ".mp4"
    for n in range(1, 1000):
        candidate = outdir / f"{stem}_{n}.mp4"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"nao achei nome livre para {base} em {outdir}")


def read_fourcc(path: Path) -> str:
    """Codec REALMENTE gravado no arquivo (FourCC lido de volta)."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return ""
        return int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("latin-1", "replace")
    finally:
        cap.release()


def _decodes_cleanly(path: Path, expected_frames: int, expected_fps: float) -> tuple[bool, str]:
    """Confere se o arquivo gravado e realmente utilizavel.

    Checar `isOpened()` na escrita nao basta: no Windows o Media Foundation
    aceita abrir um writer e produzir arquivo invalido. A unica verificacao que
    importa e abrir o arquivo de volta e decodificar.
    """
    if not path.exists() or path.stat().st_size < MIN_VALID_BYTES:
        size = path.stat().st_size if path.exists() else 0
        return False, f"arquivo com {size} bytes (min {MIN_VALID_BYTES})"

    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return False, "arquivo nao pode ser reaberto"
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        ok, frame = cap.read()
    finally:
        cap.release()

    if not ok or frame is None:
        return False, "primeiro frame nao decodifica"
    if frames + 0.5 < expected_frames:
        return False, f"somente {int(frames)} de {expected_frames} frames no arquivo"
    if expected_fps > 0 and fps > 0 and abs(fps - expected_fps) / expected_fps > 0.02:
        return False, f"fps gravado {fps:.2f} != {expected_fps:.2f} pedido"
    return True, ""


class VideoExporter:
    """Grava listas de frames em arquivos .mp4 independentes."""

    def __init__(
        self,
        outdir: str | os.PathLike[str] = "replays",
        fourcc_chain: tuple[str, ...] = DEFAULT_FOURCC_CHAIN,
        prefix: str = "replay",
    ) -> None:
        self.outdir = Path(outdir)
        self.fourcc_chain = fourcc_chain
        self.prefix = prefix
        self._selected: str | None = None

    def ensure_outdir(self) -> Path:
        self.outdir.mkdir(parents=True, exist_ok=True)
        return self.outdir

    def _select_fourcc(self, frame: object, size: tuple[int, int], fps: float) -> str:
        """Escolhe um FourCC testando se o arquivo de verdade decodifica.

        A sondagem grava UM frame num arquivo temporario, fecha o writer e
        reabre o arquivo para conferir que decodifica e tem os frames certos.
        Custo: um encode + uma reabertura, so na primeira gravacao do processo.
        Vantagem: nunca entrega um .mp4 silenciosamente corrompido.

        Ver o docstring do modulo sobre por que a tag em si nao e validada.
        """
        if self._selected is not None:
            return self._selected

        height, width = size
        tried: list[str] = []
        reasons: list[str] = []
        for tag in self.fourcc_chain:
            tried.append(tag)
            probe = self.outdir / f".probe_{tag}.mp4"
            writer = cv2.VideoWriter(str(probe), cv2.VideoWriter_fourcc(*tag), fps, size)
            opened = writer.isOpened()
            if opened:
                writer.write(frame)
            writer.release()  # conserta o header; sem isso o arquivo fica sem indice

            ok, why = _decodes_cleanly(probe, expected_frames=1, expected_fps=fps)
            if probe.exists():
                probe.unlink(missing_ok=True)
            if ok:
                self._selected = tag
                return tag
            reasons.append(f"{tag}: {'nao abriu o writer' if not opened else why}")

        # Inclui o motivo de cada candidato: sem isso o usuario ve "codec falhou"
        # e nao tem como saber se o problema e FFmpeg ausente, permissao de
        # arquivo ou codec genuinamente indisponivel. Este e o caminho que
        # garante que a falha real continue audivel mesmo com o log do FFmpeg
        # silenciado: aqui o diagnostico vem do OpenCV, nao do stderr do C.
        detail = "; ".join(reasons) or "sem candidatos"
        raise CodecError(
            f"nenhum FourCC da cadeia gerou um arquivo que decodifica "
            f"(testados: {', '.join(tried)}). Detalhe: {detail}. "
            f"Tente forcar --fourcc mp4v, ou instale o FFmpeg e "
            f"garanta que o OpenCV o encontre. {native_ffmpeg_hint()}"
        )

    def export(self, frames: list[object], fps: float, when: datetime | None = None) -> ExportResult:
        """Grava `frames` em um .mp4. Nunca lanca: devolve ExportResult com ok=False.

        Os frames ja vem downscaled do buffer, entao o tamanho do video e
        deduzido do primeiro frame. `fps` deve ser o FPS DE EXPORTACAO, que
        precisa bater com o FPS de captura ou o replay sai acelerado/lento.
        """
        started = time.perf_counter()
        if not frames:
            return ExportResult(
                ok=False, path=None, frames=0, fps=fps, size_bytes=0,
                requested_fourcc=None, actual_fourcc=None, duration_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started,
                error="buffer vazio: nenhum frame capturado ainda",
            )

        self.ensure_outdir()
        first = frames[0]
        height, width = first.shape[:2]
        # VideoWriter exige dimensoes PARES em varios codecs/caminhos do Media
        # Foundation. Cortar de 1 em 1 e mais barato e seguro que redimensionar.
        width -= width % 2
        height -= height % 2
        size = (width, height)

        def fail(error: str, **kw: object) -> ExportResult:
            return ExportResult(
                ok=False, path=None, frames=len(frames), fps=fps, size_bytes=0,
                actual_fourcc=None, duration_seconds=0.0,
                elapsed_seconds=time.perf_counter() - started, error=error, **kw,  # type: ignore[arg-type]
            )

        try:
            tag = self._select_fourcc(first, size, fps)
        except CodecError as exc:
            return fail(str(exc), requested_fourcc=None)

        path = unique_path(self.outdir, when, self.prefix)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*tag), fps, size)
        if not writer.isOpened():
            writer.release()
            return fail("VideoWriter recusou abrir o arquivo no segundo passo", requested_fourcc=tag)

        written = 0
        try:
            for frame in frames:
                # Ultimo recorte de seguranca: se o driver devolveu dimensao
                # inesperada, corta em vez de escrever fora do tamanho pedido.
                if frame.shape[0] != height or frame.shape[1] != width:
                    frame = frame[:height, :width]
                writer.write(frame)
                written += 1
        finally:
            writer.release()

        ok, why = _decodes_cleanly(path, expected_frames=written, expected_fps=fps)
        if not ok:
            # O arquivo e descartado: um .mp4 invalido no disco e pior que
            # nenhum, porque o usuario so descobriria o problema ao abrir.
            path.unlink(missing_ok=True)
            return fail(f"gravacao invalida ({why}), arquivo descartado", requested_fourcc=tag)

        size_bytes = path.stat().st_size
        return ExportResult(
            ok=True, path=path, frames=written, fps=fps, size_bytes=size_bytes,
            requested_fourcc=tag, actual_fourcc=read_fourcc(path),
            duration_seconds=written / fps if fps else 0.0,
            elapsed_seconds=time.perf_counter() - started,
        )

    def probe_duration(self, path: Path) -> float:
        """Duracao de um .mp4 ja gravado, lida do proprio arquivo. Usado no self-test."""
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened():
                return 0.0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                return 0.0
            return frames / fps
        finally:
            cap.release()

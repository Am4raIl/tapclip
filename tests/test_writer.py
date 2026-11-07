"""Testes do VideoExporter. Gravam em disco de verdade, em tmp_path."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from replaycam.writer import (
    DEFAULT_FOURCC_CHAIN,
    MIN_VALID_BYTES,
    CodecError,
    VideoExporter,
    build_filename,
    unique_path,
)

SIZE = (320, 240)  # (largura, altura)

# Descobre, uma vez, se esta plataforma aceita silenciosamente um FourCC lixo.
# O resultado muda o que os testes de fallback podem afirmar, entao e medido em
# vez de presumido - ver o docstring de replaycam.writer para os numeros.
_PROBE_DIR = Path(tempfile.mkdtemp(prefix="replaycam-probe-"))
atexit.register(shutil.rmtree, _PROBE_DIR, True)
_BOGUS_ACCEPTED = VideoExporter(outdir=_PROBE_DIR, fourcc_chain=("ZZZZ",)).export(
    [np.full((240, 320, 3), i % 255, np.uint8) for i in range(20)], 30.0
).ok


def _frames(n: int = 45, size: tuple[int, int] = SIZE) -> list[np.ndarray]:
    """Frames com estrutura, nao ruido: JPEG/H.264 comprimem ruido muito bem."""
    out = []
    for i in range(n):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        frame[:, (i * 5) % size[0]] = 255
        frame[:, :, 1] = i % 255
        out.append(frame)
    return out


def test_filename_has_timestamp(tmp_path) -> None:
    name = build_filename()
    assert name.startswith("replay_")
    assert name.endswith(".mp4")
    # replay_YYYYMMDD_HHMMSS.mp4
    stem = name[len("replay_"):-len(".mp4")]
    assert len(stem) == 15  # 8 + 1 + 6


def test_unique_path_does_not_clobber(tmp_path) -> None:
    a = unique_path(tmp_path)
    a.write_bytes(b"x")  # apenas para ocupar o nome
    b = unique_path(tmp_path)
    assert a != b
    assert b.name.endswith("_1.mp4")


def test_export_creates_playable_file_with_right_duration(tmp_path) -> None:
    exporter = VideoExporter(outdir=tmp_path)
    frames = _frames(45)
    fps = 30.0

    result = exporter.export(frames, fps)

    assert result.ok, result.error
    assert result.path is not None and result.path.exists()
    assert result.frames == 45
    assert result.size_bytes >= MIN_VALID_BYTES
    assert result.requested_fourcc in DEFAULT_FOURCC_CHAIN
    assert result.actual_fourcc, "o codec real do arquivo deve ser reportado"
    assert result.duration_seconds == pytest.approx(1.5, abs=0.01)
    assert result.bitrate_mbps > 0

    # Confirma lendo o arquivo de volta, e nao so o que devolvemos.
    read_back = exporter.probe_duration(result.path)
    assert read_back == pytest.approx(1.5, abs=0.2)


def test_first_valid_codec_in_chain_wins(tmp_path) -> None:
    """A cadeia escolhe o primeiro que produz arquivo decodificavel."""
    exporter = VideoExporter(outdir=tmp_path, fourcc_chain=("mp4v", "avc1"))
    result = exporter.export(_frames(20), 30.0)
    assert result.ok, result.error
    assert result.requested_fourcc == "mp4v"


def test_fourcc_is_remembered_between_exports(tmp_path) -> None:
    """A sondagem so acontece na primeira gravacao do processo."""
    exporter = VideoExporter(outdir=tmp_path)
    first = exporter.export(_frames(15), 30.0)
    assert first.ok, first.error
    assert exporter._selected == first.requested_fourcc
    second = exporter.export(_frames(15), 30.0)
    assert second.ok, second.error
    assert second.requested_fourcc == first.requested_fourcc
    # E nenhum arquivo de sonda sobrou no diretorio.
    assert list(tmp_path.glob(".probe_*")) == []


def test_chain_advances_when_a_codec_cannot_open(tmp_path, monkeypatch) -> None:
    """Um codec que nao abre faz a cadeia passar para o proximo.

    Injetamos a falha em vez de confiar em uma tag invalida: nesta plataforma
    um FourCC lixo NAO falha (o FFMPEG cai no codec padrao e o MSMF encode
    H.264), entao testar com "ZZZZ" nao provaria nada sobre a logica de
    fallback. Ver `test_bogus_fourcc_silently_succeeds_on_this_platform`.
    """
    real_writer = cv2.VideoWriter
    asked: list[str] = []

    class RefusingWriter:
        def __init__(self, path, fourcc, fps, size):
            tag = int(fourcc).to_bytes(4, "little").decode("latin-1", "replace")
            asked.append(tag)
            self._inner = real_writer(path, fourcc, fps, size)
            if tag == "BAD!":  # so este falha
                self._inner.release()
                self._inner = None

        def isOpened(self) -> bool:  # noqa: N802 (nome da API do OpenCV)
            return self._inner is not None and self._inner.isOpened()

        def write(self, frame) -> None:
            if self._inner is not None:
                self._inner.write(frame)

        def release(self) -> None:
            if self._inner is not None:
                self._inner.release()

    monkeypatch.setattr("replaycam.writer.cv2.VideoWriter", RefusingWriter)
    exporter = VideoExporter(outdir=tmp_path, fourcc_chain=("BAD!", "mp4v"))
    result = exporter.export(_frames(20), 30.0)

    assert result.ok, result.error
    assert result.requested_fourcc == "mp4v"
    assert asked[:2] == ["BAD!", "mp4v"]


def test_all_codecs_failing_raises_codec_error(tmp_path, monkeypatch) -> None:
    class DeadWriter:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def isOpened(self) -> bool:  # noqa: N802
            return False

        def write(self, _frame) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr("replaycam.writer.cv2.VideoWriter", DeadWriter)

    # `_select_fourcc` levanta...
    exporter = VideoExporter(outdir=tmp_path, fourcc_chain=("ZZZZ", "YYYY"))
    frame = _frames(1)[0]
    with pytest.raises(CodecError) as exc:
        exporter._select_fourcc(frame, (frame.shape[1], frame.shape[0]), 30.0)
    message = str(exc.value)
    assert "mp4v" in message          # mensagem sugere o caminho de escape
    assert "nao abriu o writer" in message  # e diz o motivo de cada candidato

    # ...mas `export()` nunca levanta: converte em ExportResult com ok=False.
    res = exporter.export(_frames(5), 30.0)
    assert not res.ok and res.error
    assert "decodifica" in res.error


def test_codec_that_writes_but_does_not_decode_is_rejected(tmp_path, monkeypatch) -> None:
    """O caso que motivou a validacao: `isOpened()` True e arquivo lixo.

    Este e o modo de falha real no Windows (Media Foundation aceitando abrir o
    writer e produzindo um container sem dados), e nao um FourCC inventado.
    """
    class EmptyWriter:
        """Abre com sucesso, mas nao escreve nada - simula o Media Foundation que aceitou."""

        def __init__(self, *_a, **_kw) -> None:
            pass

        def isOpened(self) -> bool:  # noqa: N802
            return True

        def write(self, _frame) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr("replaycam.writer.cv2.VideoWriter", EmptyWriter)
    res = VideoExporter(outdir=tmp_path, fourcc_chain=("avc1",)).export(_frames(10), 30.0)
    assert not res.ok
    assert res.error and "bytes" in res.error
    # E nada de arquivo invalido sobrado em disco.
    assert list(tmp_path.glob("*.mp4")) == []


@pytest.mark.skipif(
    not _BOGUS_ACCEPTED, reason="nesta plataforma um FourCC invalido e aceito silenciosamente"
)
def test_bogus_fourcc_silently_succeeds_on_this_platform(tmp_path) -> None:
    """Documenta um comportamento real, nao um bug do codigo.

    Medido nesta maquina: `ZZZZ` gera arquivo byte-a-byte identico ao `avc1`
    (25825 B, lido de volta como h264). Nao ha como detectar pela saida que a
    tag pedida era sem sentido - o `_select_fourcc` pede e valida o resultado.
    """
    res = VideoExporter(outdir=tmp_path, fourcc_chain=("ZZZZ",)).export(_frames(20), 30.0)
    assert res.ok, res.error
    assert res.requested_fourcc == "ZZZZ"
    assert res.actual_fourcc == "h264"


def test_empty_buffer_is_reported_not_raised(tmp_path) -> None:
    res = VideoExporter(outdir=tmp_path).export([], 30.0)
    assert not res.ok
    assert "vazio" in res.error
    assert res.path is None


def test_odd_dimensions_are_made_even(tmp_path) -> None:
    """Dimensoes impares sao ajustadas para par, senao varios codecs recusam."""
    exporter = VideoExporter(outdir=tmp_path)
    frames = _frames(10, size=(321, 241))
    result = exporter.export(frames, 30.0)
    assert result.ok, result.error


def test_successive_exports_do_not_overwrite(tmp_path) -> None:
    exporter = VideoExporter(outdir=tmp_path)
    first = exporter.export(_frames(10), 30.0)
    second = exporter.export(_frames(10), 30.0)
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


def test_exporter_creates_missing_outdir(tmp_path) -> None:
    target = tmp_path / "deep" / "replays"
    result = VideoExporter(outdir=target).export(_frames(10), 30.0)
    assert result.ok, result.error
    assert result.path.parent == target

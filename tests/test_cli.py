"""Testes de CLI: --self-test, --list-cameras, export ponta a ponta."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from replaycam.cli import main
from replaycam.config import config_from_args
from replaycam.writer import VideoExporter

import numpy as np


def test_self_test_passes(tmp_path, capsys) -> None:
    """O caminho mais importante: ring buffer + concorrencia + writer, sem camera."""
    rc = main(["--self-test", "--outdir", str(tmp_path), "--buf-width", "160", "--buf-height", "120"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[self-test] OK" in out
    assert "janela temporal" in out
    assert "concorrencia" in out
    assert "duracao lida de volta" in out
    # O self-test nao deve deixar lixo no disco.
    assert list(Path(tmp_path).glob("*.mp4")) == []


def test_self_test_reports_failure_instead_of_crashing(tmp_path, capsys) -> None:
    rc = main(["--self-test", "--fourcc", "ZZZZ", "--outdir", str(tmp_path),
               "--buf-width", "160", "--buf-height", "120"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FALHOU" in out
    assert "ZZZZ" in out


def test_list_cameras_does_not_crash(tmp_path, capsys) -> None:
    """Pode nao achar camera nenhuma, mas nao pode estourar."""
    rc = main(["--list-cameras"])
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "sondando cameras" in out


def test_export_fps_override_warns() -> None:
    warnings = config_from_args(["--export-fps", "60"]).validate()
    assert any("export-fps" in w for w in warnings)


def test_end_to_end_snapshot_to_mp4(tmp_path) -> None:
    """Simula o caminho real: frames no buffer -> snapshot -> arquivo.

    E a mesma sequencia que a thread writer executa, entao valida o contrato
    entre ring buffer e writer sem precisar de camera nem de threads.
    """
    from replaycam.ringbuffer import RingBuffer

    ring = RingBuffer.for_duration(fps=30.0, seconds=2.0)
    t0 = 1000.0
    for i in range(60):  # 2 s a 30 fps
        frame = np.zeros((120, 160, 3), np.uint8)
        frame[:, (i * 3) % 160] = 255
        ring.push(frame, t0 + i / 30.0)

    result = VideoExporter(outdir=tmp_path).export(ring.snapshot(), 30.0)
    assert result.ok, result.error
    assert result.frames == len(ring)
    assert result.duration_seconds == pytest.approx(2.0, abs=0.05)
    assert result.path.stat().st_size > 1024


# --------------------------------------------------- sondagem de camera (com fake)


def test_list_cameras_stops_after_consecutive_misses() -> None:
    """Duas falhas seguidas encerram a sondagem.

    Sem isso, `--list-cameras` probe 6 indices e cospe 6 blocos de erro do
    FFMPEG para indices que nunca existiram, gastando ~3 s.
    """
    from replaycam.capture import list_cameras

    tried: list[int] = []

    def fake(idx: int, _api: int):
        tried.append(idx)
        return None  # camera alguma

    found = list_cameras(indices=range(0, 6), probe=fake)
    assert found == []
    assert tried == [0, 1]  # parou no segundo miss, nao foi ate o 5


def test_list_cameras_resets_the_miss_counter_on_a_hit() -> None:
    """Layout esparso (cameras em 0 e 2) precisa ser descoberto inteiro."""
    from replaycam.capture import list_cameras

    def fake(idx: int, _api: int):
        return {"index": idx, "width": 1280, "height": 720, "declared_width": 1280,
                "declared_height": 720, "fps": 30.0, "fourcc": "h264",
                "frame_size": (1280, 720)} if idx in (0, 2) else None

    found = list_cameras(indices=range(0, 6), probe=fake)
    assert [c["index"] for c in found] == [0, 2]


def test_list_cameras_labels_the_requested_mode() -> None:
    from replaycam.capture import list_cameras

    def fake(idx: int, _api: int):
        return {"index": idx, "width": 1280, "height": 720, "declared_width": 1280,
                "declared_height": 720, "fps": 30.0, "fourcc": "h264",
                "frame_size": (1280, 720)}

    native = list_cameras(probe=fake)
    assert native[0]["requested"] == "nativo"
    fixo = list_cameras(width=1920, height=1080, probe=fake)
    assert fixo[0]["requested"] == "1920x1080"


# ------------------------------------------------------- log nativo do FFmpeg


def test_ffmpeg_env_only_reaches_child_processes(monkeypatch) -> None:
    """O ruido do FFMPEG so cede a variavel posta no ambiente do processo.

    Medido: com as vars no ambiente antes de o processo nascer, o ruido cai de 8
    para 2 linhas. Definir `os.environ` de dentro do Python nao tem efeito no
    proprio processo (a lib nativa le o ambiente no carregamento), entao quem
    faz o trabalho e o launcher `replaycam.cmd` - e esta funcao so afeta
    filhos. O teste existe para ninguem inventar de que ela silencia o app.
    """
    from replaycam.writer import (
        FFMPEG_LOG_VALUES,
        silence_expected_ffmpeg_h264_fallback,
    )

    monkeypatch.delenv("OPENCV_FFMPEG_LOGLEVEL", raising=False)
    monkeypatch.delenv("OPENCV_LOG_LEVEL", raising=False)
    assert silence_expected_ffmpeg_h264_fallback() is True
    assert os.environ["OPENCV_FFMPEG_LOGLEVEL"] == FFMPEG_LOG_VALUES["OPENCV_FFMPEG_LOGLEVEL"]
    assert os.environ["OPENCV_LOG_LEVEL"] == FFMPEG_LOG_VALUES["OPENCV_LOG_LEVEL"]


def test_ffmpeg_silencing_never_overrides_the_user(monkeypatch) -> None:
    from replaycam.writer import silence_expected_ffmpeg_h264_fallback

    monkeypatch.setenv("OPENCV_FFMPEG_LOGLEVEL", "debug")
    monkeypatch.delenv("OPENCV_LOG_LEVEL", raising=False)
    silence_expected_ffmpeg_h264_fallback()
    assert os.environ["OPENCV_FFMPEG_LOGLEVEL"] == "debug"


def test_verbose_leaves_the_log_alone(monkeypatch) -> None:
    from replaycam.writer import silence_expected_ffmpeg_h264_fallback

    monkeypatch.delenv("OPENCV_FFMPEG_LOGLEVEL", raising=False)
    monkeypatch.delenv("OPENCV_LOG_LEVEL", raising=False)
    assert silence_expected_ffmpeg_h264_fallback(verbose=True) is False
    assert "OPENCV_FFMPEG_LOGLEVEL" not in os.environ


def test_launcher_sets_the_env_that_actually_works() -> None:
    """O `.cmd` e a peca que funciona; o codigo Python nao consegue.

    Se alguem apagar o launcher ou trocar os nomes das variaveis, a reducao de
    8 para 2 linhas de ruido deixa de acontecer em silencio. O teste le o
    arquivo e cobra que as duas variaveis estejam la.
    """
    from replaycam.writer import FFMPEG_LOG_ENVS, FFMPEG_LOG_VALUES

    launcher = Path(__file__).resolve().parent.parent / "replaycam.cmd"
    assert launcher.exists(), "replaycam.cmd sumiu: o ruido do FFmpeg volta a 8 linhas"
    body = launcher.read_text(encoding="utf-8")
    for name in FFMPEG_LOG_ENVS:
        assert name in body, f"{name} ausente do launcher"
    assert FFMPEG_LOG_VALUES["OPENCV_FFMPEG_LOGLEVEL"] in body
    assert FFMPEG_LOG_VALUES["OPENCV_LOG_LEVEL"] in body


def test_codec_error_points_at_the_way_to_see_the_native_log() -> None:
    """Falha real de codec precisa dizer como ver o log nativo."""
    from replaycam.writer import native_ffmpeg_hint

    hint = native_ffmpeg_hint()
    assert "replaycam.cmd" in hint

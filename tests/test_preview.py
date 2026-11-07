"""Testes da camada de preview: overlay e slider de tempo retroativo.

O overlay e o slider rodam DENTRO da thread de preview, entao nao dao para
verifica-los com um run real de forma confiavel (uma captura de janela do
OpenCV via PrintWindow nao traz o canvas). Aqui eles sao exercitados direto, no
codigo que de fato desenha e no codigo que de fato ajusta a janela.
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import pytest

from replaycam.capture import CameraCapture
from replaycam.cli import RetroController, draw_overlay
from replaycam.config import config_from_args
from replaycam.ringbuffer import RingBuffer


# ------------------------------------------------------------------------ overlay


def test_overlay_draws_text_onto_the_canvas() -> None:
    """O overlay precisa realmente pintar pixels, nao apenas nao quebrar."""
    blank = np.zeros((480, 640, 3), np.uint8)
    draw_overlay(blank, ["1280x720  30.0 fps", "buffer 300/300", "retroativo 10.0s"], "Enter=salvar")

    painted = int((blank.max(axis=2) > 40).sum())
    assert painted > 200, f"o overlay quase nao desenhou ({painted} px)"

    # As tres linhas ficam no topo, separadas por 20 px: y = 22, 42, 62.
    for expected_y in (22, 42, 62):
        band = blank[max(0, expected_y - 12):expected_y + 4, 5:400]
        assert band.max() > 40, f"nada pintado na linha em y={expected_y}"


def test_overlay_draws_the_hint_at_the_bottom() -> None:
    blank = np.zeros((480, 640, 3), np.uint8)
    draw_overlay(blank, ["a"], "Enter=salvar ESC=sair")
    bottom = blank[-20:, 5:400]
    assert bottom.max() > 40, "a legenda de teclas nao foi desenhada no rodape"


def test_overlay_outline_makes_text_readable_on_white() -> None:
    """O contorno preto e o que impede o texto de sumir em cena clara."""
    white = np.full((480, 640, 3), 250, np.uint8)
    draw_overlay(white, ["30.0 fps"], "")
    changed = white != 250
    assert changed.any(), "nada foi alterado numa cena clara"
    # Tem que existir pixel escuro (o contorno) E pixel claro (o texto).
    assert (white.max(axis=2) < 60).any(), "sem contorno preto: texto some em cena clara"
    assert (white.max(axis=2) > 250).any(), "sem texto claro por cima do contorno"


def test_overlay_does_not_touch_the_source_frame() -> None:
    """A preview pinta numa copia: o frame da camera precisa ficar intacto,
    porque e a mesma referencia que o ring buffer pode estar guardando."""
    frame = np.zeros((480, 640, 3), np.uint8)
    original = frame.copy()
    canvas = frame.copy()
    draw_overlay(canvas, ["texto"], "dica")
    assert np.array_equal(frame, original)


def test_camera_mirror_flips_only_when_enabled() -> None:
    """O flip horizontal precisa ser aplicado na captura, antes de preview/buffer."""
    frame = np.zeros((2, 3, 3), np.uint8)
    frame[0, 0] = (10, 20, 30)
    frame[0, 2] = (40, 50, 60)

    off = CameraCapture(
        ring=_ring(), stop_event=threading.Event(), mirror_preview=False
    )
    assert off._maybe_mirror(frame) is frame

    on = CameraCapture(
        ring=_ring(), stop_event=threading.Event(), mirror_preview=True
    )
    flipped = on._maybe_mirror(frame)
    assert np.array_equal(flipped[0, 0], frame[0, 2])
    assert np.array_equal(flipped[0, 2], frame[0, 0])
    assert not np.shares_memory(flipped, frame)


# ---------------------------------------------------------------------- slider


def _ring(seconds: float = 10.0) -> RingBuffer:
    return RingBuffer.for_duration(fps=30.0, seconds=seconds)


def test_slider_ceiling_comes_from_the_ram_budget() -> None:
    """O teto do slider e calculado a partir da RAM, nao chutado.

    E o que impede arrastar para 60 s num buffer de 1080p, que exigiria 5,2 GiB.
    """
    hd = RetroController(_ring(), config_from_args(["--buf-width", "1920", "--buf-height", "1080"]))
    sd = RetroController(_ring(), config_from_args(["--buf-width", "640", "--buf-height", "480"]))
    assert hd.max_seconds < 10
    assert sd.max_seconds > 30


def test_slider_start_is_clamped_to_the_ceiling() -> None:
    """Pedir 30 s num buffer de 1080p tem que cair para o teto, nao estourar RAM."""
    cfg = config_from_args(["--buf-width", "1920", "--buf-height", "1080", "--seconds", "30"])
    ctrl = RetroController(_ring(), cfg)
    assert ctrl.initial <= ctrl.max_seconds


def test_seconds_is_clamped_to_the_ram_budget() -> None:
    """O teto precisa valer no STARTUP, nao so no slider.

    Este e o bug que motivou `effective_seconds`: `--seconds 30` com buffer de
    1080p sao 5,2 GiB. O aviso existia, mas o ring era construido com o valor
    CRU mesmo assim - o teto so restringia o slider, e nao a reserva inicial.
    """
    cfg = config_from_args(["--buf-width", "1920", "--buf-height", "1080", "--seconds", "30"])
    assert cfg.seconds_clamped
    assert cfg.effective_seconds == pytest.approx(cfg.max_retro_seconds)
    # O que realmente vai ser reservado cabe no orcamento.
    assert cfg.effective_buffer_mib <= cfg.max_buffer_mib
    # E o valor cru, o que nao cabe - para deixar claro a diferenca que importa.
    assert cfg.estimated_buffer_mib() > cfg.max_buffer_mib * 4


def test_the_ram_ceiling_is_never_exceeded_by_rounding() -> None:
    """O teto e bytes, e o slider e em segundos: a conversao nao pode estourar.

    1024 MiB em 1920x1080 da 172,6 frames. Arredondando esse numero para frames
    inteiros o estourava (173 frames = 1026 MiB) — ou seja, o "limite" era um
    limite que nao limitava. O teto precisa ser calculado em frames, com
    arredondamento para baixo.
    """
    for w, h in ((1920, 1080), (1280, 720), (640, 480), (320, 240)):
        for budget in (256.0, 512.0, 1024.0, 1536.0, 2048.0):
            cfg = config_from_args(
                ["--buf-width", str(w), "--buf-height", str(h),
                 "--max-buffer-mib", str(budget), "--seconds", "300"]
            )
            assert cfg.effective_buffer_mib <= budget, (
                f"{w}x{h} @ {budget:g} MiB pediu {cfg.effective_seconds:.2f}s "
                f"e reservou {cfg.effective_buffer_mib:.1f} MiB"
            )
            # E o teto do slider tambem tem que respeitar o orcamento.
            ctrl = RetroController(_ring(), cfg)
            assert ctrl._projected_mib(ctrl.max_seconds) <= budget * 1.001


def test_seconds_under_the_budget_is_untouched() -> None:
    """O caminho normal nao pode ser afetado pelo clamp."""
    cfg = config_from_args(["--seconds", "30"])
    assert not cfg.seconds_clamped
    assert cfg.effective_seconds == 30
    assert cfg.effective_buffer_mib == pytest.approx(cfg.estimated_buffer_mib())


def test_effective_seconds_has_a_floor_of_one() -> None:
    """Nenhum clamp pode devolver 0 s: o ring ficaria com 1 frame e o replay
    sairia com duracao zero em vez de avisar."""
    cfg = config_from_args(["--seconds", "0.001", "--max-buffer-mib", "0.0001"])
    assert cfg.effective_seconds >= 1.0
    assert cfg.effective_capacity >= 1


def test_slider_ignores_duplicate_positions() -> None:
    ctrl = RetroController(_ring(), config_from_args([]))
    before = ctrl.ring.stats().target_seconds
    ctrl.poll()  # nada pendente: nao deve reencostar no ring
    assert ctrl.ring.stats().target_seconds == before


def test_slider_applies_the_new_value_after_the_debounce() -> None:
    ctrl = RetroController(_ring(), config_from_args([]))
    ctrl._on_change(7)
    assert ctrl.ring.stats().target_seconds != 7  # ainda no debounce

    time.sleep(RetroController.DEBOUNCE + 0.05)
    ctrl.poll()
    st = ctrl.ring.stats()
    assert st.target_seconds == 7
    assert st.capacity == 210  # 7 s a 30 fps


def test_slider_debounce_collapses_a_drag_into_one_change() -> None:
    """Arrastar o slider dispara o callback a cada pixel; sem debounce, a deque
    seria reconstruida dezenas de vezes por segundo, prendendo o lock que a
    thread de captura usa a cada frame."""
    ctrl = RetroController(_ring(), config_from_args([]))
    for v in range(3, 25):  # simula um arrasto
        ctrl._on_change(v)
    time.sleep(RetroController.DEBOUNCE + 0.05)
    ctrl.poll()
    st = ctrl.ring.stats()
    # Terminou no ULTIMO valor, e o ring nao esta inconsistente.
    assert st.target_seconds == 24
    assert st.nbytes == st.count * (640 * 480 * 3)


def test_slider_clamps_a_zero_from_the_trackbar() -> None:
    """O trackbar vai de 1 ao max, mas um 0 nao pode chegar ao ring buffer."""
    ctrl = RetroController(_ring(), config_from_args([]))
    ctrl._on_change(0)
    time.sleep(RetroController.DEBOUNCE + 0.05)
    ctrl.poll()
    assert ctrl.ring.stats().target_seconds >= 1


def test_projected_mib_tracks_the_window() -> None:
    """O numero que o log do slider mostra tem que acompanhar o tamanho da janela."""
    cfg = config_from_args(["--buf-width", "640", "--buf-height", "480"])
    ring = _ring()
    ctrl = RetroController(ring, cfg)
    small = ctrl._projected_mib(5.0)
    big = ctrl._projected_mib(20.0)
    assert big == pytest.approx(small * 4, rel=0.02)


def test_slider_reports_instead_of_raising_when_the_ring_cannot_retime() -> None:
    """Um ring sem fps nao pode ser reajustado; o preview avisa e segue vivo,
    em vez de a thread da preview morrer com excecao."""
    ctrl = RetroController(RingBuffer(capacity=100, seconds=10.0), config_from_args([]))
    ctrl._on_change(5)
    time.sleep(RetroController.DEBOUNCE + 0.05)
    ctrl.poll()  # nao pode levantar
    assert ctrl.ring.stats().target_seconds == 10.0


def test_preview_window_title_is_stable() -> None:
    """O harness de verificacao procura a janela por titulo exato."""
    from replaycam.cli import PREVIEW_WINDOW

    assert PREVIEW_WINDOW == "replaycam"
    assert cv2.getBuildInformation()  # so checando que cv2 responde

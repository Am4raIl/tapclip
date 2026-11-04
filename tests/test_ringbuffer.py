"""Testes do RingBuffer. Nao precisam de camera nem de disco."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from replaycam.ringbuffer import RingBuffer

BGR = (16, 12)  # largura, altura pequenas: 16*12*3 = 576 bytes por frame


def _frame(value: int = 0) -> np.ndarray:
    return np.full((BGR[1], BGR[0], 3), value, np.uint8)


def test_sizing_for_duration() -> None:
    ring = RingBuffer.for_duration(fps=30.0, seconds=30.0)
    assert ring.stats().capacity == 900


def test_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        RingBuffer(capacity=0)
    with pytest.raises(ValueError):
        RingBuffer(capacity=10, seconds=0)
    with pytest.raises(ValueError):
        RingBuffer.for_duration(fps=0, seconds=10)


def test_keeps_exactly_the_time_window_despite_bad_fps() -> None:
    """O caso que `deque(maxlen=fps*seconds)` erra.

    Pedimos 30 fps por 2 s (teto de 60 frames), mas entregamos 15 fps por 5 s
    (75 frames). Um buffer por contagem de frames seguraria tudo; o nosso tem
    de cortar em ~2 s de tempo real.
    """
    ring = RingBuffer.for_duration(fps=30.0, seconds=2.0)
    t0 = 100.0
    for i in range(75):  # 5 s a 15 fps
        ring.push(_frame(), t0 + i / 15.0)

    st = ring.stats()
    assert st.span_seconds == pytest.approx(2.0, abs=0.15)
    # ~2 s a 15 fps = ~30 frames, bem abaixo do teto de 60.
    assert 25 <= st.count <= 35
    assert st.count < st.capacity


def test_capacity_is_a_hard_ceiling_when_time_disabled() -> None:
    ring = RingBuffer(capacity=10, seconds=None)
    for i in range(50):
        ring.push(_frame(i), float(i))
    assert len(ring) == 10
    # Snapshot tem que estar em ordem cronologica e conter os 10 mais recentes.
    snap = ring.snapshot()
    assert len(snap) == 10
    assert [int(f[0, 0, 0]) for f in snap] == list(range(40, 50))


def test_byte_accounting_is_exact() -> None:
    ring = RingBuffer.for_duration(fps=30.0, seconds=2.0)
    expected = 30 * 2 * BGR[0] * BGR[1] * 3
    for i in range(60):
        ring.push(_frame(), 100.0 + i / 30.0)
    st = ring.stats()
    assert st.nbytes == st.count * BGR[0] * BGR[1] * 3
    assert 0 < st.nbytes <= expected

    ring.clear()
    assert ring.stats().nbytes == 0
    assert len(ring) == 0


def test_snapshot_is_isolated_from_later_pushes() -> None:
    ring = RingBuffer(capacity=50, seconds=None)
    ring.push(_frame(1), 1.0)
    ring.push(_frame(2), 2.0)
    snap = ring.snapshot()
    ring.push(_frame(3), 3.0)
    assert len(snap) == 2, "snapshot nao pode mudar quando o buffer recebe mais frames"
    assert [f[0, 0, 0] for f in snap] == [1, 2]


def test_snapshot_survives_concurrent_pushers() -> None:
    """Snapshot durante push concorrente nao pode levantar RuntimeError.

    Iterar uma deque que esta sendo mutada levanta RuntimeError; e exatamente
    isso que o lock do snapshot previne.
    """
    ring = RingBuffer(capacity=200, seconds=30.0)
    stop = threading.Event()
    errors: list[BaseException] = []

    def pusher() -> None:
        try:
            while not stop.is_set():
                ring.push(_frame(), time.perf_counter())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=pusher) for _ in range(4)]
    for th in threads:
        th.start()
    try:
        for _ in range(300):
            snap = ring.snapshot()
            for frame in snap:  # itera a copia local, como a thread writer faz
                assert frame.shape == (BGR[1], BGR[0], 3)
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5.0)

    assert not errors, f"erros nas threads: {errors!r}"
    assert 0 < len(ring) <= 200


def test_measured_fps_reflects_timestamps() -> None:
    ring = RingBuffer.for_duration(fps=30.0, seconds=5.0)
    for i in range(31):
        ring.push(_frame(), 200.0 + i / 30.0)
    assert ring.measured_fps() == pytest.approx(30.0, abs=0.1)


def test_is_full_means_at_capacity_not_span() -> None:
    """`is_full` = no teto de frames, para o usuario saber que o replay cobre tudo.

    A duracao realmente coberta e sempre um frame a menos que o alvo, porque
    nao existe frame entre o ultimo capturado e "agora". Por isso o criterio
    e `count >= capacity`, nao `span >= seconds`.
    """
    ring = RingBuffer.for_duration(fps=30.0, seconds=1.0)  # teto de 30 frames
    for i in range(29):
        ring.push(_frame(), 500.0 + i / 30.0)
    assert not ring.stats().is_full

    ring.push(_frame(), 500.0 + 29 / 30.0)  # 30 frames = teto
    st = ring.stats()
    assert st.is_full
    assert st.count == st.capacity == 30
    assert st.fill_ratio == 1.0
    # Confirmando a semantica: span e menor que o alvo mesmo cheio.
    assert st.span_seconds == pytest.approx(29 / 30.0, abs=0.01)
    assert st.span_seconds < st.target_seconds


# ------------------------------------------------------------- set_seconds (slider)


def test_set_seconds_recomputes_capacity() -> None:
    ring = RingBuffer.for_duration(fps=30.0, seconds=5.0)
    assert ring.stats().capacity == 150
    ring.set_seconds(10.0)
    assert ring.stats().capacity == 300
    assert ring.stats().target_seconds == 10.0


def test_shrinking_the_window_drops_frames_immediately() -> None:
    """Encolher precisa descartar na hora: o replay nao pode dar mais que o slider."""
    ring = RingBuffer.for_duration(fps=30.0, seconds=10.0)
    now = time.perf_counter()
    for i in range(300):  # 10 s a 30 fps
        ring.push(_frame(), now - 10.0 + i / 30.0)
    assert ring.stats().count == 300

    ring.set_seconds(2.0)
    st = ring.stats()
    assert st.capacity == 60
    # So os ~2 s mais recentes sobrevivem; o resto foi descartado.
    assert st.count <= 61
    assert st.nbytes == st.count * _frame().nbytes  # contagem de bytes continua exata
    assert st.dropped > 239


def test_growing_the_window_does_not_invent_past_frames() -> None:
    """Crescer a janela sobe o teto: o passado nao e recuperado."""
    ring = RingBuffer.for_duration(fps=30.0, seconds=2.0)
    now = time.perf_counter()
    for i in range(60):
        ring.push(_frame(), now - 2.0 + i / 30.0)
    before = ring.stats().count
    ring.set_seconds(30.0)
    after = ring.stats()
    assert after.capacity == 900
    assert after.count == before  # nada foi criado do nada


def test_set_seconds_keeps_bytes_consistent_under_repeated_calls() -> None:
    """O slider pode ser arrastado muitas vezes; a contagem tem que ficar exata."""
    ring = RingBuffer.for_duration(fps=30.0, seconds=5.0)
    now = time.perf_counter()
    for i in range(150):
        ring.push(_frame(), now - 5.0 + i / 30.0)
    for target in (3.0, 12.0, 1.0, 7.5, 4.0):
        ring.set_seconds(target)
        st = ring.stats()
        assert st.capacity == int(round(30 * target))
        assert st.nbytes == st.count * _frame().nbytes


def test_set_seconds_requires_fps() -> None:
    """Sem fps nao da para recalcular o teto; e melhor falhar que adivinhar."""
    ring = RingBuffer(capacity=10, seconds=5.0)
    with pytest.raises(RuntimeError, match="fps"):
        ring.set_seconds(8.0)


def test_set_seconds_rejects_non_positive() -> None:
    ring = RingBuffer.for_duration(fps=30.0, seconds=5.0)
    with pytest.raises(ValueError):
        ring.set_seconds(0.0)

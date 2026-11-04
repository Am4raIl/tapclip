"""Ring buffer circular de frames em RAM, com janela temporal exata.

## Por que nao basta `deque(maxlen=fps * seconds)`

A receita obvia seria `deque(maxlen=30 * fps)`. Ela so funciona se a camera
entregar EXATAMENTE o FPS pedido. Na pratica cameras mentem: voce pede 30 fps,
ela entrega 15 (limite do driver, modo USB, camera ocupada) e o buffer passa a
guardar 30*30 = 900 frames que, a 15 fps reais, sao **60 segundos** de tempo
corrido. Exportando a 30 fps o replay sai em camera lenta.

Aqui `maxlen` e apenas um **teto de seguranca** e a janela e mantida por
`time.perf_counter()`. Assim o buffer sempre vale N segundos REAIS, mesmo com
drop de frames, troca de modo da camera ou CPU saturada.

## Ownership dos arrays numpy

`push()` guarda a REFERENCIA do array, sem copiar. Isso e seguro e e o que
mantem o loop de captura barato: tanto `cap.read()` quanto `cv2.resize()`
alocam um array novo a cada chamada, entao o buffer fica com referencia
privada de um array que ninguem mais vai mutar.

Cuidado: se algum dia voce trocar para um backend de captura com buffer
compartilhado/zero-copy, a referencia deixa de ser exclusiva e passa a ser
necessario um `.copy()` explicito no push. O comentario acima existe para
avisar quem fizer essa troca.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FrameSample:
    """Um frame capturado e o instante (monotonic) em que chegou."""

    t: float
    frame: np.ndarray


@dataclass(frozen=True, slots=True)
class RingStats:
    """Fotografia do buffer. Barato de ler, mas nao usar no hot loop a cada frame."""

    count: int
    capacity: int
    nbytes: int
    dropped: int
    pushed: int
    span_seconds: float
    target_seconds: float

    @property
    def mib(self) -> float:
        return self.nbytes / (1024 * 1024)

    @property
    def fill_ratio(self) -> float:
        return self.count / self.capacity if self.capacity else 0.0

    @property
    def is_full(self) -> bool:
        """True quando o buffer esta no teto e ja comeca a descartar frames antigos.

        Definido por `count >= capacity`, e nao por `span >= seconds`: o que
        importa para quem vai apertar o gatilho e que os frames mais antigos
        ja estao sendo rotacionados fora. A duracao realmente coberta fica em
        `span_seconds`, que e sempre um frame a menos que o alvo (o intervalo
        entre o ultimo frame e "agora" nao tem frame nenhum).
        """
        return self.count >= self.capacity


class RingBuffer:
    """Fila circular de frames com janela temporal garantida e contagem exata de RAM.

    Thread-safe: `push` (thread de captura) e `snapshot` (thread de escrita)
    sao as unicas operacoes publicas e ambas sao serializadas por um unico
    lock. O lock e o unico ponto de conflito: `snapshot` apenas copia ponteiros
    (~0.01 ms para 900 frames), entao a thread de captura quase nunca espera.
    """

    def __init__(self, capacity: int, seconds: float | None = None, fps: float | None = None) -> None:
        """`capacity` e o teto de frames; `seconds` e a janela temporal alvo.

        `fps` e o que permite `set_seconds` recalcular o teto ao vivo (slider da
        preview). Sem ele, a janela pode ser lida mas nao alterada.

        Use `RingBuffer.for_duration(fps, seconds)` para dimensionar certo.
        """
        if capacity < 1:
            raise ValueError(f"capacity precisa ser >= 1, recebido {capacity}")
        if seconds is not None and seconds <= 0:
            raise ValueError(f"seconds precisa ser > 0, recebido {seconds}")
        if fps is not None and fps <= 0:
            raise ValueError(f"fps precisa ser > 0, recebido {fps}")

        self._capacity = capacity
        self._seconds = seconds
        self._fps = fps
        # maxlen ainda e ativado como rede de seguranca, mas a eviccao normal
        # acontece explicitamente em `_evict_locked` para manter `_bytes` exato.
        self._dq: deque[FrameSample] = deque(maxlen=capacity)
        self._lock = threading.Lock()

        self._bytes = 0
        self._pushed = 0
        self._dropped = 0

    # ------------------------------------------------------------------ sizing

    @classmethod
    def for_duration(cls, fps: float, seconds: float) -> "RingBuffer":
        """Dimensiona o buffer para `seconds` a `fps`.

        O teto de frames e calculado pela duracao pedida, mas a janela real e
        governada pelo tempo (ver docstring do modulo).
        """
        if fps <= 0:
            raise ValueError(f"fps precisa ser > 0, recebido {fps}")
        capacity = max(1, int(round(fps * seconds)))
        return cls(capacity=capacity, seconds=seconds, fps=fps)

    def estimate_bytes(self, width: int, height: int, channels: int = 3) -> int:
        """RAM maxima teorica do buffer, para aviso antes de comecar a capturar."""
        return self._capacity * width * height * channels

    # ------------------------------------------------------------------- write

    def push(self, frame: np.ndarray, t: float | None = None) -> None:
        """Insere um frame, descartando os mais antigos que sairam da janela.

        NAO faz `.copy()`: ver "Ownership dos arrays numpy" na docstring do
        modulo. `t` default e `perf_counter()` no momento da chamada.
        """
        if t is None:
            t = time.perf_counter()

        nbytes = frame.nbytes
        with self._lock:
            self._evict_locked(t)
            self._dq.append(FrameSample(t=t, frame=frame))
            self._bytes += nbytes
            self._pushed += 1

    def _evict_locked(self, now: float) -> None:
        """Remove da cabeca enquanto a janela estourar o alvo ou a capacidade.

        Deve ser chamado com o lock adquirido. A eviccao e explicita (em vez de
        deixar o `maxlen` do deque fazer isso) para que a contagem de bytes
        permaneca exata.
        """
        dq = self._dq
        seconds = self._seconds

        if seconds is not None:
            cutoff = now - seconds
            while dq and dq[0].t < cutoff:
                self._bytes -= dq.popleft().frame.nbytes
                self._dropped += 1

        # Teto de seguranca: cobre o caso de a camera entregar mais que o FPS
        # pedido, ou `seconds=None`.
        while len(dq) >= self._capacity:
            self._bytes -= dq.popleft().frame.nbytes
            self._dropped += 1

    # -------------------------------------------------------------------- read

    def snapshot(self) -> list[np.ndarray]:
        """Copia a lista de REFERENCIAS do conteudo atual, do mais antigo ao mais novo.

        Seguro para iterar mesmo com a thread de captura empurrando frames,
        porque a deque e modificada sob o lock e a copia local ja esta isolada.
        Nao copia pixels - a thread de escrita decodifica/reescreve em disco.
        """
        with self._lock:
            return [s.frame for s in self._dq]

    def snapshot_samples(self) -> list[FrameSample]:
        """Como `snapshot`, mas preservando os timestamps (usado em diagnostico)."""
        with self._lock:
            return list(self._dq)

    def measured_fps(self) -> float:
        """FPS real de entrega medido a partir dos timestamps do conteudo atual."""
        samples = self.snapshot_samples()
        if len(samples) < 2:
            return 0.0
        span = samples[-1].t - samples[0].t
        if span <= 0:
            return 0.0
        return (len(samples) - 1) / span

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)

    def stats(self) -> RingStats:
        """Snapshot das metricas. Custo O(n) em contagem de frames, nao em bytes."""
        with self._lock:
            count = len(self._dq)
            span = (self._dq[-1].t - self._dq[0].t) if count >= 2 else 0.0
            return RingStats(
                count=count,
                capacity=self._capacity,
                nbytes=self._bytes,
                dropped=self._dropped,
                pushed=self._pushed,
                span_seconds=span,
                target_seconds=self._seconds if self._seconds is not None else 0.0,
            )

    def set_seconds(self, seconds: float) -> None:
        """Reajusta a janela temporal em tempo de execucao (usado pelo slider).

        O `maxlen` de uma `deque` e imutavel, entao mudar a janela exige recriar
        a deque. Isso e aceitavel porque este metodo so e chamado quando o
        usuario move o slider - algumas vezes por sessao, nao por frame - e
        percorrer ponteiros e da ordem de microssegundos mesmo com 900 frames.

        Semantica, espelhando o que `push` ja faz:
          - encolher  -> frames fora da nova janela sao descartados na hora;
          - crescer   -> o teto de frames sobe, mas o passado NAO e recuperado.
                         O buffer segue com o que tem ate encher, e e por isso
                         que o status mostra `5.0/30s` em vez de `30.0/30s`.

        Sem `fps` conhecido nao da para recalcular o teto de frames, e o metodo
        nao faz nada em vez de adivinhar - `is_full` ficaria mentindo.
        """
        if seconds <= 0:
            raise ValueError(f"seconds precisa ser > 0, recebido {seconds}")
        if self._fps is None:
            raise RuntimeError(
                "set_seconds exige um RingBuffer construído com fps "
                "(use RingBuffer.for_duration) para recalcular o teto de frames"
            )
        with self._lock:
            if self._seconds is not None and abs(self._seconds - seconds) < 1e-9:
                return
            self._retime_locked(seconds, max(1, int(round(self._fps * seconds))))

    def _retime_locked(self, seconds: float, capacity: int) -> None:
        """Recria a deque com novo `maxlen`, mantendo o que sobrevive a janela.

        Deve ser chamado com o lock adquirido.
        """
        now = time.perf_counter()
        cutoff = now - seconds
        kept: deque[FrameSample] = deque(maxlen=max(1, capacity))
        kept_bytes = 0
        for sample in self._dq:
            if sample.t < cutoff:
                self._dropped += 1
                continue
            kept.append(sample)
            kept_bytes += sample.frame.nbytes
        self._dq = kept
        self._capacity = max(1, capacity)
        self._bytes = kept_bytes
        self._seconds = seconds

    def clear(self) -> None:
        with self._lock:
            self._dq.clear()
            self._bytes = 0

    def __repr__(self) -> str:
        st = self.stats()
        return (
            f"<RingBuffer {st.count}/{st.capacity} frames "
            f"{st.mib:.1f} MiB span={st.span_seconds:.1f}s dropped={st.dropped}>"
        )

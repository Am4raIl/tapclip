"""replaycam - ring buffer de video em RAM com replay instantaneo.

Fluxo geral:

    camera -> RingBuffer (RAM)  --[Enter / GPIO / MQTT / HTTP]-->  VideoWriter (disco)

O `RingBuffer` e o unico estado compartilhado. A thread de captura escreve
nele; a thread de escrita le um snapshot dele. Nenhuma das duas toca o
disco da outra, entao a captura nao trava enquanto o .mp4 e salvo.
"""

from .config import Config
from .ringbuffer import FrameSample, RingBuffer
from .triggers import Trigger
from .writer import ExportResult, VideoExporter

__version__ = "1.0.0"

__all__ = [
    "Config",
    "ExportResult",
    "FrameSample",
    "RingBuffer",
    "Trigger",
    "VideoExporter",
    "__version__",
]

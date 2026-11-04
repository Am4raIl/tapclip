"""Fontes de gatilho.

Este e o ponto de extensao do sistema. A regra e uma so:

    um gatilho chama `on_fire(source)` e nao faz mais nada.

Quem decide o que fazer com o replay NAO e o gatilho, e a `cli`, que enfileira
o pedido na fila da thread de escrita. Isso mantem o caminho de gravacao
identico para teclado, GPIO, MQTT e HTTP, e permite combinar varios gatilhos
ao mesmo tempo (`--trigger keyboard,gpio`).

## Como adicionar o seu proprio

```python
class MyTrigger(Trigger):
    name = "meu-gatilho"

    def _read_loop(self) -> None:          # bloqueia ate stop_event
        while not self.stop_event.is_set():
            if botao_foi_pressionado():
                self.fire()
            time.sleep(0.02)
```

`fire()` aplica o debounce e chama `on_fire`. Nao coloque logica de gravacao
aqui dentro, senao voce perde o isolamento entre threads.

Implementacoes de referencia (GPIO/RPi, MQTT/ESP32, HTTP) estao ao final deste
arquivo, comentadas, prontas para copiar quando voce tiver o hardware.
"""

from __future__ import annotations

import abc
import sys
import threading
import time
from typing import Callable

# Teclas: '\r' e '\n' sao as duas representacoes de Enter no Windows.
ENTER_KEYS = ("\r", "\n")

FireCallback = Callable[[str], None]


class Trigger(abc.ABC):
    """Base de todo gatilho. Subclasses implementam apenas `_read_loop`."""

    name = "trigger"

    def __init__(
        self,
        on_fire: FireCallback,
        debounce: float = 0.35,
        poll_interval: float = 0.02,
    ) -> None:
        self._on_fire = on_fire
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fire = 0.0
        self.fire_count = 0

    # ------------------------------------------------------------- para subclasses

    @abc.abstractmethod
    def _read_loop(self) -> None:
        """Fica bloqueado lendo a fonte; retorna quando `stop_event` for setado."""

    # --------------------------------------------------------------------- comum

    def fire(self) -> bool:
        """Dispara o callback, respeitando debounce. Retorna True se aceitou.

        O debounce protege contra rajadas: botoes mecanicos quicam, GPIO gera
        bordas de ruido e MQTT pode reentregar a mesma mensagem QoS>0.
        """
        now = time.monotonic()
        if now - self._last_fire < self.debounce:
            return False
        self._last_fire = now
        self.fire_count += 1
        try:
            self._on_fire(self.name)
        except Exception:  # noqa: BLE001 - um gatilho nunca pode derrubar a si mesmo
            import traceback

            traceback.print_exc()
        return True

    def start(self) -> threading.Thread:
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._run_guarded, name=f"trigger:{self.name}", daemon=True)
        self._thread.start()
        return self._thread

    def _run_guarded(self) -> None:
        try:
            self._read_loop()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> "Trigger":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class KeyboardEnterTrigger(Trigger):
    """Dispara quando o usuario aperta Enter no console.

    Implementado com `msvcrt` no Windows (stdlib, zero dependencias, nao exige
    admin) e com `termios`+`select` no POSIX.

    Limitacao importante: ambos leem do CONSOLE do processo. Se o usuario clicar
    em outra janela, o Enter deixa de chegar. Para hotkey global independente
    de foco, instale `pynput` e veja `PynputEnterTrigger` ao final do arquivo.
    """

    name = "keyboard"

    def _read_loop(self) -> None:
        if sys.platform == "win32":
            self._read_loop_windows()
        else:
            self._read_loop_posix()

    def _read_loop_windows(self) -> None:
        import msvcrt

        while not self.stop_event.is_set():
            # kbhit() nao bloqueia: essencial para o stop_event ser notado.
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ENTER_KEYS:
                    self.fire()
                # Ignora o segundo caractere de sequences como "\x00" (teclas
                # especiais do console) e o '\n' de eco de "\r".
            self.stop_event.wait(self.poll_interval)

    def _read_loop_posix(self) -> None:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            # stdin nao e um terminal (pip, servico, notebook): sem hotkey.
            return
        try:
            tty.setcbreak(fd)
            while not self.stop_event.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], self.poll_interval)
                if ready:
                    char = sys.stdin.read(1)
                    if char in ENTER_KEYS:
                        self.fire()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ============================================================================
# REFERENCIAS PARA AS OUTRAS FONTES DE GATILHO
#
# As tres classes abaixo estao comentadas de proposito: nao ha como testar GPIO
# de Raspberry Pi nem MQTT de ESP32 nesta maquina, e codigo de hardware nao
# testado e pior do que codigo ausente. Copie a que precisar, instale a
# dependencia correspondente e registre em `build_triggers` (em cli.py).
#
# Todos seguem o mesmo contrato: implementam `_read_loop` e chamam `self.fire()`.
# ============================================================================
#
# --- 1) Raspberry Pi: botao fisico em GPIO -----------------------------------
#
# class GpioButtonTrigger(Trigger):
#     name = "gpio"
#
#     def __init__(self, on_fire, pin: int = 17, *, pull_up: bool = True, **kw):
#         super().__init__(on_fire, **kw)
#         self.pin = pin
#         self.pull_up = pull_up
#
#     def _read_loop(self) -> None:
#         import RPi.GPIO as GPIO
#         GPIO.setmode(GPIO.BCM)
#         GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP if self.pull_up else GPIO.PUD_DOWN)
#         try:
#             while not self.stop_event.is_set():
#                 pressed = GPIO.input(self.pin) == (not self.pull_up)
#                 if pressed:
#                     self.fire()          # o debounce de `fire()` ja segura o chaco
#                 self.stop_event.wait(0.005)   # 5 ms: latencia boa, CPU baixa
#         finally:
#             GPIO.cleanup()
#
# Nota: prefira este polling a `add_event_detect` quando a thread ja esta em
# loop; a callback do RPi.GPIO roda na mesma GIL e pode atrasar a captura.
#
#
# --- 2) ESP32 -> MQTT (botao do outro lado da casa, pela rede) ---------------
#
# class MqttTrigger(Trigger):
#     name = "mqtt"
#
#     def __init__(self, on_fire, host="localhost", port=1883, topic="cam/trigger", **kw):
#         super().__init__(on_fire, **kw)
#         self.host, self.port, self.topic = host, port, topic
#
#     def _read_loop(self) -> None:
#         import paho.mqtt.client as mqtt
#         client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
#
#         def on_connect(cl, _userdata, _flags, reason_code, _props=None):
#             if reason_code == 0:
#                 cl.subscribe(self.topic)
#
#         def on_message(_cl, _userdata, msg):
#             # Payload "1"/"on"/"press" liga; qualquer outra coisa e ruido.
#             if msg.payload.decode(errors="ignore").strip() in ("1", "on", "press"):
#                 self.fire()
#             # NADA pesado aqui: esta e a thread de rede do paho. `fire()`
#             # so enfileira; a gravacao acontece na thread de escrita.
#
#         client.on_connect = on_connect
#         client.on_message = on_message
#         client.connect(self.host, self.port, keepalive=30)
#         client.loop_start()               # nao bloqueia esta thread
#         try:
#             while not self.stop_event.is_set():
#                 self.stop_event.wait(0.5)
#         finally:
#             client.loop_stop()
#             client.disconnect()
#
# No ESP32: publique em `cam/trigger` no `loop()` quando o botao for apertado,
# com `client.publish("cam/trigger", "1", retain=False)`.
#
#
# --- 3) HTTP: um POST de qualquer lugar (celular, curl, outro PC) ------------
#
# class HttpTrigger(Trigger):
#     name = "http"
#
#     def __init__(self, on_fire, host="0.0.0.0", port=8080, path="/trigger", **kw):
#         super().__init__(on_fire, **kw)
#         self.address, self.port, self.path = host, port, path
#
#     def _read_loop(self) -> None:
#         from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
#
#         trigger = self
#
#         class Handler(BaseHTTPRequestHandler):
#             def do_POST(self):                       # noqa: N802 (nome imposto)
#                 if self.path == trigger.path:
#                     trigger.fire()
#                     self.send_response(202)
#                 else:
#                     self.send_response(404)
#                 self.end_headers()
#                 self.wfile.write(b"ok")
#
#             def log_message(self, *a):               # silencia o log por request
#                 pass
#
#         # ThreadingHTTPServer: cada request em thread propria, para um POST
#         # lento nunca travar os outros.
#         ThreadingHTTPServer((self.address, self.port), Handler).serve_forever()
#
# Teste: `curl -X POST http://localhost:8080/trigger`
# Seguranca: em LAN confiavel e aceitavel. Exposto na internet, exija um
# header de token - qualquer um com a URL poderia disparar gravacoes.

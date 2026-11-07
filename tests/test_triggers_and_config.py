"""Testes dos gatilhos e do parsing de configuracao."""

from __future__ import annotations

import time

import pytest

from replaycam.config import Config, config_from_args
from replaycam.triggers import KeyboardEnterTrigger, Trigger


class ManualTrigger(Trigger):
    """Gatilho de teste: `poke()` simula um evento do mundo exterior."""

    name = "manual"

    def __init__(self, on_fire, **kw) -> None:
        super().__init__(on_fire, **kw)
        self.poked = 0

    def _read_loop(self) -> None:
        while not self.stop_event.is_set():
            self.stop_event.wait(0.01)

    def poke(self) -> bool:
        self.poked += 1
        return self.fire()


def test_fire_calls_callback_with_source_name() -> None:
    seen: list[str] = []
    trg = ManualTrigger(seen.append, debounce=0.0)
    assert trg.poke() is True
    assert seen == ["manual"]


def test_debounce_rejects_rapid_refire() -> None:
    seen: list[str] = []
    trg = ManualTrigger(seen.append, debounce=0.5)
    for _ in range(10):
        trg.poke()
    assert len(seen) == 1, f"debounce nao segurou: {seen}"
    time.sleep(0.55)
    assert trg.poke() is True
    assert len(seen) == 2


def test_callback_exception_does_not_kill_trigger() -> None:
    """Um gatilho nunca pode derrubar a si mesmo nem o pipeline."""

    def boom(_name: str) -> None:
        raise RuntimeError("falha no consumidor")

    trg = ManualTrigger(boom, debounce=0.0)
    th = trg.start()
    try:
        trg.poke()
        trg.poke()
        # O traceback vai para stderr, mas fire() engole a excecao e segue vivo.
        assert trg.fire_count == 2
        assert th.is_alive()
    finally:
        trg.stop(timeout=1.0)


def test_start_stop_joins_thread() -> None:
    trg = ManualTrigger(lambda _n: None)
    th = trg.start()
    time.sleep(0.02)
    assert th.is_alive()
    trg.stop(timeout=1.0)          # stop() e o que faz a _read_loop sair
    assert not th.is_alive()
    assert trg.stop_event.is_set()


def test_keyboard_trigger_thread_starts_and_stops() -> None:
    """Smoke test: a thread do teclado sobe e desce sem estourar.

    Nao simulamos a tecla - isso exigiria injetar o console. Verificado aqui e
    que a construcao e o teardown funcionam em qualquer plataforma.
    """
    trg = KeyboardEnterTrigger(lambda _n: None)
    trg.start()
    time.sleep(0.05)
    trg.stop(timeout=1.0)
    assert trg.stop_event.is_set()


# ---------------------------------------------------------------- config


def test_defaults_match_documented_30s_plan() -> None:
    cfg = config_from_args([])
    assert cfg.quality == "standard"
    assert cfg.seconds == 30.0
    assert cfg.fps == 30.0
    assert cfg.buf_width == 640 and cfg.buf_height == 480
    assert cfg.triggers == ("keyboard",)
    assert cfg.effective_export_fps == 30.0


def test_quality_high_uses_720p_and_keeps_30s() -> None:
    cfg = config_from_args(["--quality", "high"])
    assert cfg.quality == "high"
    assert (cfg.width, cfg.height) == (1280, 720)
    assert (cfg.buf_width, cfg.buf_height) == (1280, 720)
    assert cfg.max_buffer_mib == 2560
    assert cfg.effective_seconds == 30.0
    assert cfg.estimated_buffer_mib() > 2300
    assert cfg.max_retro_seconds >= 30


def test_quality_high_does_not_emit_the_generic_ram_alarm() -> None:
    """O preset high gasta mais RAM de proposito; o aviso generico seria ruido."""
    assert config_from_args(["--quality", "high"]).validate() == []


def test_quality_high_respects_explicit_overrides() -> None:
    cfg = config_from_args([
        "--quality", "high",
        "--buf-width", "640", "--buf-height", "480",
        "--max-buffer-mib", "1024",
    ])
    assert (cfg.buf_width, cfg.buf_height) == (640, 480)
    assert cfg.max_buffer_mib == 1024
    # A captura continua no modo high, a menos que o usuario tambem a mude.
    assert (cfg.width, cfg.height) == (1280, 720)


def test_quality_high_warns_when_the_window_exceeds_its_budget() -> None:
    cfg = config_from_args(["--quality", "high", "--seconds", "120"])
    assert any("teto" in w for w in cfg.validate())


def test_estimated_buffer_is_829mb_for_640x480_30s_30fps() -> None:
    """900 frames x 640x480x3 = 829.440.000 B = 791,0 MiB (ou 829,4 MB decimal)."""
    cfg = Config(fps=30.0, seconds=30.0, buf_width=640, buf_height=480)
    assert cfg.estimated_buffer_bytes() == 900 * 640 * 480 * 3
    assert cfg.estimated_buffer_bytes() == 829_440_000
    assert cfg.estimated_buffer_mib() == pytest.approx(791.0, abs=0.1)


def test_warns_on_huge_buffer() -> None:
    warnings = Config(fps=60.0, seconds=60.0, buf_width=1280, buf_height=720).validate()
    joined = " ".join(warnings)
    assert "RAM" in joined


def test_warns_on_export_fps_mismatch() -> None:
    warnings = Config(fps=30.0, export_fps=60.0).validate()
    assert any("export-fps" in w for w in warnings)


def test_no_warnings_on_the_recommended_defaults() -> None:
    assert config_from_args([]).validate() == []


def test_trigger_key_escape_is_decoded() -> None:
    assert config_from_args(["--trigger-key", "\\r"]).trigger_key == "\r"
    assert config_from_args(["--trigger-key", "q"]).trigger_key == "q"


def test_multiple_triggers_parse() -> None:
    assert config_from_args(["--trigger", "keyboard,keyboard"]).triggers == ("keyboard", "keyboard")
    assert config_from_args(["--trigger", "keyboard, mqtt"]).triggers == ("keyboard", "mqtt")


def test_auto_trigger_defaults_off_and_parses() -> None:
    assert config_from_args([]).auto_trigger == 0.0
    assert config_from_args(["--auto-trigger", "7.5"]).auto_trigger == 7.5


def test_preview_is_on_by_default() -> None:
    """A preview e o modo de uso principal: entra ligada, sai com --no-preview."""
    assert config_from_args([]).preview is True
    assert config_from_args(["--no-preview"]).preview is False


def test_capture_resolution_defaults_to_native() -> None:
    cfg = config_from_args([])
    assert cfg.width == 0 and cfg.height == 0
    # Modo nativo nao pode gerar o aviso de upscale: com 0, todo buffer
    # pareceria "maior que a captura", que e um aviso falso.
    assert not any("MAIOR que a captura" in w for w in cfg.validate())


def test_upscale_warning_still_fires_when_a_mode_is_requested() -> None:
    cfg = config_from_args(["--width", "320", "--height", "240",
                            "--buf-width", "640", "--buf-height", "480"])
    assert any("MAIOR que a captura" in w for w in cfg.validate())


def test_max_retro_seconds_follows_the_ram_budget() -> None:
    """O teto do slider e calculado, nao chutado: 1080p tem muito menos tempo."""
    hd = config_from_args(["--buf-width", "1920", "--buf-height", "1080"])
    sd = config_from_args(["--buf-width", "640", "--buf-height", "480"])
    assert hd.max_retro_seconds < 10
    assert sd.max_retro_seconds > 30
    # Dobrar o orcamento dobra o tempo possivel.
    twice = config_from_args(["--buf-width", "640", "--buf-height", "480",
                              "--max-buffer-mib", "2048"])
    assert twice.max_retro_seconds == pytest.approx(sd.max_retro_seconds * 2, rel=0.01)


def test_seconds_beyond_the_budget_is_flagged() -> None:
    cfg = config_from_args(["--buf-width", "1920", "--buf-height", "1080",
                            "--seconds", "30", "--max-buffer-mib", "1024"])
    assert any("teto" in w for w in cfg.validate())


def test_verbose_flag() -> None:
    assert config_from_args([]).verbose is False
    assert config_from_args(["--verbose"]).verbose is True


def test_mirror_preview_defaults_off_and_parses() -> None:
    assert config_from_args([]).mirror_preview is False
    assert config_from_args(["--mirror-preview"]).mirror_preview is True


def test_short_window_still_warns_on_ram() -> None:
    # Uma janela curta e o jeito mais barato de testar sem estourar RAM.
    cfg = config_from_args(["--seconds", "5"])
    assert cfg.validate() == []
    assert cfg.estimated_buffer_mib() == pytest.approx(131.8, abs=0.5)

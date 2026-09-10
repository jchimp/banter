"""M0 acceptance for the client: config parses, state machine enforces exclusivity."""

import pytest

from banter_client.__main__ import App, dir_problem, stuck_button_problems
from banter_client.config import ClientSettings
from banter_client.state import State, StateMachine


def test_settings_build_urls():
    s = ClientSettings(api_url="http://server:8080/", _env_file=None)
    assert s.recordings_url == "http://server:8080/api/recordings"
    assert s.next_url == "http://server:8080/api/recordings/next"
    assert s.heartbeat_url().endswith("/api/devices/kidbox-01/heartbeat")


def test_default_pins_match_wiring_diagram():
    s = ClientSettings(_env_file=None)
    assert s.pin_record == 17
    assert s.pin_play == 22


def test_record_and_play_are_mutually_exclusive():
    m = StateMachine()
    assert m.to(State.RECORDING)
    # FR-10: no playback while recording
    assert not m.can(State.PLAYING)
    assert not m.to(State.PLAYING)
    assert m.state is State.RECORDING


def test_playing_blocks_recording():
    m = StateMachine()
    m.to(State.PLAYING)
    assert not m.to(State.RECORDING)
    assert m.is_busy()


def test_normal_record_cycle():
    m = StateMachine()
    assert m.to(State.RECORDING)
    assert m.to(State.UPLOADING)
    assert m.to(State.IDLE)
    assert not m.is_busy()


def test_error_recovers_to_idle_only():
    m = StateMachine()
    m.to(State.ERROR)
    assert not m.to(State.RECORDING)
    assert m.to(State.IDLE)


@pytest.mark.parametrize("mode", ["hold", "toggle"])
def test_button_modes_accepted(mode):
    assert ClientSettings(button_mode=mode, _env_file=None).button_mode == mode


def test_default_heartbeat_interval():
    assert ClientSettings(_env_file=None).heartbeat_interval_seconds == 60.0


# ------------------------------------------------- data dir preflight (main())
def test_dir_problem_creates_missing_dir(tmp_path):
    target = tmp_path / "queue" / "nested"
    assert dir_problem(target) is None
    assert target.is_dir()


def test_dir_problem_reports_unusable_path(tmp_path):
    # A file where a directory should be: mkdir raises, and the caller gets a message
    # naming the path instead of a traceback out of RecordingQueue.
    blocker = tmp_path / "queue"
    blocker.write_text("not a dir")
    problem = dir_problem(blocker / "sub")
    assert problem is not None
    assert str(blocker) in problem


# ------------------------------------------------ stuck-button preflight (run())
def test_stuck_button_problems_empty_when_nothing_held():
    assert stuck_button_problems([]) == []


def test_stuck_button_problems_names_the_pin():
    (problem,) = stuck_button_problems([("play", 27)])
    assert "name=play" in problem
    assert "pin=27" in problem
    # The message has to explain *why* silence follows, or the next person repeats the
    # GPIO22 session: a stuck line looks exactly like a button nobody pressed.
    assert "EDGE" in problem


class _FakeButtons:
    """Stands in for GpioButtons so the preflight is testable with no GPIO."""

    def __init__(self, held: list[tuple[str, int]]) -> None:
        self._held = held

    def start(self) -> None: ...

    def held_pins(self) -> list[tuple[str, int]]:
        return self._held

    def close(self) -> None: ...


def _gpio_settings(tmp_path) -> ClientSettings:
    # button_backend="gpio" with make_buttons patched out: proves the guard reads
    # config rather than sniffing the object it got back.
    return ClientSettings(
        audio_backend="synthetic",
        button_backend="gpio",
        ring_backend="null",
        queue_dir=tmp_path / "queue",
        cache_dir=tmp_path / "cache",
        _env_file=None,
    )


def test_check_buttons_logs_a_stuck_pin(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        "banter_client.__main__.make_buttons", lambda s, cb: _FakeButtons([("play", 27)])
    )
    app = App(_gpio_settings(tmp_path))
    with caplog.at_level("ERROR"):
        app._check_buttons()
    assert "event=button_stuck" in caplog.text
    assert "pin=27" in caplog.text


def test_check_buttons_silent_when_lines_are_healthy(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("banter_client.__main__.make_buttons", lambda s, cb: _FakeButtons([]))
    app = App(_gpio_settings(tmp_path))
    with caplog.at_level("ERROR"):
        app._check_buttons()
    assert "event=button_stuck" not in caplog.text


def test_check_buttons_survives_a_backend_that_cannot_read(tmp_path, monkeypatch, caplog):
    class _Exploding(_FakeButtons):
        def held_pins(self):
            raise OSError("gpiochip busy")

    monkeypatch.setattr("banter_client.__main__.make_buttons", lambda s, cb: _Exploding([]))
    app = App(_gpio_settings(tmp_path))
    with caplog.at_level("WARNING"):
        app._check_buttons()  # must not raise: diagnostics never block startup
    assert "event=button_check_failed" in caplog.text


def test_check_buttons_skips_non_gpio_backends(tmp_path, monkeypatch, caplog):
    # The keyboard backend has no pins, so the check is meaningless there — and a
    # laptop demo must never see a hardware warning it can do nothing about.
    monkeypatch.setattr(
        "banter_client.__main__.make_buttons", lambda s, cb: _FakeButtons([("play", 27)])
    )
    s = _gpio_settings(tmp_path)
    app = App(s.model_copy(update={"button_backend": "keyboard"}))
    with caplog.at_level("ERROR"):
        app._check_buttons()
    assert "event=button_stuck" not in caplog.text

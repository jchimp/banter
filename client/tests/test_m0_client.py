"""M0 acceptance for the client: config parses, state machine enforces exclusivity."""

import pytest

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

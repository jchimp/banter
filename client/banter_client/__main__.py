"""Entry point. M0: prove config loads and the server is reachable, then exit.

M1 replaces the body with the real loop: buttons -> recorder -> queue -> uploader,
and M2 adds the player. Kept deliberately thin so wiring stays visible.
"""

import logging
import sys

import requests

from banter_client.config import get_settings
from banter_client.state import State, StateMachine

log = logging.getLogger("banter.client")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    settings = get_settings()
    machine = StateMachine(State.IDLE)

    settings.queue_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)

    log.info("device=%s server=%s", settings.device_id, settings.api_url)
    log.info(
        "record=GPIO%d play=GPIO%d mode=%s",
        settings.pin_record,
        settings.pin_play,
        settings.button_mode,
    )
    log.info("state=%s queue=%s", machine.state.value, settings.queue_dir)

    try:
        resp = requests.get(f"{settings.api_url.rstrip('/')}/healthz", timeout=5)
        log.info("server healthz: %s %s", resp.status_code, resp.text.strip())
    except requests.RequestException as exc:
        log.warning("server unreachable (%s) - client would queue locally", exc.__class__.__name__)

    log.info("M0 skeleton OK. Button/audio wiring lands in M1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The radio's commands, for the Radio tab and the Radio card in Settings.
The radio itself is radio.py; this keeps one Link for the app and words
problems for the person."""

from __future__ import annotations

import threading
import time

from museion import radio
from museion.appstate import AppState, UiError

# How long Look for a radio waits for every port to answer.
PROBE_SECONDS = 8


def link(state: AppState) -> radio.Link | None:
    return getattr(state, "radio_link", None)


def start(state: AppState, address: str) -> radio.Link:
    old = link(state)
    if old is not None:
        old.stop()
    state.radio_link = radio.Link(address, state.notes_dir(state.config()), state.emit).start()
    return state.radio_link


def start_saved(state: AppState) -> None:
    """At start: connect the radio named in the config, if there is one."""
    address = state.config().radio
    if address:
        start(state, address)


def radio_state(state: AppState) -> dict:
    found = link(state)
    if found is None:
        return {"address": state.config().radio, "connected": False, "error": "", "me": "", "nodes": []}
    return found.state()


def find_radios(state: AppState) -> list[dict]:
    """Every serial port with a Meshtastic radio answering on it. The one in
    use already is listed without being opened twice. Ports are tried side
    by side, and one that hangs (a Bluetooth port on Windows can) is left
    behind rather than holding up the page."""
    current = link(state)
    found: dict[str, str] = {}

    def try_port(port: str) -> None:
        name = radio.probe(port)
        if name:
            found[port] = name

    threads = []
    for port in radio.serial_ports():
        if current is not None and current.address == port and current.connected:
            found[port] = current.state()["me"]
            continue
        t = threading.Thread(target=try_port, args=(port,), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + PROBE_SECONDS
    for t in threads:
        t.join(max(deadline - time.monotonic(), 0))
    return [{"address": a, "name": n} for a, n in sorted(dict(found).items())]


def connect_radio(state: AppState, address: str) -> dict:
    address = address.strip()
    if not address:
        raise UiError("Name the radio's port (like COM5 or /dev/ttyUSB0), or tcp:<address> for one on the network.")
    cfg = state.config()
    cfg.radio = address
    state.save_config(cfg)
    start(state, address)
    return radio_state(state)


def disconnect_radio(state: AppState) -> dict:
    found = link(state)
    if found is not None:
        found.stop()
        state.radio_link = None
    cfg = state.config()
    cfg.radio = ""
    state.save_config(cfg)
    return radio_state(state)


def radio_messages(state: AppState, limit: int = 100) -> list[dict]:
    return radio.recent(state.notes_dir(state.config()), limit)


def send_radio_message(state: AppState, text: str, to: str = "") -> list[dict]:
    found = link(state)
    if found is None:
        raise UiError("No radio is connected. Connect one in Settings.")
    try:
        return [m.row() for m in found.send(text, to)]
    except radio.RadioError as e:
        raise UiError(str(e)) from None

"""The radio bridge: text messages over a Meshtastic radio, if one is plugged
in. An optional accessory: nothing else in Museion needs it.

Meshtastic radios are small LoRa boards that pass short text messages from
radio to radio, a few kilometres a hop, with no phone network or internet.
A computer talks to one over USB (a serial port) or, for boards with Wi-Fi,
over TCP port 4403, in the radio's own "client API": frames of
0x94 0xC3, a two-byte length, and a protobuf message (ToRadio to the radio,
FromRadio back). The few messages Museion needs are encoded and decoded here
by hand, so it needs no protobuf library: the field numbers are those of
meshtastic/protobufs, meshtastic/mesh.proto (checked against it in
tests/test_radio.py).

Every message sent or received is added to radio-messages.csv in the
person's notes, so it is kept, searched and cited like any other note.

Tested against a simulated radio that speaks the same frames; not yet tried
with a real one (see docs/radio.md).
"""

from __future__ import annotations

import csv
import io
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from museion import notes

START1, START2 = 0x94, 0xC3
MAX_FRAME = 512
TCP_PORT = 4403
BAUD = 115200
BROADCAST = 0xFFFFFFFF
TEXT_MESSAGE_APP = 1  # portnums.proto
# A text message's payload limit is 233 bytes (DATA_PAYLOAD_LEN); kept
# under it with room to spare.
MAX_TEXT_BYTES = 200
MAX_PARTS = 4
# The radio drops an API client it has not heard from in a while.
HEARTBEAT_SECONDS = 300
RECONNECT_SECONDS = 10
MESSAGES = "radio-messages.csv"
MESSAGE_COLUMNS = ["time", "direction", "from", "to", "channel", "text"]


class RadioError(Exception):
    """A problem with the radio, worded for the person."""


# ── protobuf, the little that is needed ──────────────────────────────────────

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_fixed32(number: int, value: int) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<I", value & 0xFFFFFFFF)


def field_bytes(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def read_varint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(data):
            raise ValueError("truncated varint")
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode(data: bytes) -> dict[int, list]:
    """A message's fields, by number: varints and fixed numbers as ints,
    length-delimited fields as bytes. Unknown wire types end the message."""
    out: dict[int, list] = {}
    i = 0
    while i < len(data):
        key, i = read_varint(data, i)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, i = read_varint(data, i)
        elif kind == 1:
            value = struct.unpack_from("<Q", data, i)[0]
            i += 8
        elif kind == 2:
            length, i = read_varint(data, i)
            value = data[i:i + length]
            if len(value) < length:
                raise ValueError("truncated field")
            i += length
        elif kind == 5:
            value = struct.unpack_from("<I", data, i)[0]
            i += 4
        else:
            raise ValueError(f"wire type {kind}")
        out.setdefault(number, []).append(value)
    return out


def first(fields: dict, number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


# ── the messages ──────────────────────────────────────────────────────────────

def want_config(config_id: int) -> bytes:
    """ToRadio.want_config_id (3): send me the node database, then
    config_complete_id with this number."""
    return field_varint(3, config_id)


def heartbeat() -> bytes:
    """ToRadio.heartbeat (7), an empty Heartbeat."""
    return field_bytes(7, b"")


def text_packet(text: str, to: int = BROADCAST, channel: int = 0, packet_id: int | None = None) -> bytes:
    """ToRadio.packet (1): a MeshPacket with to (2), channel (3), decoded (4)
    Data{portnum (1) = TEXT_MESSAGE_APP, payload (2)}, id (6), want_ack (10)."""
    data = field_varint(1, TEXT_MESSAGE_APP) + field_bytes(2, text.encode("utf-8"))
    packet = field_fixed32(2, to)
    if channel:
        packet += field_varint(3, channel)
    packet += field_bytes(4, data)
    packet += field_fixed32(6, packet_id if packet_id is not None else random.randint(1, 0xFFFFFFFE))
    packet += field_varint(10, 1)
    return field_bytes(1, packet)


@dataclass
class Event:
    kind: str  # my_info, node, text, config_complete, other
    num: int = 0  # my_info and node: the node's number
    long_name: str = ""
    short_name: str = ""
    sender: int = 0  # text
    to: int = 0
    channel: int = 0
    text: str = ""
    rx_time: int = 0
    config_id: int = 0


def parse_from_radio(data: bytes) -> Event:
    """What a FromRadio says, as far as Museion cares: packet (2) carrying a
    text message, my_info (3), node_info (4) or config_complete_id (7)."""
    fields = decode(data)
    if 2 in fields:
        packet = decode(first(fields, 2))
        decoded = first(packet, 4)
        if decoded is None:
            return Event("other")  # still encrypted: not for a channel we have
        payload = decode(decoded)
        if first(payload, 1) != TEXT_MESSAGE_APP:
            return Event("other")
        return Event("text", sender=first(packet, 1, 0), to=first(packet, 2, 0), channel=first(packet, 3, 0),
                     text=first(payload, 2, b"").decode("utf-8", errors="replace"), rx_time=first(packet, 7, 0))
    if 3 in fields:
        return Event("my_info", num=first(decode(first(fields, 3)), 1, 0))
    if 4 in fields:
        info = decode(first(fields, 4))
        user = decode(first(info, 2, b""))
        return Event("node", num=first(info, 1, 0),
                     long_name=first(user, 2, b"").decode("utf-8", errors="replace"),
                     short_name=first(user, 3, b"").decode("utf-8", errors="replace"))
    if 7 in fields:
        return Event("config_complete", config_id=first(fields, 7, 0))
    return Event("other")


def frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ValueError("frame too long")
    return bytes([START1, START2]) + struct.pack(">H", len(payload)) + payload


class Frames:
    """Turns the bytes from the radio into frames. Anything between frames
    is the radio's debug log, printed as text before the client API starts,
    and is skipped."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(bytes([START1, START2]))
            if start < 0:
                # Keep a trailing START1: its START2 may be in the next read.
                del self.buf[:max(len(self.buf) - 1, 0)]
                return out
            del self.buf[:start]
            if len(self.buf) < 4:
                return out
            length = struct.unpack(">H", self.buf[2:4])[0]
            if length > MAX_FRAME:
                del self.buf[:1]  # not a real header: look for the next one
                continue
            if len(self.buf) < 4 + length:
                return out
            out.append(bytes(self.buf[4:4 + length]))
            del self.buf[:4 + length]


# ── the connection ───────────────────────────────────────────────────────────

class Port:
    """A byte stream to the radio: read() blocks for up to about a second and
    returns b"" if nothing came."""

    def read(self) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpPort(Port):
    def __init__(self, host: str, port: int = TCP_PORT, timeout: float = 5.0) -> None:
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise RadioError(f"no radio answered at {host}:{port} ({e})") from e
        self.sock.settimeout(1.0)

    def read(self) -> bytes:
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if not data:
            raise RadioError("the radio closed the connection")
        return data

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class PosixSerial(Port):
    """A USB serial port on Linux or macOS, raw, at 115200 baud."""

    def __init__(self, path: str) -> None:
        import termios
        import tty
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise RadioError(f"cannot open {path}: {e.strerror}") from e
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        speed = getattr(termios, f"B{BAUD}")
        attrs[4] = attrs[5] = speed
        attrs[2] &= ~termios.HUPCL  # do not reset the board on close
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def read(self) -> bytes:
        import select
        ready, _, _ = select.select([self.fd], [], [], 1.0)
        if not ready:
            return b""
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return b""
        if not data:
            raise RadioError("the radio was unplugged")
        return data

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            view = view[n:]

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class WindowsSerial(Port):
    """A COM port on Windows. Windows' own `mode` command sets the speed (and
    keeps DTR and RTS on, as other Meshtastic programs do, so a board that
    resets on them does not); the port is then an ordinary file. Reads run
    on a thread of their own, because a COM port read blocks."""

    def __init__(self, name: str) -> None:
        name = name.upper()
        flags = 0x0800_0000  # no console window
        try:
            subprocess.run(["mode", name, f"BAUD={BAUD}", "PARITY=N", "DATA=8", "STOP=1", "DTR=ON", "RTS=ON",
                            "TO=OFF"], capture_output=True, timeout=10, creationflags=flags, check=False)
            self.file = open(rf"\\.\{name}", "r+b", buffering=0)  # noqa: SIM115 -- held open for the session
        except OSError as e:
            raise RadioError(f"cannot open {name}: {e}") from e
        self.inbox: list[bytes] = []
        self.ready = threading.Condition()
        self.error: Exception | None = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                b = self.file.read(1)
                if not b:
                    raise RadioError("the radio was unplugged")
                with self.ready:
                    self.inbox.append(b)
                    self.ready.notify()
        except Exception as e:  # noqa: BLE001 -- handed to the reader
            with self.ready:
                self.error = e
                self.ready.notify()

    def read(self) -> bytes:
        with self.ready:
            if not self.inbox and self.error is None:
                self.ready.wait(1.0)
            if self.error is not None and not self.inbox:
                raise RadioError(f"the radio stopped answering ({self.error})")
            data, self.inbox = b"".join(self.inbox), []
            return data

    def write(self, data: bytes) -> None:
        self.file.write(data)
        self.file.flush()

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


def open_port(address: str) -> Port:
    """tcp:<host>[:<port>] for a radio on the network, else a serial port:
    COM5 on Windows, /dev/ttyUSB0 or /dev/ttyACM0 on Linux, /dev/cu.* on a Mac."""
    address = address.strip()
    if address.lower().startswith("tcp:"):
        host, _, port = address[4:].partition(":")
        return TcpPort(host or "127.0.0.1", int(port or TCP_PORT))
    if sys.platform == "win32":
        return WindowsSerial(address)
    return PosixSerial(address)


def serial_ports() -> list[str]:
    """The serial ports a radio could be on."""
    if sys.platform == "win32":
        import winreg
        found = []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                i = 0
                while True:
                    try:
                        found.append(str(winreg.EnumValue(key, i)[1]))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return sorted(set(found))
    import glob
    patterns = ["/dev/ttyUSB*", "/dev/ttyACM*"] if sys.platform.startswith("linux") else \
        ["/dev/cu.usbserial*", "/dev/cu.usbmodem*", "/dev/cu.SLAB*", "/dev/cu.wchusbserial*"]
    return sorted(p for pattern in patterns for p in glob.glob(pattern))


# ── the link: one radio, kept connected ──────────────────────────────────────

@dataclass
class Message:
    time: str
    direction: str  # "in" or "out"
    sender: str
    to: str
    channel: int
    text: str

    def row(self) -> dict:
        """As radio-messages.csv has it, which is how the page sees it too."""
        return dict(zip(MESSAGE_COLUMNS, (self.time, self.direction, self.sender, self.to, self.channel, self.text)))


@dataclass
class Link:
    """One radio, connected on a thread of its own and reconnected if it
    drops. Messages go to radio-messages.csv in the notes and to `on_event`
    (the page)."""

    address: str
    notes_dir: Path | None
    on_event: Callable[[str, dict], None] = lambda kind, data: None
    opener: Callable[[str], Port] = open_port
    me: int = 0
    nodes: dict[int, tuple[str, str]] = field(default_factory=dict)  # num -> (long name, short name)
    connected: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        self.port: Port | None = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True, name="radio")

    def start(self) -> Link:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            if self.port:
                self.port.close()

    def state(self) -> dict:
        return {"address": self.address, "connected": self.connected, "error": self.error,
                "me": self.name_of(self.me) if self.me else "",
                "nodes": sorted({self.name_of(n) for n in self.nodes if n != self.me})}

    def name_of(self, num: int) -> str:
        if num == BROADCAST:
            return "everyone"
        long, short = self.nodes.get(num, ("", ""))
        return f"{long or short or 'unknown'} (!{num:08x})"

    def number_of(self, who: str) -> int:
        """A node by its long or short name or its !id; everyone if empty."""
        who = who.strip()
        if not who or who.lower() in ("everyone", "all", "broadcast"):
            return BROADCAST
        wanted = who.lower().lstrip("!")
        for num, (long, short) in self.nodes.items():
            if wanted in (long.lower(), short.lower(), f"{num:08x}") or who.lower() == self.name_of(num).lower():
                return num
        raise RadioError(f"no radio called {who} has been heard; known: {', '.join(self.state()['nodes']) or 'none yet'}")

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.session()
            except (RadioError, OSError, ValueError) as e:
                self.error = str(e)
            self.connected = False
            self.on_event("radio-state", self.state())
            if self.stopping.wait(RECONNECT_SECONDS):
                return

    def session(self) -> None:
        port = self.opener(self.address)
        with self.lock:
            self.port = port
        try:
            # Wake the radio and move it from its debug log to the client API.
            port.write(bytes([START2]) * 32)
            time.sleep(0.1)
            config_id = random.randint(1, 0xFFFFFFFE)
            port.write(frame(want_config(config_id)))
            frames = Frames()
            last_beat = time.monotonic()
            while not self.stopping.is_set():
                for payload in frames.feed(port.read()):
                    try:
                        event = parse_from_radio(payload)
                    except (ValueError, struct.error):
                        continue
                    self.handle(event, config_id)
                if time.monotonic() - last_beat > HEARTBEAT_SECONDS:
                    port.write(frame(heartbeat()))
                    last_beat = time.monotonic()
        finally:
            with self.lock:
                self.port = None
            port.close()

    def handle(self, event: Event, config_id: int) -> None:
        if event.kind == "my_info":
            self.me = event.num
        elif event.kind == "node":
            new = event.num not in self.nodes
            self.nodes[event.num] = (event.long_name, event.short_name)
            if new and self.connected:
                self.on_event("radio-state", self.state())  # a radio heard for the first time
        elif event.kind == "config_complete" and event.config_id == config_id:
            self.connected = True
            self.error = ""
            self.on_event("radio-state", self.state())
        elif event.kind == "text":
            message = Message(stamp(event.rx_time), "in", self.name_of(event.sender), self.name_of(event.to),
                              event.channel, event.text)
            self.keep(message)

    def send(self, text: str, to: str = "", channel: int = 0) -> list[Message]:
        """Send a text message (in parts if it is long) and keep it."""
        text = text.strip()
        if not text:
            raise RadioError("There is nothing to send.")
        if not self.connected:
            raise RadioError(f"The radio is not connected{': ' + self.error if self.error else ''}.")
        number = self.number_of(to)
        parts = split_text(text)
        sent = []
        with self.lock:
            if self.port is None:
                raise RadioError("The radio is not connected.")
            for part in parts:
                self.port.write(frame(text_packet(part, number, channel)))
                time.sleep(0.2)
        for part in parts:
            message = Message(stamp(0), "out", self.name_of(self.me) if self.me else "this radio",
                              self.name_of(number), channel, part)
            self.keep(message)
            sent.append(message)
        return sent

    def keep(self, message: Message) -> None:
        if self.notes_dir is not None:
            try:
                keep_message(self.notes_dir, message)
            except (OSError, notes.NoteError) as e:
                self.error = f"a message could not be saved: {e}"
        self.on_event("radio-message", message.row())


def stamp(rx_time: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(rx_time or time.time()))


def split_text(text: str) -> list[str]:
    """A message cut into parts the radio can carry, at spaces where it
    can be, numbered "(1/3)" when there is more than one."""
    if len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        return [text]
    room = MAX_TEXT_BYTES - 8  # for " (1/4)"
    parts, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate.encode("utf-8")) <= room:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(word.encode("utf-8")) > room:
            cut = room
            while len(word[:cut].encode("utf-8")) > room:
                cut -= 1
            parts.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        parts.append(current)
    if len(parts) > MAX_PARTS:
        raise RadioError(f"That is too long to send by radio: keep it under about {MAX_PARTS * room} characters.")
    return [f"{p} ({i}/{len(parts)})" for i, p in enumerate(parts, start=1)]


def keep_message(notes_dir: Path, message: Message) -> None:
    """Add a message to radio-messages.csv, starting it if need be."""
    line = io.StringIO()
    writer = csv.writer(line, lineterminator="\n")
    if not (notes_dir / MESSAGES).is_file():
        writer.writerow(MESSAGE_COLUMNS)
    writer.writerow([message.time, message.direction, message.sender, message.to, message.channel, message.text])
    notes.write(notes_dir, MESSAGES, line.getvalue(), append=True, history=False)


def recent(notes_dir: Path | None, limit: int = 50) -> list[dict]:
    """The last messages kept, oldest first."""
    if notes_dir is None or not (notes_dir / MESSAGES).is_file():
        return []
    header, rows = notes.rows_of(notes.read(notes_dir, MESSAGES))
    out = [{h: (r[i] if i < len(r) else "") for i, h in enumerate(header)} for r in rows]
    return out[-limit:]


def probe(address: str, seconds: float = 4.0, opener: Callable[[str], Port] = open_port) -> str | None:
    """The name of the radio at `address`, if one answers in time."""
    try:
        port = opener(address)
    except (RadioError, OSError, ValueError):
        return None
    try:
        port.write(bytes([START2]) * 32)
        config_id = random.randint(1, 0xFFFFFFFE)
        port.write(frame(want_config(config_id)))
        frames, me, nodes = Frames(), 0, {}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for payload in frames.feed(port.read()):
                try:
                    event = parse_from_radio(payload)
                except (ValueError, struct.error):
                    continue
                if event.kind == "my_info":
                    me = event.num
                elif event.kind == "node":
                    nodes[event.num] = event.long_name or event.short_name
                if me and me in nodes:
                    return f"{nodes[me]} (!{me:08x})"
                if event.kind == "config_complete" and me:
                    return f"!{me:08x}"
        return None
    except (RadioError, OSError):
        return None
    finally:
        port.close()

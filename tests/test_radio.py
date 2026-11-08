"""The radio bridge, against a simulated Meshtastic radio.

The FromRadio and ToRadio bytes below were made with the official Meshtastic
protobufs (the meshtastic Python package, 2.7.11, mesh_pb2), so the hand-made
encoding in radio.py is checked against the real thing without Museion
depending on it. To remake them: pip install meshtastic in a scratch venv and
serialise the same messages."""

import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from museion import radio
from museion.agent_tools import all_tools
from agent_fakes import FakeRun

OFFICIAL_FROM = {
    "my_info": "08051a07088d98ac504002",
    "node_info": "0806222c0888ef99ab05121a0a09213535363637373838120948696c6c204661726d1a02484625000090402d00f15365",
    "text": "0807123e0d8877665515ffffffff1801221f0801121b427269646765206f6e204d696c6c204c616e65206973206f75742e35"
            "090300003d64f1536545000060c04803",
    "config_complete": "38b960",
    "metadata": "6a080a06322e362e3131",
}
OFFICIAL_TO = {
    "text": "0a1a15887766551802220a0801120668c3a96c6c6f35630000005001",  # "héllo" to !55667788, channel 2, id 99
    "want_config": "18b960",  # want_config_id 12345
    "heartbeat": "3a00",
}
ME, OTHER = 0x0A0B0C0D, 0x55667788


class ProtocolTest(unittest.TestCase):
    def test_encoding_matches_the_official_protobufs(self):
        self.assertEqual(radio.text_packet("héllo", OTHER, 2, 99).hex(), OFFICIAL_TO["text"])
        self.assertEqual(radio.want_config(12345).hex(), OFFICIAL_TO["want_config"])
        self.assertEqual(radio.heartbeat().hex(), OFFICIAL_TO["heartbeat"])

    def test_decoding_the_official_messages(self):
        e = radio.parse_from_radio(bytes.fromhex(OFFICIAL_FROM["my_info"]))
        self.assertEqual((e.kind, e.num), ("my_info", ME))
        e = radio.parse_from_radio(bytes.fromhex(OFFICIAL_FROM["node_info"]))
        self.assertEqual((e.kind, e.num, e.long_name, e.short_name), ("node", OTHER, "Hill Farm", "HF"))
        e = radio.parse_from_radio(bytes.fromhex(OFFICIAL_FROM["text"]))
        self.assertEqual((e.kind, e.sender, e.to, e.channel, e.text, e.rx_time),
                         ("text", OTHER, radio.BROADCAST, 1, "Bridge on Mill Lane is out.", 1700000100))
        e = radio.parse_from_radio(bytes.fromhex(OFFICIAL_FROM["config_complete"]))
        self.assertEqual((e.kind, e.config_id), ("config_complete", 12345))
        self.assertEqual(radio.parse_from_radio(bytes.fromhex(OFFICIAL_FROM["metadata"])).kind, "other")

    def test_frames_skip_the_debug_log_and_join_split_reads(self):
        one, two = radio.frame(b"abc"), radio.frame(b"\x94\xc3x")
        stream = b"DEBUG | booting\r\n\x94" + one + b"junk" + two
        f = radio.Frames()
        got = []
        for i in range(0, len(stream), 3):
            got += f.feed(stream[i:i + 3])
        self.assertEqual(got, [b"abc", b"\x94\xc3x"])
        self.assertEqual(radio.Frames().feed(b"\x94\xc3\xff\xff" + one), [b"abc"])  # a false header is skipped

    def test_long_messages_are_split_at_spaces(self):
        self.assertEqual(radio.split_text("short"), ["short"])
        parts = radio.split_text("word " * 100)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p.encode()) <= radio.MAX_TEXT_BYTES for p in parts))
        self.assertTrue(parts[0].endswith("(1/3)") and parts[2].endswith("(3/3)"))
        with self.assertRaises(radio.RadioError):
            radio.split_text("word " * 400)


class FakeRadio:
    """A simulated radio on a TCP port: answers want_config with the official
    my_info, nodes and config_complete, keeps what it is sent, and can
    deliver a message."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.received: list[bytes] = []
        self.conns: list[socket.socket] = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self.talk, args=(conn,), daemon=True).start()

    def talk(self, conn):
        # probe() hangs up as soon as it has the radio's name, mid-answer.
        with conn, contextlib.suppress(OSError):
            conn.sendall(b"DEBUG | 12:00:01 Booting\r\n")
            self.answer(conn)

    def answer(self, conn):
        frames = radio.Frames()
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            for payload in frames.feed(data):
                self.received.append(payload)
                fields = radio.decode(payload)
                if 3 in fields:
                    me = radio.field_bytes(3, radio.field_varint(1, ME))
                    mine = radio.field_bytes(4, radio.field_varint(1, ME) + radio.field_bytes(
                        2, radio.field_bytes(2, b"Museion Base") + radio.field_bytes(3, b"MB")))
                    for msg in (me, mine, bytes.fromhex(OFFICIAL_FROM["node_info"]),
                                bytes.fromhex(OFFICIAL_FROM["metadata"]), radio.field_varint(7, fields[3][0])):
                        conn.sendall(radio.frame(msg))

    def deliver(self, hex_message):
        self.conns[-1].sendall(radio.frame(bytes.fromhex(hex_message)))

    def close(self):
        self.server.close()
        for conn in self.conns:
            conn.close()


def wait_for(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.radio = FakeRadio()
        self.addCleanup(self.radio.close)
        self.notes = Path(tempfile.mkdtemp())
        self.events = []

    def test_connects_receives_sends_and_keeps_messages(self):
        address = f"tcp:127.0.0.1:{self.radio.port}"
        self.assertEqual(radio.probe(address), "Museion Base (!0a0b0c0d)")
        link = radio.Link(address, self.notes, lambda k, v: self.events.append((k, v))).start()
        self.addCleanup(link.stop)
        self.assertTrue(wait_for(lambda: link.connected))
        self.assertEqual(link.state()["nodes"], ["Hill Farm (!55667788)"])
        self.radio.deliver(OFFICIAL_FROM["text"])
        self.assertTrue(wait_for(lambda: any(k == "radio-message" for k, _ in self.events)))
        link.send("Thanks, we will use the ford.", to="HF")
        self.assertTrue(wait_for(lambda: any(radio.decode(p).get(1) for p in self.radio.received)))
        sent = [radio.decode(p) for p in self.radio.received if 1 in radio.decode(p)][0]
        packet = radio.decode(sent[1][0])
        self.assertEqual(packet[2][0], OTHER)
        self.assertEqual(radio.decode(packet[4][0])[2][0], b"Thanks, we will use the ford.")
        rows = radio.recent(self.notes)
        self.assertEqual([(r["direction"], r["from"], r["text"]) for r in rows], [
            ("in", "Hill Farm (!55667788)", "Bridge on Mill Lane is out."),
            ("out", "Museion Base (!0a0b0c0d)", "Thanks, we will use the ford.")])
        self.assertFalse((self.notes / ".history").exists())  # a log that only grows keeps no copies
        with self.assertRaises(radio.RadioError):
            link.send("hello", to="Nobody")

    def test_nothing_is_sent_before_it_is_connected(self):
        link = radio.Link("tcp:127.0.0.1:1", self.notes)
        with self.assertRaises(radio.RadioError):
            link.send("hello")
        self.assertIsNone(radio.probe("tcp:127.0.0.1:1", 0.5))


@unittest.skipIf(sys.platform == "win32", "a pseudo-terminal stands in for the USB serial port")
class SerialTest(unittest.TestCase):
    def test_a_serial_port(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        port = radio.PosixSerial(os.ttyname(slave))
        os.close(slave)
        self.addCleanup(port.close)
        sent = radio.frame(radio.want_config(1))
        port.write(sent)
        got = b""
        while len(got) < len(sent):
            got += os.read(master, 100)
        self.assertEqual(got, sent)
        os.write(master, radio.frame(bytes.fromhex(OFFICIAL_FROM["my_info"])))
        data = b""
        for _ in range(5):
            data += port.read()
            if len(data) >= 4 + len(OFFICIAL_FROM["my_info"]) // 2:
                break
        self.assertEqual(radio.Frames().feed(data), [bytes.fromhex(OFFICIAL_FROM["my_info"])])


class ToolsTest(unittest.TestCase):
    def test_send_radio_asks_and_shows_the_message(self):
        send = all_tools()["send_radio"]
        self.assertEqual(send.risk, "ask")
        self.assertEqual(send.summary({"text": "Water at noon", "to": "Hill Farm"}),
                         "send a radio message to Hill Farm:\nWater at noon")
        run = FakeRun()
        run.setting.radio = None
        from museion.agent_tools import ToolError
        with self.assertRaises(ToolError):
            send.run({"text": "hi"}, run)

    def test_radio_messages(self):
        run = FakeRun()
        run.setting.notes_dir = Path(tempfile.mkdtemp())
        run.setting.radio = None
        read = all_tools()["radio_messages"]
        self.assertEqual(read.run({}, run), "No radio is connected now. No messages yet.")
        radio.keep_message(run.setting.notes_dir, radio.Message("2027-02-01 09:10", "in", "Hill Farm (!55667788)",
                                                               "everyone", 0, "Bridge is out."))
        self.assertIn("2027-02-01 09:10 from Hill Farm (!55667788): Bridge is out.", read.run({}, run))


if __name__ == "__main__":
    unittest.main()

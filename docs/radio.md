# The radio (optional)

If you have a **Meshtastic** radio, Museion can send and receive short text
messages with it, with no phone network or internet. It is an accessory:
without one, nothing in Museion changes and nothing about it shows.

Meshtastic radios are small LoRa boards (Heltec, LilyGO T-Beam and T-Echo,
RAK WisBlock and others, from about 20 to 60 dollars) that pass messages from
radio to radio, a few kilometres a hop in the open, further from high ground.
Each household with one becomes a link in the chain. Buy and set them up
while the internet works: the radio needs its region set once (the Meshtastic
app or <https://flasher.meshtastic.org> does it) and every radio that should
talk must share a channel.

## Connecting it

1. Plug the radio into the computer by USB (or, for a board with Wi-Fi, put it
   on the same network).
2. Settings › **Radio**: press **Look for a radio**, which tries every serial
   port, or type where it is: `COM5` on Windows, `/dev/ttyUSB0` or
   `/dev/ttyACM0` on Linux, `/dev/cu.usbserial…` on a Mac, `tcp:192.168.1.40`
   for one on the network.
3. Press **Connect**. Museion remembers it, connects at every start, and tries
   again every ten seconds if the radio is unplugged or drops.

A **Radio** tab then appears: messages in and out, the radios heard, and a box
to send to everyone in range or to one radio. A long message goes as up to
four numbered parts.

On Linux your user needs to be in the `dialout` group to open a serial port.
Only one program can have the port open: close the Meshtastic app or any
serial monitor first.

## Where messages go

Every message sent and received is added to `radio-messages.csv` in your notes
(see [notes.md](notes.md)), so it is kept, searched with your notes and cited
like them: "what did Hill Farm say about the bridge?" can be answered from it.

The **Radio operator** agent reads the messages, says what matters in them
(requests for help, warnings, offers), and drafts replies. Sending is behind
an Allow / Deny card showing exactly what will go out, every time; without a
radio it gives you the message to read out another way.

Anyone with a radio on the same channel can read what you send (and a
default channel's key is public). Keep health details, and what you have
stored and where, off the air.

## How it works

`museion/radio.py` speaks the radio's own client API: frames of `0x94 0xC3`,
a two-byte length, and a protobuf message. The few messages Museion needs
(the node list, text messages, a heartbeat) are encoded by hand, so it needs no
protobuf library; the field numbers are those of
[meshtastic/protobufs](https://github.com/meshtastic/protobufs) (`mesh.proto`).
`tests/test_radio.py` checks the encoding byte for byte against messages made
with the official protobufs. The serial port is opened with Python alone:
`termios` on Linux and macOS, and on Windows the system's `mode` command
then the port as a file. `museion/radio_commands.py` is what the page calls.

## What has been tested

Against simulated radios: one in `tests/test_radio.py`, and one built from the
official Meshtastic Python protobufs over TCP, through the page (connect,
send to everyone and to one radio, replies, a long message in parts, the radio
dropping and coming back, reconnecting at start). The Linux serial path is
tested over a pseudo-terminal.

**Not yet tried with a real radio**, and the Windows COM-port path has not run
on Windows. If you have one, try it and say what happens: the first thing to
check is that Look for a radio finds it.

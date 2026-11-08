"""radio_messages: the last text messages sent and received by radio
(radio-messages.csv in the notes, kept by radio.py)."""

from museion import radio
from museion.agent_tools import READ, Tool
from museion.agent_tools._notes import folder


def run(args: dict, run) -> str:
    link = getattr(run.setting, "radio", None)
    status = ("The radio is connected" + (f"; other radios heard: {', '.join(link.state()['nodes'])}"
                                          if link.state()["nodes"] else "") + "."
              if link is not None and link.connected else "No radio is connected now.")
    messages = radio.recent(folder(run), int(args.get("count") or 20))
    if not messages:
        return f"{status} No messages yet."
    lines = [f"{m['time']} {'from ' + m['from'] if m['direction'] == 'in' else 'sent to ' + m['to']}: {m['text']}"
             for m in messages]
    return status + "\n" + "\n".join(lines)


TOOL = Tool(
    name="radio_messages",
    description="Read the latest text messages sent and received by radio, oldest first, and which radios have been heard.",
    parameters={"count": {"type": "integer", "description": "How many of the latest messages (default 20)"}},
    risk=READ,
    run=run,
    describe=lambda a: "read the radio messages",
)

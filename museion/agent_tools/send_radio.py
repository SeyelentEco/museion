"""send_radio: send a text message by radio. It leaves this computer, so the
person must press Allow every time and sees exactly what will be sent."""

from museion.agent_tools import ASK, Tool, ToolError


def run(args: dict, run) -> str:
    link = getattr(run.setting, "radio", None)
    if link is None:
        raise ToolError("No radio is connected, so nothing can be sent. Say so, and give the message to the person instead.")
    from museion.radio import RadioError
    try:
        sent = link.send(args["text"], args.get("to", ""))
    except RadioError as e:
        raise ToolError(str(e)) from None
    return f"Sent to {sent[0].to}" + (f" in {len(sent)} parts" if len(sent) > 1 else "") + "."


def describe(a: dict) -> str:
    to = a.get("to") or "everyone in range"
    return f"send a radio message to {to}:\n{a.get('text', '')}"


TOOL = Tool(
    name="send_radio",
    description=("Send a short text message by radio (Meshtastic), to everyone in range or to one radio by "
                 "its name. Keep it short and plain: it may be read on a tiny screen. The person must allow it."),
    parameters={
        "text": {"type": "string", "description": "The message"},
        "to": {"type": "string", "description": "Optional: the radio's name; leave out for everyone"},
    },
    required=["text"],
    risk=ASK,
    run=run,
    describe=describe,
)

"""How replies print: trimmed so they never flood an agent's context, with one state line at the end."""

EXIT_PROMPT, EXIT_ERROR, EXIT_NO_SESSION, EXIT_RUNNING, EXIT_EXITED = 0, 1, 2, 3, 4


# Enough for 150 ordinary lines; a single huge line (a hex dump, a giant print) is cut by characters too.
MAX_CHARS = 24_000


def trim(text: str, head: int, tail: int, hint: str) -> str:
    lines = text.split("\n")
    if len(lines) > head + tail + 5:
        cut = len(lines) - head - tail
        text = "\n".join([*lines[:head], f"[... {cut} lines cut: {hint}]", *lines[-tail:]])
    if len(text) > MAX_CHARS:
        cut = len(text) - MAX_CHARS
        text = f"{text[: MAX_CHARS // 3]}\n[... {cut} characters cut: {hint}]\n{text[-MAX_CHARS * 2 // 3 :]}"
    return text


def state_line(reply: dict) -> str:
    parts = [reply.get("session", "?")]
    state = reply.get("state")
    if state == "prompt":
        parts.append(f"stopped at {reply.get('prompt', '')}")
    elif state == "running":
        parts.append("running")
    elif state == "exited":
        parts.append("ended")
    if reply.get("reason"):
        parts.append(reply["reason"])
    if reply.get("cmd") and state != "exited":
        parts.append(f"cmd #{reply['cmd']}")
    return "[" + " · ".join(parts) + "]"


def exit_code(reply: dict) -> int:
    return {"prompt": EXIT_PROMPT, "running": EXIT_RUNNING, "exited": EXIT_EXITED}.get(reply.get("state"), 0)


def next_step(reply: dict) -> str:
    """One line on what to do next, only when the state needs it."""
    name = reply.get("session", "NAME")
    if reply.get("state") == "running":
        return f"[the target is running: `debugueira wait {name}` for the next stop, or `debugueira break {name}`]"
    if reply.get("state") == "exited":
        return f"[the session is over; its log stays: `debugueira log {name}`]"
    if reply.get("reason", "").startswith("all processes exited"):
        return (
            "[every debugged process has exited; the session stays open for `log`, `grep` and `procs`: "
            f"`debugueira stop {name}` when you're done]"
        )
    return ""

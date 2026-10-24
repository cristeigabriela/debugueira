"""Render a subagent's transcript as plain text a reviewer can read in full: every message, tool call and result.

Claude Code keeps each subagent's log as JSONL under
`~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl`. This finds it by the agent id the Agent
tool returned, so the caller needs no session path.

Usage: render_transcript.py AGENT_ID_OR_JSONL OUT.txt
"""

import json
import re
import sys
from pathlib import Path

# One tool result can be megabytes (a test agent printing 5 MB on purpose). The raw JSONL keeps it all;
# the rendered copy keeps the start and end, so the reviewer still sees what came back.
MAX_RESULT = 20_000
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


class TranscriptNotFound(Exception):
    def __init__(self, agent: str):
        super().__init__(f"no transcript for agent {agent!r} under ~/.claude/projects/*/*/subagents/")
        self.agent = agent


def find(agent: str) -> Path:
    path = Path(agent)
    if path.suffix == ".jsonl" and path.is_file():
        return path
    hits = sorted(
        (Path.home() / ".claude" / "projects").glob(f"*/*/subagents/agent-{agent}.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not hits:
        raise TranscriptNotFound(agent)
    return hits[-1]


def clip(text: str) -> str:
    if len(text) <= MAX_RESULT:
        return text
    cut = len(text) - MAX_RESULT
    return f"{text[: MAX_RESULT // 2]}\n[... {cut} characters cut by the renderer ...]\n{text[-MAX_RESULT // 2 :]}"


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block["text"])
        elif kind == "thinking" and block.get("thinking", "").strip():
            parts.append(f"[thinking] {block['thinking']}")
        elif kind == "tool_use":
            parts.append(f"[tool_use {block['name']}] {json.dumps(block['input'], ensure_ascii=False)}")
        elif kind == "tool_result":
            parts.append(f"[tool_result] {clip(text_of(block.get('content')))}")
    return "\n".join(parts)


def render(src: Path) -> list[str]:
    out: list[str] = []
    for raw in src.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = event.get("message")
        if not isinstance(msg, dict) or "system" in (event.get("type"), msg.get("role")):
            continue
        body = REMINDER.sub("", text_of(msg.get("content"))).strip()
        entry = f"===== {msg.get('role', event.get('type'))}\n{body}"
        # The opening prompt is logged twice; once is enough to read.
        if body and (not out or out[-1] != entry):
            out.append(entry)
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        src = find(sys.argv[1])
    except TranscriptNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    entries = render(src)
    dst.write_text("\n".join(entries) + "\n", encoding="utf-8", newline="\n")
    lines = sum(e.count("\n") + 1 for e in entries)
    print(f"{dst}: {len(entries)} messages, {lines} lines, from {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

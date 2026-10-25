"""Merging cdb's output and the target's own into one stream, a whole line at a time.

Both arrive in arbitrary chunks on separate pipes. Joining the chunks as they come splits lines, e.g.
`(180c.bb30): CLR exception` from cdb, then a line of the target's stderr, then ` - code e0434352`. So
each source's unfinished line is held back until it ends, or until something needs it now: a prompt,
or a reader.
"""

# The source of a newline the merge adds itself, to end another source's flushed line. It belongs to
# the merged view only: no source printed it, so it must not land in any one source's text.
BREAK = "break"


class LineMerger:
    def __init__(self):
        self._partial: dict[str, str] = {}
        self._last_char = "\n"

    def _emit(self, text: str, source: str) -> list[tuple[str, str]]:
        # A flushed partial line from the other source may still be open; never continue it.
        pieces = [("\n", BREAK)] if self._last_char != "\n" else []
        self._last_char = text[-1]
        return [*pieces, (text, source)]

    def feed(self, text: str, source: str) -> list[tuple[str, str]]:
        """Take a chunk; return the complete lines it finishes, as (text, source)."""
        buf = self._partial.get(source, "") + text
        cut = buf.rfind("\n") + 1
        self._partial[source] = buf[cut:]
        return self._emit(buf[:cut], source) if cut else []

    def flush(self, source: str | None = None) -> list[tuple[str, str]]:
        """Release held partial lines (one source's, or all of them) as they are."""
        out = []
        for src in [source] if source else list(self._partial):
            held = self._partial.pop(src, "")
            if held:
                out += self._emit(held, src)
        return out

    def held(self) -> str:
        """What's held back right now, for checks that must see it (e.g. a `wait --for` regex)."""
        return "".join(self._partial.values())

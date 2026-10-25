"""The watch TUI, driven headless: it renders a session, opens it, and comes back. Needs cdb.

These exercise the whole path (daemon, session, the two screens) the way a person would, so they catch a
screen that fails to mount or a reader that crashes on real output. Run via asyncio.run so the suite needs
no pytest-asyncio.
"""

import asyncio
import os

from conftest import requires_cdb

pytestmark = requires_cdb


async def _until(pilot, check, tries=80):
    for _ in range(tries):
        await pilot.pause(0.1)
        if check():
            return True
    return False


async def _open_and_back(app):
    from debugueira.watch import SessionScreen, SessionsScreen

    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: "w" in getattr(app.screen, "rows", {})), "the session is listed"
        row = app.screen.rows["w"]
        assert "access violation" in row.get("stop_reason", ""), "the table shows why it stopped"

        await pilot.press("enter")
        assert await _until(pilot, lambda: isinstance(app.screen, SessionScreen)), "Enter opens the session"
        log = "\n".join(strip.text for strip in app.screen.query_one("#log").lines)
        assert "crash" in log.lower(), f"the log pane shows the session's output:\n{log[:400]}"

        await pilot.press("escape")
        assert await _until(pilot, lambda: isinstance(app.screen, SessionsScreen)), "Esc goes back"


def test_watch_lists_a_live_session_opens_it_and_comes_back(dbg, targets):
    from debugueira.watch import WatchApp

    dbg.ok("start", "--name", "w", "--", str(targets["crash"]))
    dbg.ok("cmd", "w", "g")  # stops at the access violation, so there's a stop reason to show
    os.environ["DEBUGUEIRA_HOME"] = str(dbg.home)
    asyncio.run(_open_and_back(WatchApp()))


async def _watch_until_ended(app, dbg):
    from debugueira.watch import SessionScreen

    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: "w" in getattr(app.screen, "rows", {}))
        await pilot.press("enter")
        assert await _until(pilot, lambda: isinstance(app.screen, SessionScreen))
        dbg.ok("stop", "w")
        assert await _until(pilot, lambda: app.screen.ended), "the view notices the session ended"
        assert "ended" in app.screen.state_line().plain, "its state line now reads ended"


def test_watch_shows_when_the_session_it_is_viewing_ends(dbg, targets):
    from debugueira.watch import WatchApp

    dbg.ok("start", "--name", "w", "--", str(targets["hello"]))
    os.environ["DEBUGUEIRA_HOME"] = str(dbg.home)
    asyncio.run(_watch_until_ended(WatchApp(), dbg))


def _pane_text(app) -> str:
    return "\n".join(strip.text for strip in app.screen.query_one("#output RichLog").lines)


async def _watch_output(app):
    from debugueira.watch import SessionScreen

    async with app.run_test(size=(140, 40)) as pilot:
        assert await _until(pilot, lambda: "err: oops" in _pane_text(app)), "the output pane fills in"
        text = _pane_text(app)
        assert "out: first" in text and "out: second" in text, f"every session, both streams:\n{text}"
        assert "err │ err: oops" in text, f"stderr lines say so in words, not only in color:\n{text}"
        assert "ModLoad" not in text, "cdb's own text stays in the session's log"

        await pilot.press("s")  # both -> stdout
        await pilot.press("s")  # stdout -> stderr
        await pilot.pause(0.1)
        assert "out: first" not in _pane_text(app) and "err: oops" in _pane_text(app), "s narrows to stderr"

        await pilot.press("z")
        await pilot.pause(0.1)
        assert not app.screen.query_one("#sessions").display, "z zooms the output to the whole screen"

        await pilot.press("z", "enter")
        assert await _until(pilot, lambda: isinstance(app.screen, SessionScreen))
        assert await _until(pilot, lambda: "err: oops" in _pane_text(app)), (
            "the session's own view has it too"
        )


def test_watch_shows_the_programs_output_and_narrows_it_to_one_stream(dbg, targets):
    from conftest import run_until

    from debugueira.watch import WatchApp

    dbg.ok("start", "--name", "s", "--", str(targets["streams"]))
    run_until(dbg, "s", "all processes exited")  # the session stays live, its output written
    os.environ["DEBUGUEIRA_HOME"] = str(dbg.home)
    asyncio.run(_watch_output(WatchApp()))

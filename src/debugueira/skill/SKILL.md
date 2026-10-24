---
name: debugueira
description: Debug Windows programs with WinDbg's cdb through debugueira, a session manager that keeps many live debugger sessions running between your tool calls. Launch, attach to, or open a crash dump of native (PDB) or .NET programs, send raw WinDbg commands, and read the output without flooding your context. Use whenever you need a debugger on Windows, to find a crash, inspect a running process, trace child processes, or step through code.
---

# debugueira

debugueira keeps cdb sessions alive in the background. You send **WinDbg commands** and read **WinDbg's own output**. Everything is logged, so you can search a session later instead of printing everything now.

## Quickstart

```
debugueira start --name app -- C:\path\app.exe arg1   # launch; stops at the loader breakpoint
debugueira cmd app "lm m app*"                         # the module name for breakpoints (native: the exe's name)
debugueira cmd app "bp app!parse_header"               # any WinDbg command line
debugueira cmd app g                                   # runs to the next stop (or --timeout, default 60 s)
debugueira cmd app "k; r rcx, rdx"                     # where it stopped; at a function's entry, args are in registers
debugueira stop app                                    # always stop when done
```

**Sessions are shared by everything running as your user**, other agents included. Always pass `--name` with a name of your own (a common prefix helps: `history --name myprefix`); `list` may show sessions that aren't yours, so leave those alone.

Every reply ends in a state line, like `[app · stopped at 0:000> · breakpoint 0 · cmd #4]`, `[app · stopped at 4:005> · access violation (first chance) in child 4: worker.exe 3 · cmd #2]`, `[app · running · cmd #5]` or `[app · ended · target killed (pid 1234)]`. Your first command is #1. The `start` and `attach` replies have no number yet; they name the target's pid instead, in both bases (`pid 4660 (0x1234)`), since cdb prints pids in hex and `tasklist` in decimal.

## Commands

| Command | Does |
|---|---|
| `start --name N -- app.exe args` | Launch under cdb. The target gets its own stdin, and its output shows inline. |
| `attach --name N --pid P` / `--process app.exe` | Attach. `stop` detaches, and the process keeps running. `--noninvasive`: read-only, no breakpoints or stepping; its threads stay suspended until `stop`. |
| `peek --pid P "line"` | One-shot: attach noninvasively, run one command, detach. The quickest way to read a running process. |
| `open --name N crash.dmp` | Open a dump. |
| `cmd N "line"` | Run one WinDbg command line. Refused while the target runs. |
| `wait N [--for REGEX]` | Wait for the next stop, or until output matches REGEX (the state line says whether it matched). |
| `break N` | Break into a running target. |
| `input N "text"` | Send a line to a launched target's stdin (works while it runs). |
| `new N` | Output printed since your last reply, e.g. the target's output while it ran. |
| `procs N` | Every process the session debugs: number, pid, state or exit code, command line. |
| `stop N... [--kill \| --detach]` | End sessions. Default: kill a launched target (with its children), detach from an attached one. A launched target can't be detached: it lives in its session. |
| `list` / `status N` | Live sessions / one session as JSON. |
| `events [--session N] [--since 10m] [-n N]` | What happened across all sessions: each one starting and ending, each process starting, exiting or crashing. Handy after a session ends to see why. |
| `log N [--cmd K] [--tail L]` | Read the log: one command's full output, or the end. |
| `grep N REGEX [-C 2] [--cmd K]` | Search the raw log; hits print as `#cmd:line: text`. |
| `query N 'python'` | Run Python over the session; only what it prints comes back (below). |
| `note N "finding"` | Leave a finding on the session, for later. |
| `history [--name PREFIX] [--target X] [--grep TEXT] [--json]` / `history show ID` | Past sessions; one session's commands (with each stop's reason), outcome and note. |
| `doctor` / `gc` | Check the setup / list cdbs a crashed daemon left behind. |

`log`, `grep`, `query`, `note` and `history show` also take a finished session's name or its id from `history`.

## Keep your context small

Replies drop cdb's noise (banners, NatVis, symbol-path tables, SOS extension loads, CLR notifications), fold runs of `ModLoad:` lines into one, and keep only the first 60 and last 90 lines of long output. The `start` reply keeps the symbol search path, which helps when symbols fail. Don't reach for `--full` first:

- **One command's full output:** `debugueira log app --cmd 12 --full`
- **Find a line:** `debugueira grep app "Access violation" -C 3`
- **Compute the answer instead of reading:** `query` gives you `s` (the session), `Out`, `hexint` and `re`:

```
debugueira query app "print(s.cmd('lm').grep('deferred|export'))"
debugueira query app "print([l for l in s.cmd('~*k 1').lines if 'mylib!' in l])"
debugueira query app "print(s.cmd('!peb').grep('CommandLine'))"
```

| Name | What it is |
|---|---|
| `s.cmd(line)` | Runs a command (live sessions only) and returns its output as `Out`. |
| `s.last` | The previous command's output, as `Out`. |
| `s.log` | The whole transcript; reads like `Out` (`s.log.grep(...)`). Also `s.log.output(K)` for command #K. |
| `s.meta` | The session's record: target, start and end, end reason, note. |
| `Out` | A `str` with `.lines`, `.grep(rx)`, `.between(start_rx, end_rx)` and `.cols()` (rows split on whitespace). |
| `hexint(text)` | `00007ff6\`12345678` or `0x1f` to an int. |

What a snippet prints is trimmed like any reply (`--full` to keep it all), and one that runs past `--timeout` (default 120 s) is stopped.

## Running and stepping

- **`cmd app g` returns at the next stop.** If the target still runs at `--timeout`, you get `running` (exit code 3: normal, not an error). Then `wait app` (optionally `--for REGEX`), or `break app`. Options go after the arguments: `cmd app g --timeout 5`.
- **`wait --for REGEX` looks at output that arrives after your last reply,** so a line you've already seen won't match again.
- **`break` stops in cdb's own break-in thread** (`0:001> ntdll!DbgBreakPoint`), for launched and attached targets alike. `~*k` shows every thread; `~0s` switches to the main one.
- **While it runs, `cmd` is refused.** Use `wait`, `break`, `input`, `new`, `procs`, `log` or `grep`.
- **One execution command per `cmd`.** In a chain like `p; p; dv`, cdb doesn't print what each step did. Run `p`, `t`, `gu` and `g` on their own.
- **Locals at a function's first instruction are garbage:** the prologue hasn't stored the arguments yet. Read them from registers (`r rcx, rdx, r8, r9` on x64), or step past the prologue (`p` a few times, or `bp` on a source line with `` bp `app.c:42` ``).
- **A function's return value:** `gu` runs to where it returns, then `r rax` (the low 32 bits for an `int`). It works for managed methods too.
- **Never send cdb's own `q`, `qd`, `.shell` or an empty line.** debugueira refuses them: `stop` ends a session properly, and `.shell` would wait for console input forever.
- **Exit codes:** 0 stopped, 3 running, 4 session ended, 2 no such session, 1 error.

## A program that reads stdin

1. `start --name app -- app.exe`, then `cmd app g --timeout 3`: it comes back `running` while the program waits for input.
2. `input app "a line"`: the reply includes what the program printed in the next second (`--wait SECONDS` for longer); later output comes with `new app`. If the line makes it stop (say, at a breakpoint), the reply names the stop.
3. `break app`, then `~*k`: thread 0 shows where it's blocked (e.g. `fgets` → `ReadFile`).
4. `g --timeout 3`, more `input`, and finally the program's own quit command (or `stop`).

## Attaching

- **The first stop is in cdb's own break-in thread** (`0:001> ntdll!DbgBreakPoint`). Switch to the thread you care about first: `~*k` lists every thread's stack, `~0s` switches to thread 0.
- **`bp mod!func` says `Couldn't resolve`:** run `.reload` first, or use `bu mod!func` (deferred until the module loads).
- **A breakpoint on a function that's already running never hits,** such as the loop the process sits in: its entry won't run again. Break inside the loop instead. `uf mod!func` lists its instructions; then `bp mod!func+0x1c` on one in the loop body.
- **To read a function's locals:** find its frame with `k`, then `.frame N; dv /t /v`. `? local` prints the address; `?? local` (C++ expression) prints the value.
- **To read a global:** `? dwo(mod!g_var)` for 4 bytes, `? qwo(...)` for 8, or `dd mod!g_var L1`. `poi()` reads pointer size (8 bytes), which is wrong for an `int` or a `long`.
- **`--noninvasive` (and `peek`) don't take control:** no breakpoints, stepping or events, and the first stop is a `Wake debugger - code 80000007`. The threads stay suspended while the session is open, so two reads in one session see the same values.
- **To check it's still making progress:** `peek` twice, a few seconds apart: `debugueira peek --pid P "? dwo(mod!g_counter)"`.
- **`stop` detaches** and says so: `detached from pid P; it keeps running`.

## Child processes

Launched and attached targets follow their child processes by default (`--children all`). Each child gets its own process number in the prompt (`2:005>`). `|` lists them, `|2s` switches, and `procs` shows them all with command lines and exit codes.

- **Each child's start is shown inline,** as `[debugueira: child 4 (pid 0x3ef4) started: "worker.exe" 3]`. Its exit is logged as `Last event: 3ef4.2a10: Exit process 4:3ef4, code b`.
- **`--children none`:** debug only the target. **`--children "worker*.exe,node.exe"`:** follow only matching images; the rest are detached, and the reply says so.
- **`--break-on-child`:** stop when a followed child starts, so you can set breakpoints in it first.
- **A crash in a child stops the whole session, inside that child,** and the state line names it. After a second-chance exception, `g` re-runs the faulting instruction. To let the rest continue:
  - **`gn`** lets the crash end that process, as it would outside a debugger: its parent sees the crash's own exit code (`0xC0000005`). Use it when the program reacts to its children's exit codes.
  - **`.kill`, then `g`,** ends the process too, but with `.kill`'s exit code. `procs` shows `exit 0x80004005 (killed after c0000005)`, and the parent sees `0x80004005`.
- **When the last process exits,** the state line says `all processes exited` with the target's exit code, and every later one says it too. A further `g` prints cdb's `No runnable debuggees` error, which is normal. The session stays open for `log`, `grep` and `procs` until you `stop` it.

## .NET

SOS loads by itself when the runtime loads. A .NET app's `.exe` shows up as `apphost.exe` (the native launcher), and a first-chance `Unknown exception - code 04242420` at startup is normal.

**Managed breakpoint recipe:**
1. `sxe ld:coreclr`, then `g`: stops once the runtime is loaded (`module loaded: coreclr.dll`).
2. `!bpmd MyApp.dll MyNamespace.MyType.Method`: the module is the managed **assembly** (`.dll`), never the `.exe`. A wrong module still says `Adding pending breakpoints...` and then never hits.
3. `g`: expect `JITTED ...`, `Setting breakpoint: ...`, then `Breakpoint 0 hit`.
4. `!clrstack -a`: the managed stack with parameter values (`x (<CLR reg>) = 0x15`).

**At a managed exception:** `!pe` answers "what was thrown" in one go (type, message, stack). Then `!clrstack` and `!dso`. An unhandled exception first shows as `CLR exception - code e0434352 (first chance)` and stops at the second chance.

## At a crash

`.lastevent; k; r` shows what happened and where. Then `.frame N; dv /t` for locals, and `!analyze -v` for WinDbg's own analysis (slow, and long: grep it). In `k`, a caller's line is where the call returns to, usually one past the call itself.

## Crash dumps

- **Write one** from a stopped session: `cmd app ".dump /ma C:\dumps\app.dmp"`. cdb calls it a "mini user dump" even with `/ma`; `/ma` means full memory.
- **Open one:** `open --name d C:\dumps\app.dmp`. The state line names the exception it holds. Then `.ecxr; k` (the crash's own context) and `.exr -1` (the exception record).
- **Symbols:** a dump has no exe folder to look in, so pass `--sym C:\pdbs` when the PDBs aren't at the path they were built at.
- **A truncated dump** (a cut-off copy or upload) opens all the same, so the reply says so: the state line reads `dump (truncated): ...`, and a note gives how many bytes are there. Memory past the cut reads as `??`, and stacks can end early, so don't trust what's missing.

## Lifecycle

- **Always `stop` when you're done.** An idle session ends by itself after 30 minutes (`--idle MIN` changes it). Launched targets are killed and attached ones detached.
- **Before leaving an attached target running for a while, run `bc *`.** If debugueira itself is killed mid-session, cdb dies without detaching: an attached target that sat stopped stays frozen, and one that ran with breakpoints can keep a stray `int 3`. A clean `stop` (or the idle limit) never does this. To repair it, see "A frozen attached target" below.
- **The daemon starts on the first command and exits when idle.** You never manage it.

## When something's off

- **No symbols:** PDBs next to the exe load automatically. Otherwise use `--sym C:\pdbs` (repeatable), then `.reload /f` and `lm`.
- **"no session named":** `debugueira list`, or `debugueira history` for finished ones.
- **Setup problems:** `debugueira doctor`.
- **A frozen attached target** (history says `daemon died`, and it makes no progress): attach again and run `~`. Under a session, a healthy thread reads `Suspend: 1`. Resume each thread that reads more with `~Nm` (`~*m` for all), until each reads 1. Then `!chkimg -f MODULE` restores any stray `int 3` in that module: run it for each module that had breakpoints, usually the program's own (`!chkimg -f app`). Then `g`: if it stops at a leftover break exception, that's the event cdb died on. Then `stop`.

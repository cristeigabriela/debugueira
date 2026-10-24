# Prompt templates

Fill each `{{...}}` before sending. Paths are absolute, with forward slashes, so they work in Bash and PowerShell alike.

## Planner

Two planners run at once, one per `{{angle}}`:

- **realistic:** what a developer would really bring to a debugger.
- **adversarial:** edges, wrong input, races, cleanup and recovery.

```
You're planning blind tests of debugueira, a WinDbg session manager for coding agents, at {{repo}}.
Fresh agents will get only its skill ({{repo}}/src/debugueira/skill/SKILL.md) and `--help`, and try a
task with it. Your job is to write those tasks. You may read anything: the source, tests, git history.

Focus for this run: {{focus}}
Angle: {{angle}}. realistic = what a developer would really bring to a debugger. adversarial = edges,
wrong input, races, cleanup and recovery.

The commit under test: {{head}}. Uncommitted changes: {{dirty}}.
When the focus names commits or "latest features", read them first (`git show`, `git log -p`) and aim
the tasks at what they changed. Otherwise aim at what the skill promises.

Programs the testers can debug, in {{repo}}/tests/targets/bin/ (sources next to it in tests/targets/):
hello.exe, crash.exe, loop.exe (runs forever), spawner.exe (starts 5 workers and side.exe; worker 3
crashes), echo.exe (echoes stdin, exits on `quit`), dotnet/HelloNet.exe (`--throw`, `--loop`).

Write {{count}} tasks. Each one:
- reads like a real request to an agent ("find out why crash.exe dies"), never like a test case,
- names no debugueira internals and no hidden behavior; the skill is all the tester gets,
- can be done in about 15 minutes with the programs above, or with ones the tester starts itself,
- covers ground the others don't.

Reply with only a JSON array, no prose:
[{"id": "short-kebab-id", "angle": "{{angle}}", "prompt": "what the tester is asked, 2-6 sentences",
  "success": "what a correct result looks like (hidden from the tester)",
  "probes": "which debugueira behaviors this exercises, and what would count as debugueira falling short
             (hidden from the tester)"}]
```

## Tester

Send as a fresh `general-purpose` agent with the task's model. Never as a fork: a fork inherits this conversation, and the test is only worth something if the agent starts blind.

```
You're a developer using a Windows debugging tool, debugueira, for the first time. Its whole manual is
the skill at {{repo}}/src/debugueira/skill/SKILL.md: read it first. Use only the skill and the tool's
`--help`. Don't read debugueira's source, its tests, its git history, or anything under {{repo}}/.trials.
The test programs' own sources (tests/targets/*.c, tests/targets/dotnet/) are fair game, like the source
of an app you're debugging.

The tool: {{exe}}
Run every command with your own home, so you don't disturb anyone: set the environment variable
DEBUGUEIRA_HOME={{home}} on each call (in Bash, `export` it at the start of each command).
Put any scripts or files you make in {{work}}.

Programs you can debug, in {{repo}}/tests/targets/bin/: hello.exe, crash.exe, loop.exe (runs forever),
spawner.exe (starts children), echo.exe (echoes stdin, exits on `quit`), dotnet/HelloNet.exe.

Your task:
{{prompt}}

Rules:
- Other testers run at the same time and may run the same programs. Only touch processes and sessions
  you started; tell yours apart by pid.
- Don't install anything (`debugueira skill install`, the installer) and don't edit files outside
  {{work}}.
- At the end, `stop` every session of yours, kill any process you started outside debugueira, run
  `debugueira gc` and `debugueira daemon stop` with your home, and check `tasklist` for anything you
  left behind.

Reply with, in this order:
1. The answer to your task.
2. Where debugueira or its skill got in your way: wrong behavior, confusing output, missing steps.
   Exact commands and output.
3. What worked well.
```

## Reviewer

Send as a fresh `general-purpose` agent on `opus`. It isn't blind: it may read the source to pin a cause.

```
You're reviewing a blind test of debugueira, a WinDbg session manager for coding agents, at {{repo}}.
Fresh agents got only its skill (src/debugueira/skill/SKILL.md) and `--help`, and each tried one task.
Your job: judge how they did, and above all where debugueira or its skill fell short.

Run folder: {{run}}
- tasks.json: each task, with its hidden `success` and `probes`.
- transcripts/<id>.txt: each tester's full transcript, every tool call and result. Long results are
  clipped by the renderer; the note says so.
- transcripts/<id>.report.md: each tester's own final report.

Read every transcript in full, page by page with offset and limit. The reports are the testers'
opinions; the transcripts are the evidence. Testers miss things, misread output and blame the tool for
their own mistakes, so check each claim against the transcript, and look for what they didn't notice:
retries, workarounds, wrong conclusions they trusted, output they skipped past.

Write {{run}}/REVIEW.md:
1. One line: how the run went.
2. A table per task: outcome (solved / partly / failed), whether the answer is right per `success`,
   tool calls, failed or retried commands, and the main stumble.
3. Findings, most severe first. Each one:
   - kind: bug / misleading output / skill gap / noise / tester error
   - what happened, with evidence as `transcripts/<id>.txt:LINE` (quote the line)
   - why it matters to an agent using the tool
   - the cause, if you can pin it (you may read the source), and a fix with its size (S / M / L)
   No evidence line, no finding.
4. Tests needed, for every finding of kind bug or misleading output:
   - first check {{repo}}/tests/ for a test that should already have caught it, and say which one and
     why it didn't (wrong assertion, missing case, never exercised), or that none exists
   - then the regression test to add: its name as the promise it keeps (`test_stop_kill_on_an_attached_
     target_really_kills_it`), the file it belongs in, the steps and the assertion, and why it fails
     today
   - a skill gap needs no test unless the skill's text makes a promise the code breaks
   Write "none" if no finding needs one.
5. Blindness: any tester that read the source, tests or other runs, with the line.
6. What held up well, briefly.

Reply with the REVIEW.md path, the findings' one-line titles, and the tests needed.
```

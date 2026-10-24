---
name: blind-trial
description: Blind-test debugueira with fresh agents. Planner agents write realistic and adversarial tasks from a focus, blind testers do them with only the skill and --help, every transcript is saved, and a reviewer agent reads them all in full to find where debugueira or its skill fell short, and which regression tests each bug needs. Use for "/blind-trial", "/blind-trial try the latest commit's features with blind agents", "blind test debugueira", "run agent trials", or "see how fresh agents do with X". Runs straight through without asking.
argument-hint: "[focus, e.g. the latest commit's features] [N agents] [models sonnet,opus]"
---

# Blind trial

**Fresh agents try debugueira knowing only its skill, and a reviewer reads every move they made.** It finds what unit tests can't: the step an agent misreads, the message it can't act on, the recipe the skill forgot.

**It runs straight through.** The arguments are the whole brief: don't ask anything, don't confirm a plan. With no arguments, it runs a broad sweep of everything the skill promises.

## Arguments

All optional, in any order:

| Part | Example | Default |
|---|---|---|
| Focus, free text | `try the latest git commit's features`, `attach and peek`, `.NET` | everything the skill promises |
| Tester count | `3 agents`, `-n 3` | 5 |
| Models | `models sonnet,opus,haiku` | sonnet and opus, alternating |

A focus that names commits ("the latest commit", "the last 3 commits", "what changed since v0.1.0") means the planners read those commits and aim the tasks at what changed.

## Steps

The templates for every agent are in `prompts.md`, next to this file. Fill every `{{...}}`.

### 1. Set up the run (yourself)

1. **Repo:** the debugueira checkout this skill lives in. Use absolute paths with forward slashes.
2. **Build what the testers use:** `uv sync`, then `uv run python tests/targets/build.py`. The tool is `<repo>/.venv/Scripts/debugueira.exe`, which runs the working tree as it is, uncommitted changes included.
3. **Check cdb works:** `debugueira doctor` with `DEBUGUEIRA_HOME` set to a fresh folder. If cdb is missing, stop and say so; nothing else can run.
4. **Make the run folder:** `<repo>/.trials/<yyyymmdd-hhmmss>-<focus-slug>/`, with `homes/`, `work/` and `transcripts/` inside. `.trials/` is gitignored.
5. **Record what's under test** in `brief.md`: the focus as given, `git log -1 --oneline`, and `git status --short`.

### 2. Plan the tasks (2 planner agents)

Launch both planners in one message, as fresh `general-purpose` agents on `opus`: one `realistic`, one `adversarial`. Each writes `count` tasks, so there's room to pick.

Then pick `count` tasks yourself:
- **At least one adversarial,** and at least one realistic when `count` is 2 or more.
- **No two covering the same ground.**
- **Assign models** in turn from the model list.

Save them as `tasks.json` (the planners' fields plus `model`), and add the task list to `brief.md`.

### 3. Run the testers (N fresh agents, in parallel)

For each task, make `homes/<id>/` and `work/<id>/`. Then launch every tester in **one message**, as fresh `general-purpose` agents with the task's model.

- **Never fork.** A fork inherits this conversation, and the test is only worth something if the agent starts blind.
- **Record each agent id against its task** in `agents.json` as soon as it launches.
- **Don't nudge a running tester.** Its confusion is the data.

### 4. Save each transcript (as each tester finishes)

```
uv run python .claude/skills/blind-trial/render_transcript.py <agent-id> <run>/transcripts/<id>.txt
```

Save the tester's final report as `transcripts/<id>.report.md`.

When every tester is done, check for leftovers. For each home, run `debugueira list` and `debugueira gc`. Check `tasklist` for the test programs and `cdb`. End only what a tester's home or work folder ties to this run, and note anything left behind as a finding.

### 5. Review (1 reviewer agent)

Launch one fresh `general-purpose` agent on `opus` with the reviewer template. It reads every transcript in full and writes `REVIEW.md` in the run folder. For every bug it finds, `REVIEW.md` also names the regression test to add, and the existing test (if any) that should have caught it.

### 6. Check the review, then report (yourself)

**Check every finding before it reaches the user.** Open each cited `transcripts/<id>.txt:LINE` and confirm the line shows what the finding claims. Drop a finding the line doesn't support, or mark it "unconfirmed". Read the source for a claimed cause when it's cheap to check.

**Check the tests needed, too.** For each one, grep `tests/` yourself: if an existing test already covers the case, say so instead of proposing a duplicate. A proposed test must fail today, so name what in the transcript shows the bug it would catch.

Then reply:
- **BLUF:** one line on how the run went, and the run folder path.
- **A table per task:** id, model, outcome.
- **The findings,** numbered, most severe first, each with its kind, a one-line evidence pointer and the fix size.
- **Tests needed:** a table of the finding number, the test's name, its file, and the existing test that missed it (or "none"). Say "no new tests needed" when no bug turned up.

**Don't fix anything.** Wait for the user's call on each number.

## The run folder

```
.trials/20261024-153000-latest-commit/
  brief.md            focus, commit, dirty files, the task list
  tasks.json          tasks with their hidden success and probes, and each tester's model
  agents.json         agent id per task
  homes/<id>/         each tester's DEBUGUEIRA_HOME
  work/<id>/          each tester's own scripts and files
  transcripts/<id>.txt        the full transcript
  transcripts/<id>.report.md  the tester's own report
  REVIEW.md           the reviewer's analysis, with the regression tests each bug needs
```

Old runs stay until someone deletes them. A later run can compare against an earlier `REVIEW.md`.

## Cost

Each tester uses roughly 100-150k tokens, and the reviewer reads every transcript in full. A default run (2 planners, 5 testers, 1 reviewer) comes to roughly a million tokens. Say the tester count and models in the first line of the run, so the user sees the cost while it's still cheap to stop.

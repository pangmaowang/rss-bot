# Repo Working Agreement

Non-trivial changes go through `Plan -> Exec -> Independent Evaluator`. Keep the
implementation minimal unless asked otherwise.

## Workflow

### 1. Plan

- Read the goal, the relevant docs, the code, the tests, and `git diff/status`
  first. Protect uncommitted work belonging to the user or other agents.
- Write down the success conditions, the files involved, the risks, and a
  `step -> verify` list. Do not touch code while planning.
- When something is ambiguous but safe to infer, state the assumption and keep
  going. Ask only when the answer changes behaviour or scope.

### 2. Exec

- One agent writes files. Every changed line should trace back to the goal.
- For a bug or a behaviour change, leave a failing check first, then make the
  smallest fix. For a refactor, pin the current behaviour first.
- Match the surrounding style. No drive-by refactoring, reformatting, or
  deletion of unrelated code.
- Record the change, the verification, and any remaining risk in `CHANGELOG.md`.

### 3. Independent Evaluator

- Use a separate sub-agent. Give it the goal, the success conditions, and the
  diff, and let it read the code and run the checks itself.
- The evaluator does not edit files. It reports actionable findings as
  high/medium/low, or says `PASS` when there are none.
- Judge each finding on whether it holds and whether it is in scope. If it
  does, go back to Plan/Exec and send the fix to the same evaluator. If it
  does not, reply with evidence and ask for a re-read. Loop until `PASS`, or
  report the blocker you cannot get around.

## Minimal Engineering

- Ask whether it needs to exist. Prefer deleting, then the stdlib or a native
  feature, then an installed dependency, and only then new code.
- No interface, factory, or configuration layer for a single implementation.
  No scaffolding for a hypothetical later.
- Fewest files, shortest readable diff. No caching, concurrency, or
  performance machinery without a measurement that calls for it.
- Never simplify away error handling that prevents data loss, input
  validation, security measures, or anything explicitly requested.
- Non-trivial branching, looping, or parsing logic leaves at least one runnable
  check behind. Small changes do not turn into test projects.

## Verification

Pick the smallest sufficient check for the risk. In this repo that usually means:

```sh
.venv/bin/python test_grab.py
.venv/bin/python -m py_compile reader.py grab.py test_grab.py
git diff --check
```

Final reports contain four things: result, changes, verification, remaining risk.
Before changing the storage or fetching layer, read the invariants in
[`design/reader.md`](design/reader.md); breaking them marks the whole database
unread.

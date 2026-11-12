# Checklist format

This is the format `python3 dag/src/tm.py import <checklist.md>` accepts. A malformed file imports nothing:
every problem prints as `<file>:<line>: <message>`, and the command exits 1.

A checklist is an intake format only. Import reads the file once and never writes it back; after that the
database is the source of truth and the page is where people read it.

## Parts, in order

1. `## Tasks`: the task lists, one `### M<k>. <title>` heading per milestone, each followed by its task lines.
2. `## Context` (optional): the background and the decision log.
3. Any other `## <heading>` part (optional), such as `## Open questions`; we keep it verbatim.
4. One `## <n>. <title>` section per task.

## Task lines

A task line reads `<n>. - [ ] (agent) <title>` or `<n>. - [ ] (user) <title>`.

- `<n>` is the task id: a number, unique in the file, never reused. Ids need not be in order.
- `(agent)`: a headless worker runs it. `(user)`: only the user can do or decide it; no agent ever closes it.
- A closed task reads `<n>. - [x] (agent) <title> (<reason>)`. The reason is optional.
  - A reason that starts with `skipped`, `superseded` or `obsolete` sets that status; any other reason means done.
- Title prefixes route an `(agent)` task away from the workers, to the orchestrator session:
  - `[orch]`: needs a tool a headless worker lacks (an MCP connector, a browser, a login) or the orchestrator's judgement;
  - `[opus]`: needs stronger reasoning; the orchestrator dispatches an Opus sub-agent for it.
  - Use neither when a worker with a shell and file access can do the task.
- Lines under a milestone heading that are not task lines are kept as that milestone's note.

## Task sections

A section starts at `## <n>. <title>` and holds these labels, each alone on its line, in this order; any may be left out:

- `Why:` the reason the task exists, in a line or two.
- `What:` the output: the files the task writes or the state it leaves.
- `Details:` the steps, the inputs to read, the commands to run, limits, and the time estimate.
- `Done when:` a check a reviewer can run and compare, such as a command and its expected output.
- `Evidence:` what the result must show: paths, command output, counts.
- `Result:` filled in when the task closes; leave it out when writing a checklist.

Every open `(agent)` task needs a section; a `(user)` task may have one. The section title may differ from the task line's.

## Dependencies

A line `Depends: 3, 4.` in Details makes the task wait until tasks 3 and 4 are closed. `Depends: none.` and no line both mean none.

- A range works: `Depends: 3 to 6.`. The prose form `Depends on 3 and 4.` is read too.
- `tm.py render`, the optional export, writes the `Depends:` line as the last line of Details, so an export imports to the same DAG.
- A dependency on an id with no task line is an error.

### Building the dependencies

Import reads only the `Depends:` lines: a checklist without them imports as a DAG where every task is ready at once.
Whoever writes or imports a checklist builds them first, by reading each task's What and Details.

- A task depends on each task whose output it reads: a file, or results named in prose (`Inputs: results of tasks 3 and 4`).
- A task depends on the task that creates what it works in: a clone, a worktree, a branch, an environment.
- A `(user)` review depends on the task that produces what the user reviews; a task that needs the answer depends on the `(user)` task.
- A summary or merge task depends on every task it reports on.
- A task that removes something (a worktree, a branch, temp files) depends on the last task that can still need it.
  - That includes a review that may reopen earlier tasks.
- Name direct dependencies only; a task waits on the rest through them.
- Check after import: `tm.py ready` lists only tasks that can start with nothing else done.
  - Two or more open tasks and no `Depends:` line anywhere almost always means the lines are missing.

## Context

Prose (and code blocks) before the first top-level `- ` item is kept verbatim as the intro. Each top-level `- ` item
after it, with its nested lines, is one decision-log entry; `tm.py context "..."` appends more.

## Sizing rules

- One change per task: one output, or one tightly bound set of outputs.
- At most 30 min of work per task, 15 min or less as the rule of thumb; split larger work into parts plus a merge task.
  - Write the estimate in Details as `Time budget: estimate <n> min`. At 15 or less, the scheduler skips its split check.
  - An estimate over 30 min fails `tm.py lint` unless Details hold `exception: <reason>`.
- Name the inputs (files to read, commands to run) and the outputs (files to write) by path.
  - A path in What that the task only reads must exist or be named by a dependency, or `tm.py lint` fails the task.
- Write a Done when a reviewer can check without redoing the work.
- Add a dependency only where a task reads another task's output, so independent tasks run in parallel.
- Make a decision only the user can take its own `(user)` task, and make the tasks that need the answer depend on it.
- List entries are at most 200 characters; split longer ones into nested items.

## Example

The block between the markers imports cleanly; `src/test_import.py` checks it.

<!-- example:start -->
~~~markdown
## Tasks

### M1. Measure
1. - [ ] (agent) Count the lines of each source file
2. - [ ] (agent) Find the three largest files

### M2. Report
3. - [ ] (agent) Write `size_report.md`
4. - [ ] (user) Approve the report

## Context

We want a one-page view of where the code is, before deciding what to split.

- Decision: count lines, not bytes (user choice; rejected: bytes).
- Every result file goes in `dag_data/runs/`; the report goes in the root.

## 1. Count the lines

Why:
The report needs one number per file.

What:
`dag_data/runs/task_1_result.md`: one `<lines> <path>` line per `.py` file under `src/`.

Details:
- Run `wc -l src/*.py` and copy its lines, without the total.
- Time budget: estimate 5 min.

Done when:
The result has as many lines as `ls src/*.py | wc -l` prints.

Evidence:
The `wc -l` output.

## 2. Find the largest files

Why:
The report leads with the files most worth splitting.

What:
`dag_data/runs/task_2_result.md`: the three paths with the most lines, largest first.

Details:
- Input: `dag_data/runs/task_1_result.md`.
- Time budget: estimate 5 min.
Depends: 1.

Done when:
`sort -rn dag_data/runs/task_1_result.md | head -3` names the same three paths in the same order.

Evidence:
The sort output.

## 3. Write the report

Why:
The user reads one file, not the raw counts.

What:
`size_report.md` in the root: the total, the table of files, and the three largest with one sentence each.

Details:
- Inputs: `dag_data/runs/task_1_result.md` and `dag_data/runs/task_2_result.md`.
- Time budget: estimate 10 min.
Depends: 1, 2.

Done when:
Every path in the task 1 result appears in the table, and the total equals the sum of its line counts.

Evidence:
Path of `size_report.md` and the two numbers compared.

## 4. Approve the report

What:
The user reads `size_report.md` and approves it or asks for changes.

Details:
Depends: 3.
~~~
<!-- example:end -->

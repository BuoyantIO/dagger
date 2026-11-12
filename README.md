# The task DAG: how it works and how to run it

We run a project's work as a DAG of small tasks in SQLite. Headless `pi` workers execute, review and close each
task; two helpers keep it moving; the orchestrator (a Claude session) handles what they cannot. Everything
lives in this folder (code in `src/`, the database in `db/`, shell scripts in `scripts/`, records and runtime state
in `runs/`), and the folder runs unchanged in any project: the project root is always its parent.
The live page is where people read the DAG; no checklist file is maintained.

## Start here

From the project root, with this folder installed as `dag/` (see the next section):

```sh
dag/scripts/dag_orch.sh --checklist <path>   # a checklist in the format of dag/CHECKLIST_FORMAT.md
dag/scripts/dag_orch.sh --spec <path>        # a spec file: the orchestrator drafts the checklist from it
dag/scripts/dag_orch.sh --task "<text>"      # a task in words: the orchestrator drafts the checklist from it
dag/scripts/dag_orch.sh                      # no intake: take over a DAG that exists, or be asked which start you want
```

- The launcher starts `unblock.py` and the page (http://127.0.0.1:8766) if they do not run for this root, then opens a Claude session.
  - The session gets `ORCHESTRATOR_PROMPT.md`, plus one `Intake:` line when a flag is given; it starts the scheduler itself.
- For a spec or a task, the session writes the draft to `dag/runs/intake_checklist.md`, shows the milestones and tasks, and imports on a yes.
- By hand, a checklist needs no session: `python3 dag/src/tm.py init`, then `python3 dag/src/tm.py import <checklist.md>`.
- Every intake builds the dependencies before it imports: import reads only `Depends:` lines, so without them every task is ready at once.
  - `CHECKLIST_FORMAT.md`, "Building the dependencies", holds the rules; `tm.py ready` after the import is the check.
- The page alone: `dag/scripts/dag_site.sh`, or `python3 dag/src/tm.py serve --port 8766`.

## Add dag to this project

Steps for a session asked to "add dag to this project". Run them as written, in one shell, from inside the target
repo; ask the user only what the steps cannot answer.

1. Find the source and the target. `<source>` is a local path to this folder, or a git URL of it.
   A local path is used as is; a git URL is cloned shallow into a new temp directory, and that clone is the source.
   If `$TARGET/dag` exists, stop and report what is there; never overwrite it.

   ```sh
   SOURCE=<source>
   TARGET=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
   if [ -e "$TARGET/dag" ]; then ls -la "$TARGET/dag"; echo "STOP: $TARGET/dag exists"; fi
   case "$SOURCE" in
     *://* | git@* | *.git) CLONE=$(mktemp -d); git clone --depth 1 "$SOURCE" "$CLONE/dag"; SRC=$CLONE/dag ;;
     *) CLONE=; SRC=$SOURCE ;;
   esac
   ```

2. Copy the folder without git history, its database, its records and the files that describe the source; then make the two empty dirs.

   ```sh
   rsync -a --exclude '.git' --exclude 'db/tasks.db*' --exclude 'runs/*' --exclude '__pycache__' \
     --exclude '.pytest_cache' --exclude '.DS_Store' --exclude 'PORT_CHECK.md' "$SRC/" "$TARGET/dag/"
   mkdir -p "$TARGET/dag/db" "$TARGET/dag/runs"
   ```

3. Configure: set `name` in `dag/project.json` to the repo's name.

   ```sh
   python3 - "$TARGET" <<'EOF'
   import json, os, sys
   p = f'{sys.argv[1]}/dag/project.json'
   c = json.load(open(p)); c['name'] = os.path.basename(sys.argv[1])
   open(p, 'w').write(json.dumps(c, indent=2) + '\n')
   EOF
   ```

   - Change `rules` only if the repo's rules file is not `CLAUDE.md`.
   - Set `cite_roots` only if tasks will cite code outside the repo.

4. Git: `dag/.gitignore` already ignores the runtime files, so the repo's own ignore rules need nothing new.
   - Check that no repo rule hides the records: `git -C "$TARGET" check-ignore -v dag/db/tasks.db dag/runs/task_1_exec.md` must print nothing.
     - If it prints a rule, tell the user which one; do not edit the repo's ignore file unasked.
   - Tasks commit only while `dag/runs/COMMIT` exists and the root is a git repo.
   - Do not commit or push anything as part of the install unless the user asks.

5. Rules file: if `$TARGET/CLAUDE.md` exists, append this pointer; if it does not, create nothing unless asked.

   ```sh
   if [ -f "$TARGET/CLAUDE.md" ]; then cat >> "$TARGET/CLAUDE.md" <<'EOF'

   ## DAG

   - We run this project's tasks as a DAG in `dag/`; read `dag/README.md` before touching it.
   - The live page (`python3 dag/src/tm.py serve`) is the source of truth for people; the data is in `dag/db/tasks.db`.
   - We change tasks only with `python3 dag/src/tm.py`; task records are in `dag/runs/`.
   EOF
   fi
   ```

6. Check the install from `$TARGET`, then delete the clone if step 1 made one.

   ```sh
   cd "$TARGET"
   python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' && command -v pi claude
   python3 dag/src/tm.py init && python3 dag/src/tm.py check
   python3 -m pytest dag/src -q -p no:cacheprovider
   if [ -n "$CLONE" ]; then rm -rf "$CLONE"; fi
   ```

   - Python under 3.9: tell the user the DAG needs 3.9 or newer.
   - `pi` missing: workers cannot run; `claude` missing: the launcher cannot open the orchestrator. Tell the user which, and stop there.
   - `pytest` missing: say so; the DAG runs without it, only the tests do not.

7. Hand back and stop: adding dag starts no scheduler and creates no task. Tell the user:
   - the four starts from `## Start here`;
   - the page command: `dag/scripts/dag_site.sh`.

## Pieces

At the top of the folder:

- `project.json`: the only project-specific file: the project name, the rules file, the pi model and the cap defaults.
  - `cite_roots` lists directory globs a cited `path:line` may resolve under, beside the root; `src/host.py` documents every key.
- `db/tasks.db`: the task database, the source of truth. `tm.py init` creates it, `tm.py import` fills it.
- `runs/`: task records and runtime state (below).
- `scripts/`: `dag_orch.sh`, the one-command launch, and `dag_site.sh`, the page.
- `README.md`, `CHECKLIST_FORMAT.md` (the intake format) and `ORCHESTRATOR_PROMPT.md` (the takeover prompt).

In `src/`, all code:

- `tm.py`: the only way to read or change `tasks.db` (tasks, deps, caps, claims, locks, the page); a CLI everything calls.
- `sched.py`: workers that claim ready tasks and run each through pi; one daemon per handover.
- `unblock.py`: a daemon that gives each blocked task up to 2 retries, then escalates.
- `watchdog.py`: one-shot; exits with a report on a new anomaly or after 5 min, which wakes the orchestrator.
- `finish.py <n> [--fix]`: fix, review and close one task outside the scheduler.
- `precheck.py <n>`: deterministic checks between exec and review; the scheduler calls it.
  - It checks the exec file exists, has `## Self-check`, list lines are at most 200 characters, and each cited `path:line` resolves.
- `host.py`: every path and the config; `piusage.py`: token counts from a pi step log.
- `dag_view.html`: the live page `tm.py serve` renders.
- `test_*.py`: `python3 -m pytest dag/src/`.

Outside this folder, under the root: the project's own files and its rules file (`CLAUDE.md` by default). Every
worker reads the rules file first if it exists; project rules go there, never in this folder.

## What `runs/` holds

- Records, worth committing:
  - `task_<n>_exec.md`, the executor's log; `task_<n>_review.md` (and `_review_<k>.md`), the reviews;
  - `task_<n>_result.md`, `task_<n>_precheck.md`, `task_<n>_assess.md` and any raw output a task saves as `task_<n>_*`;
  - `intake_checklist.md`, the draft a spec or task intake was imported from.
- Disposable runtime state, ignored by `.gitignore`:
  - `status.log`, `unblock.log`, per-step `<n>_<step>.log`, `.err` and `.jsonl`, and the daemons' `*.out`;
  - `STOP`, `COMMIT`, `watchdog_seen.json`, `task_<n>_commit.txt`, and `tasks_<why>.db` backups.

`DAG_DB`, `DAG_RUNS`, `DAG_CITE_ROOTS` and `DAG_PI` override paths for tests and scratch runs.

## The page

`tm.py serve` is read-only and shows what a checklist would: milestones with progress, every task with its
status, kind and dependencies, and for a selected task its full section (Why, What, Details, Done when, Evidence),
its Result, links to its record files in `runs/`, its pi steps and its decisions. With nothing selected it lists
what runs, what is blocked, each open `(user)` question with its options, and the decision log.

- `/data.json` is the snapshot the page polls; `/task.json?id=<n>` is one task's full record; `/runs/<file>` serves a record as text.
- `tm.py render` exports the DAG as a markdown checklist to stdout, or to `--out <file>`. It is optional: nothing maintains or reads it.

## Data model (`db/tasks.db`)

- `tasks`: id, milestone, kind (`agent` or `user`), title, status, why, what, details, done_when, evidence, result, worker, `assessed`.
  - Status: `open`, `running`, `blocked`, `done`, `skipped`, `superseded`, `obsolete`.
  - Title prefixes: `[orch]` and `[opus]` tasks are the orchestrator's; a pi worker never claims one.
  - A `(user)` task waits for the user and is never decided by an agent; its question and options are columns (`tm.py decide`).
- `deps (task, dep)`: a task is ready when every dep is closed. `milestones`: id, title, order.
- `meta`: the caps (below), `priority` (the ids claim hands out first) and the imported preamble, Context intro and tail.
- `runs`: one row per pi step (exec, assess, fix0..n, review1..n, close) with rc, duration and tokens.
- `blocks`: the block audit trail; attempt 0 is the block, 1 and 2 are unblock attempts, then `escalated`.
- `locks`: named locks with holder, pid and TTL: `git` for a commit, `sched:<prefix>` for a running scheduler. We use no lock files.
- `context`: the decision log (`tm.py context "..."`).

## A task's life

1. `tm.py claim` picks the ready task with the most open work downstream (priority ids first), within the running cap.
   - It skips a task that fails `tm.py lint` and records why in `meta` as `claim_skip:<id>`.
2. Split check (`assess`): a task with no estimate, or one over 15 min, may be split into parts plus a merge (`tm.py split`).
   - A task whose Details say `estimate <n> min` with n at most 15 runs with no model call.
3. `exec`: pi reads the rules file, does what the task section asks and writes `runs/task_<n>_exec.md`.
   - A result the task names no path for goes to `runs/task_<n>_result.md`.
4. `precheck.py`; findings go to one fix round (`fix0`).
5. `review1..3`: an independent pi review into `runs/task_<n>_review.md`; a FAIL gets a fix round and a fresh review.
6. On PASS, `close` writes the Result block and closes the task.
   - It commits by path (`task <n>: <title>`) only when `runs/COMMIT` exists and the root is a git repo; otherwise it skips the commit silently.
7. Three failed reviews, a `BLOCKED:` line, a timeout or a missing exec file set the task `blocked`; `unblock.py` takes it from there.
   - A review fail gets `finish.py <n> --fix`; a question or timeout is requeued with the reason added to What.
   - After two failed attempts the block is `escalated`, and the watchdog reports it to the orchestrator.

Step caps: exec and fix 30 min, review 15 min, close and assess 10 min. To check order afterwards, use the `runs`
table and `runs/status.log`; `tasks.updated` is overwritten.

## Caps

- `max_running` (`tm.py cap <n>`): all running tasks; it gates new claims only, and `tm.py cap 0` restores the config default.
- `max_opus` (`tm.py cap --opus <n>`): `[opus]` tasks the orchestrator runs at once; the orchestrator reads it.
- `project.json` `caps` sets both defaults (6 and 2 with no file); `tm.py cap` and `tm.py cap --opus` print the live values.

## Starting everything by hand (from the project root)

```sh
D=$PWD/dag                                # absolute, so pgrep tells our daemons from another project's
W=$(python3 $D/src/tm.py cap)             # workers equal the running cap
nohup python3 $D/src/sched.py --workers $W --prefix a >> $D/runs/sched_a.out 2>&1 &
nohup python3 $D/src/unblock.py >> $D/runs/unblock.out 2>&1 &
nohup python3 $D/src/tm.py serve --port 8766 >> $D/runs/serve.out 2>&1 &   # the page, http://127.0.0.1:8766
```

- With no `--ms`, the scheduler serves every milestone; `--ms M2,M3` narrows it.
- Commits are off by default. `git init` (once, if the root is not a repo) plus `touch dag/runs/COMMIT` turns them on.
- Check what runs: `python3 dag/src/tm.py lock list` shows each scheduler as `sched:<prefix>` with its workers and milestones.
  - `pgrep -fl "$PWD/dag/src/(sched|unblock|tm)\.py"` shows the processes; each `START sched` line in `runs/status.log` marks a run.
- The cap, not the worker count, limits concurrency. We run one scheduler only.
- A scheduler exits (`END sched`) once its milestones hold no agent task that is open, running or blocked.
  - `[orch]`, `[opus]` and `(user)` tasks do not keep it alive; while one of them gates agent tasks, it waits.
- The page server loads `tm.py` once: restart it after a `tm.py` change. It re-reads `dag_view.html` per request.

## Replacing the scheduler (after a `sched.py` change)

```sh
touch $D/runs/STOP && sleep 1
nohup python3 $D/src/sched.py --workers $W --prefix <new letter> >> $D/runs/sched_<letter>.out 2>&1 &
```

STOP stops only schedulers started before its mtime, so we touch it first, then start the new one. Old workers
finish their current task and exit; nothing is killed. A prefix in use is refused; pick one `tm.py lock list` does not show.

## The orchestrator's loop

Run `python3 dag/src/watchdog.py` in the background; when it exits, read its report, act, and run it again.

- `HEARTBEAT` (every 5 min with nothing new): status only. Start the watchdog again after every exit.
- `scheduler process is not running`: it drained its milestones or died; start one if agent work remains.
- `<n> ready for the orchestrator`: an `[orch]` or `[opus]` task's dependencies are closed; run it by hand (below).
- `<n> waits on the user`: a `(user)` task is ready; tell the user, and never decide it.
- `step ... near the 30 min cap` or `running N min, written estimate M`: the cap handles it; look only if it repeats.
- `blocked N min with no unblock attempt`: no one owns the block. Check `unblock.py` runs, or run the step yourself.
  - For a review fail: `python3 dag/src/finish.py <n> --fix`; record it with `tm.py block add` and `tm.py block end`.
- `<n> blocked` on an `[orch]` or `[opus]` task: yours; `unblock.py` skips those.
- `escalated after two unblock attempts`: yours. Read the last review, then requeue with a note in What, close as partial or skipped, or rewrite the task.
  - Record the decision with `tm.py context`.
- `orphan process` or `no step started or ended for N min`: look at the process and `runs/<n>_<step>.err` before acting.

## Common operations

- `tm.py show <n>`, `ready [--user]`, `list --ms M2 --status open`, `stats`, `check`, `lint <n>`, `cost --ms M2`.
- `tm.py add --ms <m> --assessed --title ... --what @file --details @file --done-when @file --deps 1,2`.
- `tm.py edit <n> --status open --force`; `tm.py edit <n> --add-deps 3`; `tm.py prio <ids>` (0 clears).
- `tm.py close <n> --result -`; `tm.py block list`; `tm.py lock list`; `tm.py context --task <n> "decision ..."`.
- A user decision: `tm.py decide <n> --question ... --option "label: description"`; the user's answer closes it with `tm.py close`.
- A hand-run (`[orch]` or `[opus]`) task:
  - `tm.py edit <n> --status running --force` (prints nothing), so the page shows it;
  - do it (an Opus sub-agent for `[opus]`), writing `runs/task_<n>_exec.md` and its outputs;
  - review it into `runs/task_<n>_review.md`; the orchestrator reviews both kinds itself;
  - `tm.py close <n> --result -` with the exec path, a nested one-line outcome and the review line;
  - with commits on: `git add -- <files>` and `git commit -m "task <n>: <title>" -- <outputs> <exec> <review> dag/db/tasks.db`.
- An `[opus]` sub-agent's brief: the task id, `tm.py show <n>`, the files it may write (its outputs and its exec file), no `tasks.db` edits, no commit.
- Replacing the whole DAG: `tm.py import --force <checklist.md>`; it refuses without `--force` once the database holds tasks.
- Backups before bulk edits: `cp dag/db/tasks.db dag/runs/tasks_<why>.db`.
- A backend outage (every step fails in seconds and tasks block in minutes): touch STOP and stop `unblock.py`.
  - Probe with `pi -p --no-session --no-tools --model <model> "ok" </dev/null`.
  - Once it answers, delete the outage's `blocks` rows, reopen the tasks, and start a fresh scheduler and `unblock.py`.

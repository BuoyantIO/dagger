Read dag/README.md and, if it exists, the root rules file named in dag_data/project.json (CLAUDE.md by default), then take over as the DAG orchestrator for this project. Use the README for every step. The last line of this prompt may be an intake line: `Intake: checklist <path>`, `Intake: spec <path>` or `Intake: task <text>`.

0. Intake. Check the database first: `python3 dag/src/tm.py stats` (a missing database or no output means it is empty).
   - The database holds tasks: ignore any intake line, say that you ignored it, and go to step 1.
     Replace the DAG only when the user says to, with `tm.py import --force`.
   - Empty, `Intake: checklist <path>`: run `python3 dag/src/tm.py init`, then `python3 dag/src/tm.py import <path>`.
     On an error, show its `<file>:<line>` lines, fix the file with the user, and import again.
   - Empty, `Intake: spec <path>` or `Intake: task <text>`: read dag/CHECKLIST_FORMAT.md, then write the checklist to dag_data/runs/intake_checklist.md.
     Use that format and its sizing rules.
     Ask the user only the questions whose answers change the task breakdown. Show the milestone and task list, and import only after a yes: `tm.py init`, then `tm.py import dag_data/runs/intake_checklist.md`.
   - Empty, no intake line: ask the user which they want: a checklist path, a spec path, or a task in words. Then proceed as above.
   - Dependencies, on every intake and before the import: build them as dag/CHECKLIST_FORMAT.md, "Building the dependencies", says.
     Read each task's What and Details and give it a `Depends:` line naming every task whose output it reads or whose setup it works in.
     For a checklist, show the user the lines you would add or change and write them into the file on a yes; for a spec or a task, write them into the draft.
   - After any import: run `python3 dag/src/tm.py check` and `tm.py ready`, fix what they report, and carry on.
     `tm.py ready` must list only tasks that can start with nothing else done; add a missing dependency with `tm.py edit <n> --add-deps <ids>`.
     Start no scheduler while a ready task still needs another task's output.
     No checklist file is kept in step with the database: the page is where people read the DAG.
1. Report what runs: the scheduler and its milestones (`python3 dag/src/tm.py lock list`, the `START sched` line in dag_data/runs/status.log), unblock, the page, caps, and running, blocked and ready tasks (`tm.py stats`, `tm.py ready`).
2. Start the scheduler yourself if none runs and agent tasks are open, with the README's command (no `--ms`: it serves every milestone); after a `sched.py` change, replace a running one (STOP handover).
   Start `dag/src/unblock.py` and the page only after checking they do not already run for this root.
   Leave `dag_data/runs/COMMIT` as you find it: tasks commit only when it exists and the root is a git repo.
3. Run the watchdog loop: on every exit read its report, act as the README says, and start it again.
4. Run `[orch]` and `[opus]` tasks by hand when the watchdog reports them ready (an Opus sub-agent for `[opus]`, at most `max_opus` at once): set the task running, do it, review it, close it.
5. Own every escalated block and every block no one retries: read the last review, then requeue with a note, close as partial or skipped, or rewrite the task; log the decision with `tm.py context`.
6. Never decide a `(user)` task: when one is ready, tell the user and keep running what does not depend on it.
7. Follow every rule in the rules file, never push, and use only the model in dag_data/project.json for pi, ending every nested `pi -p` call with `</dev/null`.

Reply after step 2 with what you found and started, then keep running the loop.

# Port check

We ran these checks on this folder in a fresh temp directory, then deleted it. `<tmp>` stands for that directory.
This file describes the source folder, not an install; the install steps leave it out.

## What ships

```
.
./.gitignore
./CHECKLIST_FORMAT.md
./db
./db/.gitkeep
./ORCHESTRATOR_PROMPT.md
./project.json
./README.md
./runs
./runs/.gitkeep
./scripts
./scripts/dag_orch.sh
./scripts/dag_site.sh
./src
./src/dag_view.html
./src/finish.py
./src/host.py
./src/piusage.py
./src/precheck.py
./src/sched.py
./src/test_claim_lint.py
./src/test_cost.py
./src/test_import.py
./src/test_lint.py
./src/test_piusage.py
./src/test_precheck_wt.py
./src/test_run_tokens.py
./src/tm.py
./src/unblock.py
./src/watchdog.py
```

## 1. Install from a local path

We ran the sh blocks of `## Add dag to this project` in README.md as written, in one shell, inside a new git repo that had a `CLAUDE.md`. Only `<source>` was filled in.

```
--- commands run (the section's sh blocks, with <source> = the local path):
SOURCE=~/projects/dag
TARGET=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
if [ -e "$TARGET/dag" ]; then ls -la "$TARGET/dag"; echo "STOP: $TARGET/dag exists"; fi
case "$SOURCE" in
  *://* | git@* | *.git) CLONE=$(mktemp -d); git clone --depth 1 "$SOURCE" "$CLONE/dag"; SRC=$CLONE/dag ;;
  *) CLONE=; SRC=$SOURCE ;;
esac

rsync -a --exclude '.git' --exclude 'db/tasks.db*' --exclude 'runs/*' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.DS_Store' --exclude 'PORT_CHECK.md' "$SRC/" "$TARGET/dag/"
mkdir -p "$TARGET/dag/db" "$TARGET/dag/runs"

python3 - "$TARGET" <<'EOF'
import json, os, sys
p = f'{sys.argv[1]}/dag/project.json'
c = json.load(open(p)); c['name'] = os.path.basename(sys.argv[1])
open(p, 'w').write(json.dumps(c, indent=2) + '\n')
EOF

if [ -f "$TARGET/CLAUDE.md" ]; then cat >> "$TARGET/CLAUDE.md" <<'EOF'

## DAG

- We run this project's tasks as a DAG in `dag/`; read `dag/README.md` before touching it.
- The live page (`python3 dag/src/tm.py serve`) is the source of truth for people; the data is in `dag/db/tasks.db`.
- We change tasks only with `python3 dag/src/tm.py`; task records are in `dag/runs/`.
EOF
fi

cd "$TARGET"
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' && command -v pi claude
python3 dag/src/tm.py init && python3 dag/src/tm.py check
python3 -m pytest dag/src -q -p no:cacheprovider
if [ -n "$CLONE" ]; then rm -rf "$CLONE"; fi

--- output:
/opt/homebrew/bin/pi
~/.local/bin/claude
created <tmp>/projA/dag/db/tasks.db
ok
.........                                                                [100%]
9 passed in 1.53s
--- result:
dag:
.gitignore
CHECKLIST_FORMAT.md
db
ORCHESTRATOR_PROMPT.md
project.json
README.md
runs
scripts
src

dag/db:
.gitkeep
tasks.db
tasks.db-shm
tasks.db-wal

dag/runs:
{
  "name": "projA",
  "rules": "CLAUDE.md",
  "model": "zoltar/coding-deepseek",
  "caps": {
    "max_running": 6,
    "max_opus": 2
  },
  "cite_roots": []
}
--- CLAUDE.md tail:

## DAG

- We run this project's tasks as a DAG in `dag/`; read `dag/README.md` before touching it.
- The live page (`python3 dag/src/tm.py serve`) is the source of truth for people; the data is in `dag/db/tasks.db`.
- We change tasks only with `python3 dag/src/tm.py`; task records are in `dag/runs/`.
--- git check-ignore (must print nothing):
exit 1
--- git status --short --ignored:
?? CLAUDE.md
?? dag/
!! dag/db/tasks.db-shm
!! dag/db/tasks.db-wal
```

## 2. Install from a git URL

We pushed a temp copy of this folder to a throwaway local bare repo and used its `file://` URL as `<source>`, inside a new git repo with no `CLAUDE.md`. Nothing was pushed to a remote, and this folder got no `.git`.

```
--- output:
Cloning into '<tmpdir>/tmp.XSB3FHyXgZ/dag'...
/opt/homebrew/bin/pi
~/.local/bin/claude
created <tmp>/projB/dag/db/tasks.db
ok
.........                                                                [100%]
9 passed in 1.60s
--- result:
.gitignore
CHECKLIST_FORMAT.md
db
ORCHESTRATOR_PROMPT.md
project.json
README.md
runs
scripts
src
git dirs under dag: 0
CLAUDE.md created: no
  "name": "projB",
leftover clones: 0
```

## 3. A second run stops

```
STOP: <tmp>/projA/dag exists
```

## 4. Intake of the documented example, export, and the page on port 8767

In the first temp repo: init, import of the example cut from CHECKLIST_FORMAT.md, check, ready, the optional export, then the page and its endpoints.

```
$ python3 dag/src/tm.py init
kept <tmp>/projA/dag/db/tasks.db
$ python3 dag/src/tm.py import example.md
imported 4 tasks, 2 milestones, 2 context entries
$ python3 dag/src/tm.py check
ok
$ python3 dag/src/tm.py ready
1	M1	Count the lines of each source file
$ python3 dag/src/tm.py stats
M1	open	2
M2	open	2
$ python3 dag/src/tm.py show 2
2. - [ ] (agent) Find the three largest files

## 2. Find the largest files

Why:
The report leads with the files most worth splitting.

What:
`dag/runs/task_2_result.md`: the three paths with the most lines, largest first.

Details:
- Input: `dag/runs/task_1_result.md`.
- Time budget: estimate 5 min.
Depends: 1.

Done when:
`sort -rn dag/runs/task_1_result.md | head -3` names the same three paths in the same order.

Evidence:
The sort output.
$ python3 dag/src/tm.py render | head -12
<!-- Exported by tm.py render from the task database; a snapshot, not maintained. -->

## Tasks

### M1. Measure
1. - [ ] (agent) Count the lines of each source file
2. - [ ] (agent) Find the three largest files

### M2. Report
3. - [ ] (agent) Write `size_report.md`
4. - [ ] (user) Approve the report

import left the checklist file untouched: yes; checklist files written by tm.py: 0
--- page on 8767
/ 200
<title>projA: task DAG</title>
/data.json: 4 tasks, 4 links, 3 context entries, intro 'We want a one-page view of where the cod'
/task.json?id=2: {'why': True, 'what': True, 'details': True, 'done_when': True, 'evidence': True, 'result': False, 'deps': [1], 'files': ['task_2_exec.md'], 'runs_dir': 'dag/runs', 'context': ['- Decision: sort by lines (test entry)']}
exec record  (/runs/task_2_exec.md 200) 
/runs/..%2f..%2fCLAUDE.md 404
/task.json?id=99 404
(shell notice: page server stopped) python3 dag/src/tm.py serve --port 8767 > /dev/null 2>&1
listeners on 8767 after stop: 0
```

## 5. A malformed checklist

Import with `--force` over the database from step 4: every problem is named by line, and the database keeps its 4 tasks.

```
bad.md:3: a milestone heading must read `### M<k>. <title>`
bad.md:4: task 1 comes before any `### M<k>. <title>` milestone heading
bad.md:6: a task line must read `<n>. - [ ] (agent) <title>` or `<n>. - [ ] (user) <title>`, with `[x]` when closed
bad.md:8: task id 3 appears twice (first on line 7)
bad.md:11: task 3 depends on task 9, which has no task line
bad.md:9: open agent task 4 has no `## 4. <title>` section
import: 6 problem(s) in bad.md; nothing was imported
exit 1
database still holds: 4 tasks
```

## 6. Tests

```
.........                                                                [100%]
9 passed in 1.47s
compiled
```

## 7. Scheduler smoke: 2 chained tasks to DONE

In the same temp repo, with no env override: two agent tasks (the second reads the first one's output) and one user task.

```
$ python3 dag/src/tm.py import --force toy.md
imported 3 tasks, 1 milestones, 0 context entries
ok
$ python3 dag/src/sched.py --workers 2 --prefix t --idle 10   (watchdog beside it, unblock.py --once after)
sched rc=0
unblock rc=0
--- runs/status.log
10:38:47 START sched prefix=t workers=2 ms=all
10:38:47 [t0] 1 START
10:39:02 [t0] 1 exec rc=0 14s tokens=390/2428
10:39:37 [t0] 1 review1 rc=0 34s tokens=546/7472
10:39:37 [t0] 1 review1 PASS
10:39:48 [t0] 1 close rc=0 11s tokens=244/1756
10:39:48 [t0] 1 DONE
10:39:48 [t0] 2 START
10:40:09 [t0] 2 exec rc=0 20s tokens=474/3892
10:40:39 [t0] 2 review1 rc=0 30s tokens=357/6894
10:40:39 [t0] 2 review1 PASS
10:40:53 [t0] 2 close rc=0 13s tokens=428/1975
10:40:53 [t0] 2 DONE
10:41:00 END sched prefix=t: no claimable work left in all milestones
--- watchdog during
HEARTBEAT running 1, closed in last 1 min 1, open 1, blocked 0, done 1
--- watchdog after
ANOMALY
- 3 waits on the user: Approve the count
- scheduler process is not running
--- tm.py list
1	M1	done	Write the input file
2	M1	done	Count the input file
3	M1	open	Approve the count
--- out/a.txt, out/b.txt
hello from task 1
4

--- dag/runs
1_close.err
1_close.jsonl
1_close.log
1_exec.err
1_exec.jsonl
1_exec.log
1_review1.err
1_review1.jsonl
1_review1.log
2_close.err
2_close.jsonl
2_close.log
2_exec.err
2_exec.jsonl
2_exec.log
2_review1.err
2_review1.jsonl
2_review1.log
status.log
task_1_exec.md
task_1_review.md
task_2_exec.md
task_2_review.md
watchdog_seen.json
--- git status --short --ignored dag/runs (records untracked, runtime ignored)
  ?? dag/runs/task_1_exec.md
  ?? dag/runs/task_1_review.md
  ?? dag/runs/task_2_exec.md
  ?? dag/runs/task_2_review.md
  !! dag/runs/1_close.err
  !! dag/runs/1_close.jsonl
  !! dag/runs/1_close.log
  !! dag/runs/1_exec.err
  !! dag/runs/1_exec.jsonl
  !! dag/runs/1_exec.log
  !! dag/runs/1_review1.err
  !! dag/runs/1_review1.jsonl
  !! dag/runs/1_review1.log
  !! dag/runs/2_close.err
  !! dag/runs/2_close.jsonl
  !! dag/runs/2_close.log
  !! dag/runs/2_exec.err
  !! dag/runs/2_exec.jsonl
  !! dag/runs/2_exec.log
  !! dag/runs/2_review1.err
  !! dag/runs/2_review1.jsonl
  !! dag/runs/2_review1.log
  !! dag/runs/status.log
  !! dag/runs/watchdog_seen.json
--- checklist files in the project: 0
--- task.json for 2 after close
{
 "files": [
  "task_2_exec.md",
  "task_2_review.md"
 ],
 "steps": [
  {
   "step": "exec",
   "rc": "0",
   "verdict": null,
   "secs": 20,
   "tokens": 4366
  },
  {
   "step": "review1",
   "rc": "0",
   "verdict": "PASS",
   "secs": 31,
   "tokens": 7251
  },
  {
   "step": "close",
   "rc": "0",
   "verdict": null,
   "secs": 14,
   "tokens": 2403
  }
 ],
 "result": "- Exec: `dag/runs/task_2_exec.md`; rc=0 (`dag/runs/status.log:9`).\n- Outcome:\n  - `out/b.txt:1` is `4`; `od -c out/b.txt` -> `0000000    4  \\n`, from `wc -w < out/a.txt`.\n  - Done when re-run: `cat out/b.txt` printed `4`.\n- Review: `dag/runs/task_2_review.md`, verdict PASS (`dag/runs/status.log:11`)."
}
```

## 8. Name grep

`grep -rniE 'edge|captain|linkerd|buoyant|threat|wt_edgecaptain|TM_' <this folder> --exclude=PORT_CHECK.md` (exit 1 means no hit; this file is excluded because it quotes the pattern)

```
exit 1
```

The page script also passes `node --check`.
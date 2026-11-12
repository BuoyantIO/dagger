#!/usr/bin/env python3
"""Run ready DAG tasks with N parallel headless pi workers: exec, independent review, close.

Usage: sched.py --workers N [--ms M1,M2] [--max K] [--prefix P]; with no --ms it serves every milestone.
Pause: touch runs/STOP (workers finish their current task). Commits only when runs/COMMIT exists and the
root is a git repo.
"""
import argparse, os, re, signal, socket, sqlite3, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host
from tm import lock_try
from piusage import usage

ROOT = host.ROOT
TMPY = f'{host.HERE}/tm.py'
TM = ['python3', TMPY]
# One directory holds the task records (exec, review, result files) and the runtime state (logs, STOP, COMMIT).
AR = D = host.RUNS
# Same `DAG_DB` override tm.py reads, so a test or a scratch round trip points the whole scheduler at a db copy.
DB = host.DB
os.makedirs(AR, exist_ok=True)
# What the prompts call tm.py and the results dir: relative to the root pi runs in when they live under it.
rel = lambda p: os.path.relpath(p, ROOT) if p.startswith(ROOT + os.sep) else p
TMCMD = f'python3 {rel(TMPY)}'
ARREL = rel(AR)
DBREL = rel(DB)
RULES = host.RULES


class _GitLock:
    # One commit at a time across threads and scheduler processes: git's index lock is repo-wide.
    # The cross-process lock is the `git` row in tasks.db's locks table, the one source of truth for locks.
    def __init__(self): self.t = threading.Lock()
    def _db(self):
        c = sqlite3.connect(DB, timeout=60, isolation_level=None)
        c.row_factory = sqlite3.Row; return c
    def __enter__(self):
        self.t.acquire()
        c = self._db()
        while not lock_try(c, 'git', f'sched:{os.getpid()}', 600, 'git commit'): time.sleep(0.5)
        c.close()
    def __exit__(self, *a):
        c = self._db()
        c.execute('DELETE FROM locks WHERE name=? AND host=? AND pid=?', ('git', socket.gethostname(), os.getpid())); c.close()
        self.t.release()


GIT = _GitLock()
START = time.time()
LOGL = threading.Lock()

COMMON = f"""You work in `{ROOT}`. If `{RULES}` exists, read it first: it holds this project's rules, and they bind every step you take.
The task list lives in SQLite (`{DBREL}`), and the live page shows it; there is no checklist file to read or update.
Use `{TMCMD}` (`show <n>`, `add`, `edit`, `context`, `-h`) for any task, dependency or decision-log change.
Rules for every step:
- Every result and working file goes in `{ARREL}/`; a deliverable goes where the task or `{RULES}` says.
- Every list entry you write is at most 200 characters; split longer ones into nested items.
- Every factual claim carries a `path:line` or pasted command output; a behavior you did not run is written `expected (not run)`.
- Any nested pi call uses `--model {host.MODEL}` and ends with `</dev/null`.
Other tasks run in parallel here; touch only the files your task names."""

EXEC = COMMON + f"""
You are the executor for task {{n}}. Run `{TMCMD} show {{n}}` and do what its section asks (What, Details, Done when, Evidence), nothing more.
- Write the exec file `{ARREL}/task_{{n}}_exec.md`: start it before the first step and append after each step.
  - It holds each command, its output path, and numbers or lines copied from the output; no narrative claims.
- When the What asks for a result file and names no path, write it to `{ARREL}/task_{{n}}_result.md` and name that path in the exec file.
- Before replying DONE, self-check and paste the outputs under `## Self-check` in the exec file:
  - run every Done when check and paste its output; fix the work if one fails;
  - re-run each command whose output you quote, so the quote matches a fresh run;
  - print every `path:line` you cite with `sed -n '<line>p' <file>` and fix each one that does not show what you claim;
  - run `awk 'length > 200 && /^ *- /'` on each file you wrote.
- Do not close the task, do not add a Result, do not commit or stage anything.
- Never decide a (user) task. If a step needs an open user decision or a tool you lack, and the task names no default, write a line `BLOCKED: <question>` in the exec file and stop.
- Stop at 30 min and record what was done.
Reply with one line: DONE or BLOCKED."""

REVIEW = COMMON + f"""
You are an independent reviewer for task {{n}}; you did not do the work. Read `{TMCMD} show {{n}}` and `{ARREL}/task_{{n}}_exec.md`.
- Run every read-only Done when check and compare with the claimed output. Check the outputs match the task section and the changed files are the ones the task names.
- Check honesty: numbers that do not reproduce, a `path:line` that does not show the claim, claims the files do not support, a (user) decision taken by the agent.
- Check each rule in `{RULES}` (if it exists) that the task could break, with a read-only command where one exists.
- Run `awk 'length > 200 && /^ *- /'` on each file the task wrote.
- Fail only on a blocking finding: an output file wrong or missing, a step skipped, a number or citation in an output file that does not reproduce, a rule broken.
  - Exec-file wording is a note under `## Notes`, and the verdict stays PASS.
- Fix nothing. Write `{ARREL}/{{rf}}` with one line per check, and the last line exactly `Verdict: PASS` or `Verdict: FAIL`.
Reply with the verdict line."""

FIX = COMMON + f"""
Task {{n}} failed a check: read `{ARREL}/{{rf}}`. Read `{TMCMD} show {{n}}`,
fix each failure within the task's scope, and append a `## Fix` part to `{ARREL}/task_{{n}}_exec.md`.
Then re-run the whole self-check and replace its outputs under `## Self-check`.
Do not close the task, and do not commit or stage anything. Reply DONE."""

CLOSE = COMMON + f"""
Task {{n}} passed review (`{ARREL}/{{rf}}`). Follow the `## Closing a task` section of `{RULES}` if there is one. Then:
1. Close it: `{TMCMD} close {{n}} [--reason partial|...] --result -` with the Result lines on stdin:
   the exec path, a nested one-line outcome, and the review line. Pass `--reason` only for a partial or skipped result.
2. If the exec records a decision, add it with `{TMCMD} context "<decision; rejected: ...>" --task {{n}}`.
3. {{commit}}
Change nothing else. Reply DONE."""

COMMIT_NO = "Do not commit or stage anything."
# Close does not commit: pi lists the paths, and the scheduler commits them itself under the git lock, which
# takes a second instead of holding the lock for a whole pi session.
COMMIT_LIST = (f"Do not commit. Write `{ARREL}/task_{{n}}_commit.txt`: one root-relative path per line for every file the task wrote "
               f"under `{ROOT}` (its deliverables and its `{ARREL}/task_{{n}}_*` files). The scheduler adds the task database.")

COMMIT_ONLY = COMMON + f"""
Task {{n}} is closed but has no commit. Commit it now: `git commit -m "task {{n}}: <title>" -- <paths>` with its
deliverables, `{ARREL}/task_{{n}}_*` and `{DBREL}`. Change no file. Reply DONE."""

ASSESS = COMMON + f"""
Quick split check for task {{n}}; spend a few minutes at most and do not do its work. Rule of thumb: a task is 15 min or less.
Read its section with `{TMCMD} show {{n}}` and glance at the inputs it names (its dependencies are done).
If it fits in about 15 min, or is one indivisible step, reply RUN.
Otherwise split it into pieces of 15 min or less each, plus a merge piece:
1. `{TMCMD} add --ms <its milestone> --assessed --title "<title>: part k of N" --what - --details - --done-when -`
   per piece, each a full section (What listing its share, Details with an estimate and exec file, Done when) writing to a per-part file.
   When a piece needs an earlier piece's output, add `--deps <earlier part id>`; parts with no `--deps` run at once.
2. One merge task the same way that combines the parts into the original outputs, adding `--deps <part ids>` so it waits on
   the parts from the moment it exists.
3. `{TMCMD} split {{n}} --into <part ids> --merge <merge id>`.
Write `{ARREL}/task_{{n}}_assess.md` with one line of reasoning. Reply exactly RUN or SPLIT on the last line."""


def log(msg):
    line = time.strftime('%H:%M:%S ') + msg
    with LOGL:
        print(line, flush=True)
        with open(f'{D}/status.log', 'a') as f: f.write(line + '\n')


def tm(*args):
    return subprocess.run(TM + list(args), capture_output=True, text=True, cwd=ROOT)


class Gone(Exception):
    pass


def pi(w, n, step, prompt, timeout):
    # A task closed or superseded while it ran (a re-split, a manual close) gets no further pi steps.
    db = host.connect_ro(60)
    st = db.execute('SELECT status FROM tasks WHERE id=?', (int(n),)).fetchone()[0]; db.close()
    if st != 'running' and step != 'close':
        raise Gone(f'{n} is {st}')
    t = time.time()
    rid = tm('run', 'start', '--task', str(n), '--step', step, '--worker', w).stdout.strip()
    # `--mode json` writes one event per line and carries the usage, so the step can score its own tokens;
    # stderr goes to its own file because a stderr line inside the jsonl would corrupt the event it lands in.
    jsonl, logf, errf = f'{D}/{n}_{step}.jsonl', f'{D}/{n}_{step}.log', f'{D}/{n}_{step}.err'
    with open(jsonl, 'w') as out, open(errf, 'w') as err:
        # Each step runs in its own process group, and a timeout kills the whole group: killing only pi leaves
        # its self-check scripts running as orphans. stdin is /dev/null, or `pi -p` waits for it to close.
        p = subprocess.Popen([host.PI, '-p', '--no-session', '--model', host.MODEL, '--mode', 'json', prompt],
                             cwd=ROOT, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = 'timeout'
        finally:
            try: os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    # The step log keeps the shape its readers expect, the reply text, rebuilt from the events; the stderr
    # is appended after, so a failed step still shows why on the same file.
    text, tin, tout = usage(open(jsonl, errors='replace').read().splitlines())
    with open(logf, 'w') as f:
        f.write(text)
        tail = open(errf, errors='replace').read()
        if tail: f.write('\n' + tail)
    tm('run', 'end', '--run', rid, '--rc', str(rc), '--tokens-in', str(tin), '--tokens-out', str(tout))
    log(f'[{w}] {n} {step} rc={rc} {int(time.time() - t)}s tokens={tin}/{tout}')
    return rid, rc


def field(n, col):
    """One column of task n: '' for a missing row or NULL, else str(value)."""
    db = host.connect_ro(60)
    try: r = db.execute(f'SELECT {col} FROM tasks WHERE id=?', (int(n),)).fetchone()
    finally: db.close()
    return '' if r is None or r[0] is None else str(r[0])


def want_commit():
    """Tasks commit only while `runs/COMMIT` exists and the root is a git repo; otherwise close skips it silently."""
    return os.path.exists(f'{D}/COMMIT') and host.is_git()


def commit_listed(w, n):
    """Commit the paths close listed, under the git lock; the fallback pi commit runs if this fails."""
    lf = f'{AR}/task_{n}_commit.txt'
    if not os.path.exists(lf): return
    paths = [DBREL] + [l.strip() for l in open(lf) if l.strip()]
    paths = [p for p in dict.fromkeys(paths) if os.path.exists(os.path.join(ROOT, p))]
    with GIT:
        subprocess.run(['git', 'add', '--'] + paths, cwd=ROOT, capture_output=True)
        r = subprocess.run(['git', 'commit', '-q', '-m', f'task {n}: {field(n, "title")}', '--'] + paths, cwd=ROOT, capture_output=True, text=True)
    log(f'[{w}] {n} commit {len(paths)} paths rc={r.returncode}')


def committed(n):
    r = subprocess.run(['git', 'log', '--oneline', '-200', f'--grep=^task {n}:'], capture_output=True, text=True, cwd=ROOT)
    return bool(r.stdout.strip())


def verdict(rf):
    p = f'{AR}/{rf}'
    m = re.findall(r'Verdict: (PASS|FAIL)', open(p).read()) if os.path.exists(p) else []
    return m[-1] if m else None


def status(n):
    r = tm('show', str(n), '--json').stdout
    m = re.search(r'"status": "(\w+)"', r)
    return m.group(1) if m else None


def assess(w, n):
    """Split check just before the work: returns True when the task was split and must not run."""
    if field(n, 'assessed') == '1': return False
    # Tasks estimated at 15 min or less run without a model call.
    m = re.search(r'estimate (\d+)(?:\s*(?:to|-)\s*(\d+))? min', field(n, 'details'))
    if m and int(m.group(2) or m.group(1)) <= 15:
        tm('edit', str(n), '--assessed', 'yes'); return False
    rid, _ = pi(w, n, 'assess', ASSESS.format(n=n), 600)
    st = status(n)
    tm('run', 'end', '--run', rid, '--rc', '0', '--verdict', 'SPLIT' if st == 'superseded' else 'RUN')
    if st == 'superseded':
        log(f'[{w}] {n} SPLIT'); return True
    tm('edit', str(n), '--assessed', 'yes')
    return False


def run_task(w, n):
    if assess(w, n): return
    # A retry starts from a clean exec file; the previous attempt's file is kept beside it.
    ex = f'{AR}/task_{n}_exec.md'
    if os.path.exists(ex):
        k = 1
        while os.path.exists(f'{AR}/task_{n}_exec_prev{k}.md'): k += 1
        os.rename(ex, f'{AR}/task_{n}_exec_prev{k}.md')
    # A task whose Details name another exec file (split parts inherit the parent's name) gets
    # task_<n>_exec.md as a link to it, so writing either path counts. Only a numeric id counts: a
    # `task_N_exec.md` placeholder is not a file.
    other = {x for x in re.findall(re.escape(os.path.basename(AR)) + r'/(task_\d+_exec\.md)', field(n, 'details') + ' ' + field(n, 'body'))
             if x != f'task_{n}_exec.md'}
    if len(other) == 1 and not os.path.lexists(ex):
        os.symlink(other.pop(), ex)
    pi(w, n, 'exec', EXEC.format(n=n), 1800)
    if not os.path.exists(ex):
        log(f'[{w}] {n} FAILED no exec file'); tm('release', str(n), '--status', 'blocked'); return
    if re.search(r'^BLOCKED:', open(ex).read(), re.M):  # a line that starts with it, not a quote in prose
        log(f'[{w}] {n} BLOCKED'); tm('release', str(n), '--status', 'blocked'); return
    # Deterministic pre-check: send mechanical findings to one fix round before the first review.
    pc = subprocess.run(['python3', f'{host.HERE}/precheck.py', n], capture_output=True, text=True, cwd=ROOT)
    if pc.returncode == 1 and pc.stdout.strip():
        with open(f'{AR}/task_{n}_precheck.md', 'w') as f:
            f.write('# Pre-check findings (scheduler)\n\n' + ''.join(f'- {l}\n' for l in pc.stdout.strip().splitlines()))
        log(f'[{w}] {n} precheck {len(pc.stdout.strip().splitlines())} findings')
        pi(w, n, 'fix0', FIX.format(n=n, rf=f'task_{n}_precheck.md'), 1800)
    for k in (1, 2, 3):
        rf = f'task_{n}_review.md' if k == 1 else f'task_{n}_review_{k}.md'
        rid, _ = pi(w, n, f'review{k}', REVIEW.format(n=n, rf=rf), 900)
        v = verdict(rf); tm('run', 'end', '--run', rid, '--rc', '0', '--verdict', str(v))
        log(f'[{w}] {n} review{k} {v}')
        if v == 'PASS':
            want = want_commit()
            pi(w, n, 'close', CLOSE.format(n=n, rf=rf, commit=COMMIT_LIST.format(n=n) if want else COMMIT_NO), 600)
            st = status(n)
            if st in ('done', 'skipped') and want and not committed(n):
                commit_listed(w, n)
            if st in ('done', 'skipped') and want and not committed(n):
                with GIT:
                    pi(w, n, 'commit', COMMIT_ONLY.format(n=n), 600)
                if not committed(n): log(f'[{w}] {n} WARN no commit found')
            log(f'[{w}] {n} {"DONE" if st in ("done", "skipped") else "FAILED close left status " + str(st)}')
            if st == 'running': tm('release', str(n), '--status', 'blocked')
            return
        if k < 3:
            pi(w, n, f'fix{k}', FIX.format(n=n, rf=rf), 1800)
    log(f'[{w}] {n} FAILED review three times'); tm('release', str(n), '--status', 'blocked')


def work_left(ms):
    """Whether a pi worker still has something to wait for: an agent task that is open, running or blocked.

    `[orch]`, `[opus]` and (user) tasks are not ours, so a scheduler whose milestones hold only those exits; the
    watchdog then reports it, and the orchestrator starts a new one once agent tasks are ready again.
    """
    db = host.connect_ro(60)
    try:
        ids = ms.split(',') if ms else []
        only = f"AND milestone IN ({','.join('?' * len(ids))})" if ids else ''
        return bool(db.execute(f"""SELECT count(*) FROM tasks WHERE kind='agent' AND status IN ('open', 'running', 'blocked')
                                   AND title NOT LIKE '[orch]%' AND title NOT LIKE '[opus]%' {only}""", ids).fetchone()[0])
    finally: db.close()


def worker(w, ms, budget):
    # STOP pauses only schedulers started before it was touched, so a drained old scheduler can be replaced while
    # it is still there; touch it again to stop the new ones.
    while not (os.path.exists(f'{D}/STOP') and os.path.getmtime(f'{D}/STOP') > START):
        with budget['lock']:
            if budget['left'] == 0: return
            r = tm('claim', '--worker', w, *(['--ms', ms] if ms else []))
            if r.returncode == 3:
                # No claimable task can mean a cap is full or a dependency is still running, not that the work is
                # done: exit only when this scheduler's milestones hold no agent task that is open, running or blocked.
                if budget['running'] == 0 and not work_left(ms): return
                n = None
            elif r.returncode != 0:
                log(f'[{w}] claim failed rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}'); return
            else:
                n = r.stdout.strip(); budget['left'] -= 1; budget['running'] += 1
        if n is None:
            time.sleep(budget['idle']); continue
        log(f'[{w}] {n} START')
        try:
            run_task(w, n)
        except Exception as e:
            log(f'[{w}] {n} ERROR {e}'); tm('release', n, '--status', 'blocked')
        with budget['lock']:
            budget['running'] -= 1


def main():
    p = argparse.ArgumentParser(); p.add_argument('--workers', type=int, default=3)
    p.add_argument('--ms', help='comma-separated milestones to serve; default: every milestone')
    p.add_argument('--max', type=int, default=-1); p.add_argument('--prefix', default='w')
    p.add_argument('--idle', type=int, default=30, help='seconds a worker waits when nothing is claimable'); a = p.parse_args()
    budget = {'lock': threading.Lock(), 'left': a.max, 'running': 0, 'idle': a.idle}
    # A running scheduler is a `sched:<prefix>` row in the locks table: `tm.py lock list` shows it with its
    # milestones, the watchdog reads it to tell whether one runs, and a second start with the same prefix is refused.
    c = sqlite3.connect(DB, timeout=60, isolation_level=None); c.row_factory = sqlite3.Row
    name = f'sched:{a.prefix}'
    if not lock_try(c, name, name, 30 * 86400, f'workers={a.workers} ms={a.ms or "all"}'):
        sys.exit(f'a scheduler with prefix {a.prefix} already runs; pick another prefix')
    try:
        # The start line names the milestones, so a later session knows what this scheduler serves.
        log(f'START sched prefix={a.prefix} workers={a.workers} ms={a.ms or "all"}')
        ts = [threading.Thread(target=worker, args=(f'{a.prefix}{i}', a.ms, budget)) for i in range(a.workers)]
        for t in ts: t.start(); time.sleep(2)
        for t in ts: t.join()
        log(f'END sched prefix={a.prefix}: no claimable work left in {a.ms or "all milestones"}')
    finally:
        c.execute('DELETE FROM locks WHERE name=? AND host=? AND pid=?', (name, socket.gethostname(), os.getpid()))


if __name__ == '__main__': main()

#!/usr/bin/env python3
"""Unblock daemon: every 2 min, give each blocked task up to 2 unblock attempts and log each in SQLite.

A review-fail block gets a fix round on its latest review, then a fresh review, then close (finish.py).
A block whose exec asks a question (`BLOCKED:`) or a timeout is requeued with the reason attached; the
scheduler then reruns it. After 2 failed attempts the block is escalated and the watchdog wakes the
orchestrator. Every block and attempt goes to the `blocks` table (`tm.py block list`).
Usage: unblock.py [--once]; `--once` makes one pass and exits.
"""
import os, re, sqlite3, subprocess, sys, time

S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, S)
import host
TM = ['python3', f'{S}/tm.py']
AR = host.RUNS
os.makedirs(AR, exist_ok=True)


def q(sql, *a):
    c = host.connect_ro(30)
    try: return c.execute(sql, a).fetchall()
    finally: c.close()


def tm(*a):
    return subprocess.run(TM + [str(x) for x in a], capture_output=True, text=True, cwd=host.ROOT).stdout.strip()


def log(m):
    with open(f'{AR}/unblock.log', 'a') as f: f.write(time.strftime('%H:%M:%S ') + m + '\n')


def classify(n):
    ex = f'{AR}/task_{n}_exec.md'
    if os.path.exists(ex) and re.search(r'^BLOCKED:', open(ex).read(), re.M):
        return 'blocked-question', re.search(r'^BLOCKED:(.*)', open(ex).read(), re.M).group(1).strip()[:300]
    revs = sorted([f for f in os.listdir(AR) if re.match(rf'task_{n}_review(_\d+)?\.md$', f)],
                  key=lambda f: int((re.findall(r'_(\d+)\.md$', f) or ['1'])[0]))
    if revs:
        txt = open(f'{AR}/{revs[-1]}').read()
        fails = [l.strip() for l in txt.splitlines() if re.search(r'blocking|\bfail', l, re.I) and 'Verdict' not in l and 'not blocking' not in l.lower()]
        return 'review-fail', ' | '.join(fails[:3])[:300] or 'review failed'
    return 'timeout', 'no review: exec timed out or left no exec file'


def one_pass():
    # `[orch]` and `[opus]` blocks are the orchestrator's; the watchdog reports them.
    for (n,) in q("SELECT id FROM tasks WHERE status='blocked' AND kind='agent' AND title NOT LIKE '[opus]%' AND title NOT LIKE '[orch]%'"):
        # The list is read once per pass and one attempt can take 30 min: re-check, or a task the orchestrator
        # closed meanwhile gets reopened by finish.py.
        if q('SELECT status FROM tasks WHERE id=?', n)[0][0] != 'blocked':
            continue
        rows = q('SELECT attempt, outcome FROM blocks WHERE task=? ORDER BY id', n)
        attempts = [a for a, _ in rows if a > 0]
        if rows and rows[-1][1] == 'escalated':
            continue
        kind, reason = classify(n)
        zero = None
        if not rows or rows[-1][0] > 0 and rows[-1][1] in ('unblocked',):
            zero = tm('block', 'add', '--task', n, '--kind', kind, '--reason', reason, '--attempt', 0, '--outcome', 'pending')
        if len(attempts) >= 2:
            tm('block', 'add', '--task', n, '--kind', kind, '--reason', 'two unblock attempts failed', '--attempt', 0, '--outcome', 'escalated')
            log(f'{n} escalated after 2 attempts'); continue
        k = len(attempts) + 1
        bid = tm('block', 'add', '--task', n, '--kind', kind, '--reason', reason, '--attempt', k, '--outcome', 'pending')
        log(f'{n} attempt {k} ({kind})')
        if kind == 'review-fail':
            try: subprocess.run(['python3', f'{S}/finish.py', str(n), '--fix'], capture_output=True, text=True, timeout=3600, cwd=host.ROOT)
            except subprocess.TimeoutExpired: pass
            st = q('SELECT status FROM tasks WHERE id=?', n)[0][0]
            out = 'unblocked' if st in ('done', 'skipped') else 'failed'
        else:
            # Requeue with the reason attached; the scheduler reruns it from a clean exec file.
            w = q('SELECT what FROM tasks WHERE id=?', n)[0][0] or ''
            tm('edit', n, '--what', w + f'\n- Unblock attempt {k}: the previous run stopped with: {reason}. Resolve it within the task, using any default the task names.', '--status', 'open')
            out = 'unblocked'
        tm('block', 'end', '--id', bid, '--outcome', out)
        if out == 'unblocked' and zero: tm('block', 'end', '--id', zero, '--outcome', 'unblocked')  # the block itself ends too
        log(f'{n} attempt {k} {out}')


if __name__ == '__main__':
    while True:
        one_pass()
        if '--once' in sys.argv: break
        time.sleep(120)

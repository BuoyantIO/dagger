#!/usr/bin/env python3
"""Finish one task outside the scheduler: optional fix from its last review, a fresh review, then close and commit.

Usage: finish.py <n> [--fix]. Reuses the scheduler's prompts so the steps match a normal run.
"""
import os, re, subprocess, sys
S = os.path.dirname(os.path.abspath(__file__)); argv = sys.argv
# The scheduler's module body without its entry point: importing it by name would shadow the stdlib `sched`.
src = open(f'{S}/sched.py').read().split('\ndef main():')[0]
exec(compile(src, f'{S}/sched.py', 'exec'))
n = argv[1]; fix = '--fix' in argv
revs = sorted([f for f in os.listdir(AR) if re.match(rf'task_{n}_review(_\d+)?\.md$', f)], key=lambda f: int((re.findall(r'_(\d+)\.md$', f) or ['1'])[0]))
k = len(revs) + 1
# pi() skips any task that is not running (a re-split or manual close), so a blocked task is marked running first
# and put back to blocked if the review still fails.
subprocess.run(TM + ['edit', n, '--status', 'running', '--force'], cwd=ROOT, capture_output=True)
if fix and revs:
    pi('f', n, f'fix{k - 1}', FIX.format(n=n, rf=revs[-1]), 1800)
rf = f'task_{n}_review_{k}.md'
pi('f', n, f'review{k}', REVIEW.format(n=n, rf=rf), 900)
v = verdict(rf); print(n, 'review', k, v)
if v == 'PASS':
    want = want_commit()
    pi('f', n, 'close', CLOSE.format(n=n, rf=rf, commit=COMMIT_LIST.format(n=n) if want else COMMIT_NO), 600)
    st = status(n)
    if st in ('done', 'skipped') and want and not committed(n):
        commit_listed('f', n)
    print(n, 'status', st, 'committed', committed(n) if want else 'off')
    if st == 'running':
        subprocess.run(TM + ['edit', n, '--status', 'blocked', '--force'], cwd=ROOT, capture_output=True)
else:
    subprocess.run(TM + ['edit', n, '--status', 'blocked', '--force'], cwd=ROOT, capture_output=True)

#!/usr/bin/env python3
"""The three cases that decide whether `tm.py claim` honours `tm.py lint`.

A row that fails lint must never be claimed: it stays `open`, the scheduler records why in `meta` as
`claim_skip:<id>`, and the claim moves on to the next ready row. A task that fails lint and no clean task
behind it must leave the claim empty rather than take the broken row. The clean row's What names the file it
writes, so claim proves the restored path check takes a task's output for an output: when it could not tell
them apart, claim skipped every ready task. The third row reads a file nothing writes, so claim must skip it
for the path check itself, not only for the other checks. The db is a temp file built by `tm.py import`
through `DAG_DB`, so the real checklist never sees the fixture tasks.
Usage: test_claim_lint.py; prints each case and exits 1 unless all three behave as stated.
"""
import os, shutil, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# The fixture as a checklist. Task 1 fails lint because its estimate is over the 30 min rule; task 2 is clean
# and comes second in milestone order, so only the lint skip can make claim pass over task 1. Task 2's What
# names a file nothing writes, and says it writes it, so the path check must accept it for claim to reach it.
# Task 3 reads a path no task writes, so only the path check can make claim skip it.
FIXTURE = """## Tasks

### M1. Claim lint fixtures

1. - [ ] (agent) task that fails lint
2. - [ ] (agent) clean task
3. - [ ] (agent) task whose input nothing writes

## Context

## 1. task that fails lint

Details:
Reasoning: medium. Time budget: estimate 45 min, hard cap 90 min.

## 2. clean task

What:
Write `fixtures/claim_clean_output.md` with one line.

Details:
Reasoning: low. Time budget: estimate 12 min, hard cap 24 min.

## 3. task whose input nothing writes

What:
Read `fixtures/claim_missing_input.py` and paste its header.

Details:
Reasoning: low. Time budget: estimate 12 min, hard cap 24 min.
"""


def run(db, *argv):
    """Run one tm.py subcommand against the fixture db: its exit code and combined output are the evidence."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.splitlines()


def case(n, name, ok, out):
    """Print one case as judged: the stated outcome, the tool's own lines, then PASS or FAIL."""
    print(f'case {n}: {name}')
    for line in out:
        print(f'    {line}')
    print(f'  {"PASS" if ok else "FAIL"}\n')
    return ok


def main():
    tmp = tempfile.mkdtemp(prefix='claim_lint_')
    db = f'{tmp}/tasks.db'
    try:
        md = f'{tmp}/fixture.md'
        with open(md, 'w') as f:
            f.write(FIXTURE)
        rc, out = run(db, 'import', md)
        print('fixture: ' + ' '.join(out) + f'\n  exit {rc}: {"PASS" if rc == 0 else "FAIL"}\n')
        code, out = run(db, 'claim', '--worker', 'test')
        c = sqlite3.connect(db)
        skip = c.execute("SELECT value FROM meta WHERE key='claim_skip:1'").fetchone()
        status = dict(c.execute('SELECT id, status FROM tasks'))
        c.close()
        # Case 1: task 1 fails lint, so claim prints the clean task 2 and records the failure under claim_skip:1.
        # Task 2's What names the output it writes, so this also proves the path check accepts an output here.
        ok = (code == 0 and out == ['2'] and skip is not None
              and 'estimate 45 min is over 30' in skip[0] and status[1] == 'open' and status[2] == 'running')
        c1 = case(1, 'claim skips a row that fails lint and prints the clean id', ok,
                  [f'claim exit {code}, stdout {out}',
                   f"claim_skip:1 = {skip[0]!r}" if skip else "claim_skip:1 = missing",
                   f'tasks: {sorted(status.items())}'])
        # Case 2: every remaining ready row was claimed or passed over, so a second claim finds nothing and
        # exits 3; task 1 is still open rather than stuck in running, and task 3 was passed over by the path
        # check alone, which is what claim now runs.
        code2, out2 = run(db, 'claim', '--worker', 'test')
        c = sqlite3.connect(db)
        left = dict(c.execute('SELECT id, status FROM tasks'))
        skip3 = c.execute("SELECT value FROM meta WHERE key='claim_skip:3'").fetchone()
        c.close()
        # Case 3: task 3's What reads a file nothing writes, so claim_skip:3 carries the path line, not another.
        ok3 = skip3 is not None and 'names fixtures/claim_missing_input.py' in skip3[0]
        c2 = case(2, 'a task that fails lint is never claimed', code2 == 3 and left[1] == 'open',
                  [f'second claim exit {code2}, stdout {out2}', f'tasks: {sorted(left.items())}'])
        c3 = case(3, 'claim runs the path check on a task that reads a missing file', ok3,
                  [f"claim_skip:3 = {skip3[0]!r}" if skip3 else 'claim_skip:3 = missing'])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    allok = rc == 0 and c1 and c2 and c3
    print('all three cases hold' if allok else 'a case failed')
    return 0 if allok else 1


def test_claim_lint_cases():
    """All three cases hold: the pytest entry for the script, so `python3 -m pytest dag/src/` runs what `python3 dag/src/test_claim_lint.py` prints."""
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""The three checks that decide whether `tm.py cost` reports tokens and cost a reader can trust.

Cost is a sum nobody should do by hand, so each check states the number it must print before it runs: a closed
task's line carries its two runs' counts (150 in, 15 out) and the price applied to each, the total line repeats
those numbers so the columns add up, and an open task's run stays out of both. The db is a temp file given
through `DAG_DB`, so the real runs table never sees the fixture rows.
Usage: test_cost.py; prints each check and exits 1 unless all three behave as stated.
"""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# Two tasks is the smallest fixture that can show a closed task's line beside an open one's exclusion. Task 1
# is closed in the checklist itself, so `cost --ms M1` has one task to print.
FIXTURE = """## Tasks

### M1. Cost fixtures

1. - [x] (agent) closed task with two runs (done)
2. - [ ] (agent) open task with one run

## 1. closed task with two runs

Details:
Reasoning: low. Time budget: estimate 1 min, hard cap 2 min.

## 2. open task with one run

Details:
Reasoning: low. Time budget: estimate 1 min, hard cap 2 min.
"""

# Prices in USD per million tokens; 3 and 15 are unequal so a swapped in/out rate cannot pass.
PRICES = ('--price-in', '3', '--price-out', '15')


def tm(db, *argv):
    """Run one command against the fixture db: its exit code and combined output are the evidence."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.strip()


def start_end(db, task, tin, tout):
    """Record one finished step with a token count: the counts of both commands, which must both exit 0."""
    rc, out = tm(db, 'run', 'start', '--task', str(task), '--step', 'exec', '--worker', 'w1')
    rid = out.splitlines()[-1]
    rc2, _ = tm(db, 'run', 'end', '--run', rid, '--rc', '0', '--tokens-in', str(tin), '--tokens-out', str(tout))
    return rc, rc2


def lines(db, *args):
    """The command's output split into tab-separated fields, so a check compares fields, not a whole string."""
    rc, out = tm(db, 'cost', '--ms', 'M1', *args)
    return rc, [ln.split('\t') for ln in out.splitlines() if ln]


def case(n, name, want, got):
    """Print one check as judged: what it must find, what it found, and PASS or FAIL."""
    ok = got == want
    print(f'check {n}: {name}\n  want: {want}\n  got:  {got}\n  {"PASS" if ok else "FAIL"}\n')
    return ok


def main():
    tmp = tempfile.mkdtemp(prefix='cost_')
    db = f'{tmp}/tasks.db'
    try:
        md = f'{tmp}/fixture.md'
        with open(md, 'w') as f:
            f.write(FIXTURE)
        rc, out = tm(db, 'import', md)
        print(f'fixture: {out.splitlines()[-1]}\n  exit {rc}: {"PASS" if rc == 0 else "FAIL"}\n')
        # Three runs, one of them on the open task so its exclusion is a number and not an absence.
        a = start_end(db, 1, 100, 10)
        b = start_end(db, 1, 50, 5)
        d = start_end(db, 2, 200, 20)
        print(f'runs: task 1 exit {a} and {b}, task 2 exit {d}\n')
        # Check 1: one line per closed task, carrying that task's step count, counts and cost. 150 in and 15 out
        # at 3 and 15 USD per million is 0.00045 + 0.000225.
        rc1, rows = lines(db, *PRICES)
        print(f'check 1 command: cost exit {rc1}, every line {rows}\n')
        c1 = case(1, 'the closed task has one line with its own sums and price', [['1', '2', '150', '15', '165', '0.000675']],
                  [r for r in rows if r[0] != 'total'])
        # Check 2: the total line sums the printed rows, so the open task's 200 in stays out.
        c2 = case(2, 'the total line sums the closed task only', [['total', '2', '150', '15', '165', '0.000675']],
                  [r for r in rows if r[0] == 'total'])
        # Check 3: the default prices are 0, so the cost is 0 while the token counts are unchanged.
        rc3, rows3 = lines(db)
        print(f'check 3 command: cost exit {rc3}, every line {rows3}\n')
        c3 = case(3, 'default prices leave the counts and print a zero cost',
                  [['1', '2', '150', '15', '165', '0.000000'], ['total', '2', '150', '15', '165', '0.000000']], rows3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    allok = all(x == 0 for x in (rc, *a, *b, *d)) and c1 and c2 and c3
    print('all three checks hold' if allok else 'a check failed')
    return 0 if allok else 1


def test_cost_cases():
    """All three checks hold: the pytest entry for the script, so `python3 -m pytest dag/src/` runs what `python3 dag/src/test_cost.py` prints."""
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())

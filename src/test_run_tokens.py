#!/usr/bin/env python3
"""The three checks that decide whether `tm.py run` can be trusted to store a step's tokens.

A sum no column holds is unusable, so each check says up front what it must print: `run end --tokens-in 7
--tokens-out 3` leaves 7 and 3 on the row `run start` created, an end with no token flags keeps the counts
already there, and a db whose `runs` table predates the columns gains them on its next command. The db is a
temp file given through `DAG_DB`, so the real runs table never sees the fixture rows.
Usage: test_run_tokens.py; prints each check and exits 1 unless all three behave as stated.
"""
import os, shutil, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# The smallest checklist `import` accepts: one milestone with one task, which is all a run row needs for its
# foreign key. Task ids are permanent, so the fixture task is 1 and every run below names it.
FIXTURE = """## Tasks

### M1. Run fixtures

1. - [ ] (agent) task to record runs against

## 1. task to record runs against

Details:
Reasoning: low. Time budget: estimate 1 min, hard cap 2 min.
"""

# The `runs` table as a db from before these columns existed, with the fixture task it points at.
OLD_SCHEMA = """CREATE TABLE tasks (id INTEGER PRIMARY KEY);
CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL, step TEXT NOT NULL,
                   worker TEXT, started REAL NOT NULL, ended REAL, rc TEXT, verdict TEXT);
INSERT INTO tasks (id) VALUES (1);
"""


def tm(db, *argv):
    """Run one command against the fixture db: its exit code and combined output are the evidence."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.strip()


def row(db, rid):
    """The stored counts for one run id, read back from the db itself rather than from the command's word."""
    c = sqlite3.connect(db)
    try: return c.execute('SELECT tokens_in, tokens_out FROM runs WHERE id=?', (rid,)).fetchone()
    finally: c.close()


def columns(db):
    """The `runs` column names, so a missing migration is caught as a missing column, not as a NULL value."""
    c = sqlite3.connect(db)
    try: return {r[1] for r in c.execute('PRAGMA table_info(runs)')}
    finally: c.close()


def case(n, name, want, got):
    """Print one check as judged: what it must find, what it found, and PASS or FAIL."""
    ok = got == want
    print(f'check {n}: {name}\n  want: {want}\n  got:  {got}\n  {"PASS" if ok else "FAIL"}\n')
    return ok


def start_end(db, *end):
    """Start one run and end it with `end`'s flags: the run id, both exit codes and the end command's output."""
    rc, out = tm(db, 'run', 'start', '--task', '1', '--step', 'exec', '--worker', 'w1')
    rid = int(out.splitlines()[-1])
    rc2, out2 = tm(db, 'run', 'end', '--run', str(rid), *end)
    return rid, rc, rc2, out2


def main():
    tmp = tempfile.mkdtemp(prefix='run_tokens_')
    db = f'{tmp}/tasks.db'
    try:
        md = f'{tmp}/fixture.md'
        with open(md, 'w') as f:
            f.write(FIXTURE)
        rc, out = tm(db, 'import', md)
        print(f'fixture: {out.splitlines()[-1]}\n  exit {rc}: {"PASS" if rc == 0 else "FAIL"}\n')
        # Check 1: the token flags land on the row the start created.
        rid, rc1, rc2, out2 = start_end(db, '--rc', '0', '--tokens-in', '7', '--tokens-out', '3')
        print(f'check 1 commands: start exit {rc1}, end exit {rc2} {out2!r}\n')
        c1 = case(1, 'the two token flags are stored on the run', (7, 3), row(db, rid))
        # Check 2: an end with no token flags leaves the counts it found, rather than zeroing them.
        rc3, out3 = tm(db, 'run', 'end', '--run', str(rid), '--rc', '0', '--verdict', 'PASS')
        print(f'check 2 command: end exit {rc3} {out3!r}\n')
        c2 = case(2, 'a later end without flags keeps the counts', (7, 3), row(db, rid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Check 3: a db from before the columns gains them through the ALTER, and can then store a count.
    tmp = tempfile.mkdtemp(prefix='run_tokens_old_')
    old = f'{tmp}/tasks.db'
    try:
        c = sqlite3.connect(old)
        c.executescript(OLD_SCHEMA)
        c.close()
        # What the old table lacks, so the want is that same set coming back empty after the migration.
        before = {'tokens_in', 'tokens_out'} - columns(old)
        rid, rc1, rc2, out2 = start_end(old, '--rc', '0', '--tokens-in', '11', '--tokens-out', '5')
        print(f'check 3 commands: start exit {rc1}, end exit {rc2} {out2!r}'
              f', columns the old table lacked: {sorted(before)}\n')
        # The want is that same set coming back empty: nothing the old table lacked is still missing.
        c3 = case(3, 'an old runs table gains the columns and stores a count',
                  (set(), (11, 5)), ({'tokens_in', 'tokens_out'} - columns(old), row(old, rid)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    allok = rc == 0 and c1 and c2 and c3
    print('all three checks hold' if allok else 'a check failed')
    return 0 if allok else 1


def test_run_tokens_cases():
    """All three checks hold: the pytest entry for the script, so `python3 -m pytest dag/src/` runs what `python3 dag/src/test_run_tokens.py` prints."""
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())

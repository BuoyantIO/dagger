#!/usr/bin/env python3
"""`tm.py close --hold` keeps a dependent unclaimed until a follow-up task gates it.

Closing a task frees its dependents, and a worker claims one within seconds. When the close calls for a new
task ahead of a dependent, the hold must land in the same step as the close, and lift once the dependency is
wired. The db is a temp file built through `DAG_DB`, so no project database sees the fixture.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# Task 2 waits on task 1 only, so closing task 1 is all that stands between task 2 and a worker.
FIXTURE = """## Tasks

### M1. Hold fixtures

1. - [ ] (user) decision
2. - [ ] (agent) dependent

## 1. decision

What:
Decide.

Details:
estimate 5 min.

Done when:
Done.

## 2. dependent

What:
Write `out/dependent.md`.

Details:
estimate 5 min.
Depends: 1.

Done when:
`out/dependent.md` exists.
"""


def tm(db, *argv):
    """Run one `tm.py` command against the fixture db: its exit code and output lines."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.splitlines()


def test_close_hold_blocks_claim_until_dependency_added(tmp_path):
    """A dependent held by `close --hold` is not claimed, and `edit --add-deps` lifts the hold.

    Sets up a decision with one dependent, closes the decision with the dependent held, then adds a follow-up
    task and wires it in. Catches a hold that claim or ready ignores, one that outlives the dependency it
    waited for (the task would be stranded), and one that lifts while the follow-up is still open.
    """
    # Import the fixture: task 2 waits on task 1.
    db = str(tmp_path / 'tasks.db')
    md = tmp_path / 'fixture.md'
    md.write_text(FIXTURE)
    assert tm(db, 'init')[0] == 0
    assert tm(db, 'import', str(md))[0] == 0

    # Close the decision and hold its dependent in one command: nothing is ready, and claim comes back empty.
    rc, out = tm(db, 'close', '1', '--hold', '2')
    assert rc == 0, out
    assert tm(db, 'hold')[1] == ['2']
    assert tm(db, 'ready')[1] == []
    assert tm(db, 'claim', '--worker', 'w1')[0] == 3

    # Add the follow-up and gate the dependent on it: the hold is gone, and only the follow-up is ready.
    rc, out = tm(db, 'add', '--ms', 'M1', '--assessed', '--id', '3', '--title', 'follow-up',
                 '--what', 'Write `out/follow.md`.', '--details', 'estimate 5 min.', '--done-when', 'Done.')
    assert rc == 0, out
    assert tm(db, 'edit', '2', '--add-deps', '3')[0] == 0
    assert tm(db, 'hold')[1] == ['none']
    assert [l.split('\t')[0] for l in tm(db, 'ready')[1]] == ['3']

    # Close the follow-up: the dependent is claimable again, through its dependency and not through a hold.
    assert tm(db, 'close', '3')[0] == 0
    assert tm(db, 'claim', '--worker', 'w1')[1] == ['2']


def test_hold_clear_releases_task(tmp_path):
    """`tm.py hold --clear` makes a held task ready again when no follow-up turns out to be needed.

    Sets up the same decision and dependent, holds the dependent at the close, then clears the hold. Catches
    a clear that leaves the task unclaimable.
    """
    # Import the fixture and close the decision with the dependent held.
    db = str(tmp_path / 'tasks.db')
    md = tmp_path / 'fixture.md'
    md.write_text(FIXTURE)
    assert tm(db, 'init')[0] == 0
    assert tm(db, 'import', str(md))[0] == 0
    assert tm(db, 'close', '1', '--hold', '2')[0] == 0

    # Clear the hold: the dependent is ready with no new dependency.
    assert tm(db, 'hold', '--clear', '2')[1] == ['none']
    assert [l.split('\t')[0] for l in tm(db, 'ready')[1]] == ['2']

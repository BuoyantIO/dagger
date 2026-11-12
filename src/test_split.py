#!/usr/bin/env python3
"""`tm.py split` leaves parts a worker can claim.

The assess step writes the parts before the split, so their paths carry the id of the task being split. That
task becomes superseded, and lint fails any task naming a superseded one, so `claim` skipped every part. The
db is a temp file built through `DAG_DB`, so no project database sees the fixture.
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# Task 2 is the one split; task 3 waits on it, so the dependent rewrite is covered beside the parts.
FIXTURE = """## Tasks

### M1. Split fixtures

1. - [ ] (agent) setup
2. - [ ] (agent) big task
3. - [ ] (agent) reader

## 1. setup

What:
Nothing.

Details:
estimate 5 min.

Done when:
Done.

## 2. big task

What:
Write `out/task_2.md`.

Details:
estimate 40 min, exception: fixture.
Depends: 1.

Done when:
`out/task_2.md` exists.

## 3. reader

What:
Summarise the result of task 2.

Details:
estimate 5 min.
Depends: 2.

Done when:
Done.
"""


def tm(db, *argv, stdin=None):
    """Run one `tm.py` command against the fixture db: its exit code and output lines."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db}, input=stdin,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.splitlines()


def test_split_parts_pass_lint_and_share_names(tmp_path):
    """Parts and merge pass lint after a split, and a part file keeps one name where written and where read.

    Sets up a task split into two parts and a merge whose texts name the split task's id in paths and in prose,
    plus a bare `2` that is a count and the split task's exec file. Catches the split that leaves `task_2` in a
    part (lint fails it and no worker claims it), one that renames a file in the writer but not the reader, one
    that rewrites a bare number in a part, and one that points a part's exec file at the merge's.
    """
    # Import the fixture: task 1 open, task 2 waiting on it, task 3 waiting on task 2.
    db = str(tmp_path / 'tasks.db')
    md = tmp_path / 'fixture.md'
    md.write_text(FIXTURE)
    assert tm(db, 'init')[0] == 0
    assert tm(db, 'import', str(md))[0] == 0

    # Add the parts and the merge as the assess step does: paths named after task 2, before the split exists.
    def add(k, title, what, details, deps=None):
        argv = ['add', '--ms', 'M1', '--assessed', '--id', str(k), '--title', title, '--what', what,
                '--details', details, '--done-when', 'Done.']
        rc, out = tm(db, *argv, *(['--deps', deps] if deps else []))
        assert rc == 0, out
    add(4, 'big task: part 1 of 2', 'Map 2 scripts into `runs/task_2_part1_a.md`.',
        'Write `runs/task_2_part1_a.md`; estimate 12 min. Do not write `out/task_2.md`. Log to `runs/task_2_exec.md`.')
    add(5, 'big task: part 2 of 2', 'Read `runs/task_2_part1_a.md`; write `runs/task_2_part2_b.md`.',
        'estimate 12 min.', deps='4')
    add(6, 'big task: merge', 'Combine the parts of task 2 into `out/task_2.md`.',
        'Read `runs/task_2_part1_a.md` and `runs/task_2_part2_b.md`; estimate 10 min.', deps='4,5')
    rc, out = tm(db, 'split', '2', '--into', '4,5', '--merge', '6')
    assert rc == 0, out

    # Every part, the merge and the dependent pass lint: none names the superseded task 2.
    for n in ('4', '5', '6', '3'):
        rc, out = tm(db, 'lint', n)
        assert rc == 0, out

    # The file part 1 writes is the file part 2 and the merge read, and the bare count `2 scripts` is untouched.
    show = {n: '\n'.join(tm(db, 'show', n)[1]) for n in ('3', '4', '5', '6')}
    assert 'Map 2 scripts into `runs/task_6_part1_a.md`' in show['4']
    assert 'Read `runs/task_6_part1_a.md`; write `runs/task_6_part2_b.md`' in show['5']
    assert '`runs/task_6_part1_a.md` and `runs/task_6_part2_b.md`' in show['6']
    assert '`out/task_6.md`' in show['6'] and '`out/task_6.md`' in show['4']

    # Part 1 logs to its own exec file, not the merge's: the scheduler renames the merge's when the merge starts.
    assert '`runs/task_4_exec.md`' in show['4']

    # The dependent names the merge, as before this change.
    assert 'result of task 6' in show['3']

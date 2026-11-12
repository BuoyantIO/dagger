#!/usr/bin/env python3
"""The eight cases that decide whether `tm.py lint` can be trusted.

A check that never fires is worse than none, so each case says up front what it must print: an estimate over
the 30 min rule fails once, a task naming a superseded task fails once, a user task with no question or options
fails once, a task whose What names a path nothing writes fails once with that path, a task whose What names a
path its dependency names passes, a task that breaks none of the checks exits 0 with no failure line, a task
whose What says it writes the path passes, and a task whose Done when names the path passes. The last two are
the outputs the path check must never ask for: before it told them apart, the check failed every ready task.
The db is a temp file built by `tm.py import` through `DAG_DB`, so the real checklist never sees the fixture
tasks.
Usage: test_lint.py; prints each case and exits 1 unless all eight behave as stated.
"""
import os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'

# The fixture as a checklist. `import` writes the db from it, and the `[x] ... (superseded: ...)` line is the
# only way to get a superseded task without a write command, since those re-render the real checklist .md.
# Tasks 6 to 8 cover the path check: two paths that exist nowhere under the root, one produced by task 8.
# Tasks 9 and 10 name a path that exists nowhere and is the task's own output: 9 says so with a write verb in
# What, 10 only in its Done when. Task 6 keeps the guard that a read before the verb leaves the path an input.
FIXTURE = """## Tasks

### M1. Lint fixtures

1. - [ ] (agent) clean task
2. - [ ] (agent) task with an estimate over the cap
3. - [ ] (agent) task that names a superseded task
4. - [x] (agent) old task replaced by a split (superseded: split into 9)
5. - [ ] (user) user task with no question
6. - [ ] (agent) task whose input nothing writes
7. - [ ] (agent) task whose input its dependency names
8. - [ ] (agent) dependency that produces the input
9. - [ ] (agent) task whose What writes its own output
10. - [ ] (agent) task whose Done when names its own output

## Context

## 1. clean task

Details:
Reasoning: low. Time budget: estimate 12 min, hard cap 24 min.

## 2. task with an estimate over the cap

Details:
Reasoning: medium. Time budget: estimate 45 min, hard cap 90 min.

## 3. task that names a superseded task

What:
Rerun the same step as task 4 with the new flag.

## 4. old task replaced by a split

Details:
Not estimated; the fixture only needs the superseded status.

## 5. user task with no question

Details:
Reasoning: high. No question and no options were ever set.

## 6. task whose input nothing writes

What:
Read `fixtures/lint_missing_input.py` and write nothing.

Details:
Reasoning: low. Time budget: estimate 5 min, hard cap 10 min.

## 7. task whose input its dependency names

What:
Read `fixtures/lint_produced_input.py` and write nothing.

Details:
Reasoning: low. Time budget: estimate 5 min, hard cap 10 min. Depends on 8.

## 8. dependency that produces the input

What:
Write `fixtures/lint_produced_input.py` with one line.

Details:
Reasoning: low. Time budget: estimate 5 min, hard cap 10 min.

## 9. task whose What writes its own output

What:
- Create `fixtures/lint_own_output.md` with the two lists.
- Write `fixtures/lint_own_output_raw.txt` from the same run.

Details:
Reasoning: low. Time budget: estimate 5 min, hard cap 10 min.

## 10. task whose Done when names its own output

What:
Concatenate the four part files into `fixtures/lint_own_joined.md`; one table, sorted.

Done when:
- `fixtures/lint_own_joined.md` holds one table with no duplicate row.

Details:
Reasoning: low. Time budget: estimate 5 min, hard cap 10 min.
"""


def run(db, *argv):
    """Run one lint against the fixture db: its exit code and combined output are the evidence."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.splitlines()


def case(n, name, want, fails, code, out, *needles):
    """Print one case as judged: the stated outcome, the tool's own lines, and PASS or FAIL."""
    got = sum(1 for l in out if re.match(r'^\d+: ', l))
    ok = (code == want) and (got == fails) and all(any(nd in l for l in out) for nd in needles)
    print(f'case {n}: {name}\n  want: exit {want}, {fails} failure line(s)'
          + ''.join(f' and "{nd}"' for nd in needles))
    for line in out:
        print(f'    {line}')
    print(f'  exit {code}, {got} failure line(s), got {"all" if ok else "not all"} of what it wants: '
          f'{"PASS" if ok else "FAIL"}\n')
    return ok


def main():
    tmp = tempfile.mkdtemp(prefix='lint_')
    db = f'{tmp}/tasks.db'
    try:
        md = f'{tmp}/fixture.md'
        with open(md, 'w') as f:
            f.write(FIXTURE)
        rc, out = run(db, 'import', md)
        print('fixture: ' + ' '.join(out) + f'\n  exit {rc}: {"PASS" if rc == 0 else "FAIL"}\n')
        # Case 1: an estimate of 45 min with no `exception:` in Details.
        c1 = case(1, 'an estimate over 30 min fails once', 1, 1, *run(db, 'lint', '2'),
                  'estimate 45 min is over 30', '1 problem(s)')
        # Case 2: the text names task 4, which the fixture imported as superseded.
        c2 = case(2, 'a reference to a superseded task fails once', 1, 1, *run(db, 'lint', '3'),
                  'names task 4, which is superseded')
        # Case 3: a user task with neither question nor options, reported on one line for both.
        c3 = case(3, 'a user task with no question or options fails once', 1, 1, *run(db, 'lint', '5'),
                  'user task has no question or options')
        # Case 4: a 12 min agent task with no stale reference and no user fields.
        c4 = case(4, 'a clean task exits 0', 0, 0, *run(db, 'lint', '1'), 'ok')
        # Case 5: task 6's What names a path no task in the fixture writes, so the path check must name it.
        c5 = case(5, 'a What path nothing writes fails once', 1, 1, *run(db, 'lint', '6'),
                  'names fixtures/lint_missing_input.py', '1 problem(s)')
        # Case 6: the same shape, but task 8's What names the path and task 7 depends on it.
        c6 = case(6, 'a What path a dependency names passes', 0, 0, *run(db, 'lint', '7'), 'ok')
        # Case 7: task 9's What says it creates one path and writes the other, so both are outputs, not inputs.
        c7 = case(7, 'a What path the task says it writes passes', 0, 0, *run(db, 'lint', '9'), 'ok')
        # Case 8: task 10's What only says `into`, so the Done when is what tells the path is an output.
        c8 = case(8, 'a Done when path passes', 0, 0, *run(db, 'lint', '10'), 'ok')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    allok = rc == 0 and c1 and c2 and c3 and c4 and c5 and c6 and c7 and c8
    print('all eight cases hold' if allok else 'a case failed')
    return 0 if allok else 1


def test_lint_cases():
    """All eight cases hold: the pytest entry for the script, so `python3 -m pytest dag/src/` runs what `python3 dag/src/test_lint.py` prints."""
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())

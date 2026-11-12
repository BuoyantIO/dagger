#!/usr/bin/env python3
"""The cases that decide whether `tm.py import` can be trusted with a checklist someone just wrote.

The documented example must import to the DAG the document describes, and a malformed checklist must import
nothing and name each problem by line. The db is a temp file given through `DAG_DB`, so the real one never
sees the fixtures.
Usage: python3 -m pytest -q dag/src/test_import.py
"""
import os, re, sqlite3, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TM = f'{HERE}/tm.py'
FORMAT = f'{os.path.dirname(HERE)}/CHECKLIST_FORMAT.md'

# One defect per marked line; the test finds each line number from its marker, so the fixture can be edited.
MALFORMED = """## Tasks

### Setup
1. - [ ] (agent) Task under a heading with no milestone id
### M1. Build
2. - [ ] agent Task line with no parentheses
3. - [ ] (agent) First task
3. - [ ] (agent) Second task with the same id
4. - [ ] (agent) Task with no section

## Context

## 3. First task

Details:
Depends: 9.

## 7. Section with no task line

What:
Nothing.
"""


def tm(db, *argv):
    """Run one command against the fixture db: its exit code, stdout lines and stderr lines are the evidence."""
    p = subprocess.run([sys.executable, TM, *argv], env={**os.environ, 'DAG_DB': db}, capture_output=True, text=True)
    return p.returncode, p.stdout.splitlines(), p.stderr.splitlines()


def test_documented_example_imports(tmp_path):
    """The example in CHECKLIST_FORMAT.md imports, passes `check`, and yields the dependencies its `Depends:` lines state.

    Sets up the example cut from between the document's markers. Catches the document and the importer drifting
    apart: an example that no longer imports, or a `Depends:` line that no longer becomes a dependency row.
    """
    # Cut the example from the document, so the test reads what a user would copy.
    doc = open(FORMAT).read()
    m = re.search(r'<!-- example:start -->\n~~~markdown\n(.*?)\n~~~\n<!-- example:end -->', doc, re.S)
    assert m, 'no example between the markers'
    md = tmp_path / 'example.md'; md.write_text(m.group(1) + '\n')
    db = str(tmp_path / 'tasks.db')

    # Import and check: 4 tasks in 2 milestones, 2 context entries, no problem.
    rc, out, errs = tm(db, 'import', str(md))
    assert (rc, out) == (0, ['imported 4 tasks, 2 milestones, 2 context entries']), out + errs
    assert tm(db, 'check')[:2] == (0, ['ok'])

    # The `Depends:` lines are rows, the kinds are as written, and only task 1 is ready.
    c = sqlite3.connect(db)
    assert sorted(c.execute('SELECT task, dep FROM deps')) == [(2, 1), (3, 1), (3, 2), (4, 3)]
    assert dict(c.execute('SELECT id, kind FROM tasks')) == {1: 'agent', 2: 'agent', 3: 'agent', 4: 'user'}
    c.close()
    assert [l.split('\t')[0] for l in tm(db, 'ready')[1]] == ['1']

    # Every agent task passes lint, so claim would hand each out; a render imports back to the same render.
    for n in ('1', '2', '3'):
        assert tm(db, 'lint', n)[0] == 0
    first = '\n'.join(tm(db, 'render')[1]) + '\n'
    again = tmp_path / 'again.md'; again.write_text(first)
    assert tm(db, 'import', '--force', str(again))[0] == 0
    assert '\n'.join(tm(db, 'render')[1]) + '\n' == first


def test_malformed_checklist_imports_nothing(tmp_path):
    """A checklist with seven defects exits 1, names each defect by file and line, and leaves no database.

    Sets up one file with a bad milestone heading, a task before any milestone, a bad task line, a duplicate
    id, a task with no section, a dependency on an unknown id and a section with no task line. Catches an
    importer that stops at the first problem, reports no line, or writes a partial database.
    """
    md = tmp_path / 'bad.md'; md.write_text(MALFORMED)
    db = str(tmp_path / 'tasks.db')
    rc, out, errs = tm(db, 'import', str(md))

    # Nothing imported: exit 1, no stdout, no database file.
    assert rc == 1 and out == [], out
    assert not os.path.exists(db)

    # Each defect is one `<file>:<line>: <message>` line, at the line that holds it.
    line = lambda text: MALFORMED.split('\n').index(text) + 1
    want = [(line('### Setup'), 'milestone heading must read'),
            (line('1. - [ ] (agent) Task under a heading with no milestone id'), 'before any'),
            (line('2. - [ ] agent Task line with no parentheses'), 'a task line must read'),
            (line('3. - [ ] (agent) Second task with the same id'), 'task id 3 appears twice'),
            (line('4. - [ ] (agent) Task with no section'), 'open agent task 4 has no'),
            (line('## 3. First task'), 'depends on task 9'),
            (line('## 7. Section with no task line'), 'has no task line')]
    for n, text in want:
        assert any(e.startswith(f'{md}:{n}: ') and text in e for e in errs), (n, text, errs)
    assert errs[-1].startswith('import: 7 problem(s)'), errs

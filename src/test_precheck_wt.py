#!/usr/bin/env python3
"""The cases that decide whether precheck resolves a citation and flags what it must.

`host.cite_roots()` gives the directories a cited `path:line` may resolve under beside the root: the
`cite_roots` globs of `project.json`, or `DAG_CITE_ROOTS` when set. The first case says what the variable
must make `cite_roots` and `resolve` do; the second runs the whole pre-check on a scratch copy of this folder.
Usage: python3 -m pytest -q dag/src/test_precheck_wt.py
"""
import importlib, os, shutil, sqlite3, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host, precheck


def reload_with(monkeypatch, **env):
    """The modules as one environment sees them: `host` reads its variables at import, so each case re-imports."""
    monkeypatch.delenv('DAG_CITE_ROOTS', raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    importlib.reload(host)
    return importlib.reload(precheck)


def test_cite_roots_follow_the_environment(monkeypatch, tmp_path):
    """`DAG_CITE_ROOTS` names the only extra roots, in order, and an absolute citation resolves without any.

    Sets up a two-line file in a fake repo tree so a citation resolves only when that tree is searched.
    Catches a root list that ignores the environment: every citation then reads as unplaceable and is skipped.
    """
    # Set: the named trees are searched in order, and a citation under one resolves to that tree's file.
    tree = tmp_path / 'wt_demo_repo'
    (tree / 'bin').mkdir(parents=True)
    (tree / 'bin' / 'build').write_text('#!/bin/sh\necho built\n')
    other = tmp_path / 'wt_demo_other'; other.mkdir()
    m = reload_with(monkeypatch, DAG_CITE_ROOTS=f'{other},{tree}')
    assert host.cite_roots() == [str(other), str(tree)]
    assert m.resolve('bin/build') == [str(tree / 'bin' / 'build')]

    # An absolute citation resolves on its own, whatever the roots are; a missing one resolves to nothing.
    assert m.resolve(str(tree / 'bin' / 'build')) == [str(tree / 'bin' / 'build')]
    assert m.resolve(str(tree / 'bin' / 'absent')) == []
    reload_with(monkeypatch)


def test_precheck_reports_each_finding(tmp_path):
    """One exec file with four defects yields four findings and exit 1; the corrected file yields none and exit 0.

    Sets up a scratch project (a copy of `host.py` and `precheck.py` under `<tmp>/dag/src`, a `runs` table, the
    runs dir, one repo file of two lines) and runs `precheck.py` as the scheduler does. Catches a check that
    never fires: a missing self-check section, a list line over 200 characters, a citation past the end of its
    file, and a citation on a blank line.
    """
    # Scratch project: the root is two levels above the copied src/, so nothing here touches the real one.
    dag = tmp_path / 'dag' / 'src'; dag.mkdir(parents=True); (tmp_path / 'dag' / 'db').mkdir()
    for name in ('host.py', 'precheck.py'):
        shutil.copy(f'{HERE}/{name}', dag / name)
    ar = tmp_path / 'dag' / 'runs'; ar.mkdir()
    tree = tmp_path / 'wt_demo_repo'; tree.mkdir()
    (tree / 'README.md').write_text('# Readme\n\n')
    # The db needs only the `runs` table `started` reads.
    c = sqlite3.connect(str(tmp_path / 'dag' / 'db' / 'tasks.db'))
    c.execute('CREATE TABLE runs (task INTEGER, step TEXT, started REAL)'); c.commit(); c.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith('DAG_')}
    env['DAG_CITE_ROOTS'] = str(tree)
    run = lambda: subprocess.run([sys.executable, str(dag / 'precheck.py'), '7'], env=env, capture_output=True, text=True)

    # Four defects: no self-check heading, a 210-character list line, `README.md:9` past the end, `README.md:2` blank.
    (ar / 'task_7_exec.md').write_text('# exec\n- ' + 'x' * 208 + '\n- step from `README.md:9`\n- step from `README.md:2`\n')
    r = run()
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout.splitlines()
    assert len(out) == 4, out
    assert any('Self-check' in l for l in out) and any('limit 200' in l for l in out)
    assert any('README.md:9' in l for l in out) and any('README.md:2' in l for l in out)

    # Corrected: the heading is there, the line is short, and the citation lands on the heading line.
    (ar / 'task_7_exec.md').write_text('# exec\n- step from `README.md:1`\n\n## Self-check\n- ok\n')
    r = run()
    assert (r.returncode, r.stdout) == (0, ''), r.stdout + r.stderr

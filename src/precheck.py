#!/usr/bin/env python3
"""Deterministic pre-review checks for one task: run by the scheduler between exec and review.

The model's self-check sometimes reports checks it did not run (wrong `file:line`, list lines over 200
characters, a missing self-check section), so the scheduler runs these itself and sends failures to a fix
round before review. Usage: precheck.py <task id>; prints findings and exits 1 when there are any.
"""
import os, re, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host

ROOT = host.ROOT
AR = host.RUNS

# A citation: a path with a directory (`bin/build:12`, `~/src/repo/README.md:3-9`) or a bare file name with a
# known extension (`README.md:40`), then a line or a line range.
CITE = re.compile(r'`?((?:~/|/)?(?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:md|ya?ml|toml|json|go|rs|py|sh|proto)):(\d+)(?:-(\d+))?`?')
TRIVIAL = re.compile(r'^\s*[{}()\[\],;]*\s*$')


def started(n):
    """When the task's latest exec began; files older than that were not written by this attempt."""
    c = host.connect_ro(60)
    r = c.execute("SELECT max(started) FROM runs WHERE task=? AND step='exec'", (int(n),)).fetchone()[0]
    return (r or 0) - 5


def task_files(n):
    """Files this attempt wrote: its runs-dir records plus the root deliverables its exec file names, changed since exec began."""
    ex = f'{AR}/task_{n}_exec.md'
    files = {ex} if os.path.exists(ex) else set()
    files |= {f'{AR}/{f}' for f in os.listdir(AR) if f.startswith(f'task_{n}_') and f.endswith('.md') and 'review' not in f and 'precheck' not in f}
    if os.path.exists(ex):
        for p in set(re.findall(r'`([\w./-]+\.md)`', open(ex).read())):
            if os.path.isfile(f'{ROOT}/{p}'):
                files.add(os.path.realpath(f'{ROOT}/{p}'))
    t0 = started(n)
    return sorted(f for f in files if os.path.getmtime(f) >= t0)


def resolve(path):
    """Every place a cited path exists: the same relative path can live under several cite roots."""
    if path.startswith(('~', '/')):
        p = os.path.expanduser(path)
        return [p] if os.path.isfile(p) else []
    return [os.path.join(b, path) for b in [ROOT] + host.cite_roots() if os.path.isfile(os.path.join(b, path))]


def main(n):
    out = []
    ex = f'{AR}/task_{n}_exec.md'
    if not os.path.exists(ex):
        out.append(f'no exec file {ex}')
    elif '## Self-check' not in open(ex).read():
        out.append('exec file has no `## Self-check` section')
    for f in task_files(n):
        rel = os.path.relpath(f, ROOT)
        for i, line in enumerate(open(f, errors='replace'), 1):
            if len(line.rstrip('\n')) > 200 and re.match(r'^ *- ', line):
                out.append(f'{rel}:{i}: list line of {len(line.rstrip())} characters (limit 200)')
            for m in CITE.finditer(line):
                path, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
                cands = resolve(path)
                if not cands:
                    continue  # a path we cannot place (another repo or a deleted file) is the reviewer's call
                def ok(p):
                    lines = open(p, errors='replace').read().split('\n')
                    # A single line on a brace is a likely off-by-N; a range only fails when it holds no code at all.
                    return a <= len(lines) and not all(TRIVIAL.match(l) for l in lines[a - 1:min(b, len(lines))])
                if not any(ok(p) for p in cands):
                    span = f'{a}' if a == b else f'{a}-{b}'
                    out.append(f'{rel}:{i}: cites {path}:{span}, which is past the end or holds only blank lines or braces in every tree that has the file')
    for o in out:
        print(o)
    sys.exit(1 if out else 0)


if __name__ == '__main__':
    main(sys.argv[1])

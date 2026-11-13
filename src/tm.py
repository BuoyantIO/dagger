#!/usr/bin/env python3
"""Task DAG: SQLite is the source of truth, and the live page (`tm.py serve`) is the view people read.

We keep tasks, milestones, dependencies, the decision log and run timings in `tasks.db`, so the
dependency graph is data rather than prose and parallel workers claim ready tasks atomically.
Run `tm.py -h` for commands. A markdown checklist is an intake format only: `tm.py import` reads one once.
"""
import argparse, glob, json, os, re, sqlite3, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host
ROOT = host.ROOT  # the project root: a task's relative path tokens are resolved against it
RUNS = os.path.relpath(host.RUNS, ROOT)  # the runs dir as task text names it, relative to the root
DB = host.DB
LABELS = ['Why', 'What', 'Details', 'Done when', 'Evidence', 'Result']
COLS = {'Why': 'why', 'What': 'what', 'Details': 'details', 'Done when': 'done_when',
        'Evidence': 'evidence', 'Result': 'result'}
# Closed statuses: a dependency in one of these no longer blocks its dependents.
CLOSED = ('done', 'superseded', 'skipped', 'obsolete')
STATUSES = ('open', 'running', 'blocked') + CLOSED
# Cap defaults when `meta` holds none: `max_running` gates claims, `max_opus` is read by the orchestrator.
MAX_RUNNING, MAX_OPUS = host.CAPS['max_running'], host.CAPS['max_opus']

SCHEMA = f"""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones (
  id TEXT PRIMARY KEY CHECK (id GLOB 'M[0-9]*'),
  title TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  ord INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  milestone TEXT NOT NULL REFERENCES milestones(id),
  ord INTEGER NOT NULL,          -- position in its milestone's list
  sec_ord INTEGER,               -- position of its section; NULL when it has none
  kind TEXT NOT NULL CHECK (kind IN ('agent', 'user')),
  title TEXT NOT NULL,
  sec_title TEXT,                -- the section heading when it differs from the title
  status TEXT NOT NULL CHECK (status IN {STATUSES}),
  suffix TEXT NOT NULL DEFAULT '', -- closing reason shown in parentheses on the task line
  why TEXT, what TEXT, details TEXT, done_when TEXT, evidence TEXT, result TEXT,
  body TEXT,                     -- raw section text for legacy sections that do not split into fields
  worker TEXT,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deps (
  task INTEGER NOT NULL REFERENCES tasks(id),
  dep INTEGER NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task, dep),
  CHECK (task != dep)
);
CREATE TABLE IF NOT EXISTS context (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  task INTEGER REFERENCES tasks(id),
  added REAL NOT NULL
);
-- Named mutexes for anything that must not run twice at once (a git commit, a scheduler prefix);
-- one row per held lock, so tasks.db is the single source of truth for who holds what.
CREATE TABLE IF NOT EXISTS locks (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,          -- free text: worker or task that holds it
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  at REAL NOT NULL,
  expires REAL NOT NULL,         -- a holder past this is stale and may be taken over
  cmd TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task INTEGER NOT NULL REFERENCES tasks(id),
  at REAL NOT NULL,
  kind TEXT NOT NULL,            -- review-fail, blocked-question, timeout, other
  reason TEXT NOT NULL,
  attempt INTEGER NOT NULL,      -- 0 = the block itself, 1..2 = unblock attempts
  outcome TEXT                   -- pending, unblocked, failed, escalated
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task INTEGER NOT NULL REFERENCES tasks(id),
  step TEXT NOT NULL,
  worker TEXT,
  started REAL NOT NULL,
  ended REAL,
  rc TEXT,
  verdict TEXT,
  tokens_in INTEGER,             -- pi's summed input tokens for the step; NULL when the step recorded none
  tokens_out INTEGER             -- pi's summed output tokens for the step; NULL when the step recorded none
);
"""


def connect(ro=False):
    if ro:
        # Read-only: inspecting the DAG never takes a write lock or touches the schema.
        c = host.connect_ro(60)
        c.row_factory = sqlite3.Row
        return c
    # Only `init` and `import` may create a database; anything else on a missing file is a wrong path, not a new DAG.
    if not os.path.exists(DB) and sys.argv[1:2] not in (['import'], ['init']):
        sys.exit(f'no task database at {DB}; run `tm.py init` first')
    c = sqlite3.connect(DB, timeout=60, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA)
    # A user task carries its question and options as data, so viewers show them without parsing prose.
    cols = {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
    # The summary and details give the evidence for the decision, shown above and inside an expander.
    for col in ('question', 'options', 'summary', 'facts'):
        if col not in cols:
            c.execute(f'ALTER TABLE tasks ADD COLUMN {col} TEXT')
    # assessed: the scheduler already asked whether this task should split before it runs (1 = yes, run as is).
    if 'assessed' not in cols:
        c.execute('ALTER TABLE tasks ADD COLUMN assessed INTEGER NOT NULL DEFAULT 0')
    # Token counts per step: a cost figure is read per pi step from runs, so a db from before these
    # columns existed must gain them or every cost query silently falls back to its NULL path.
    rcols = {r[1] for r in c.execute('PRAGMA table_info(runs)')}
    for col in ('tokens_in', 'tokens_out'):
        if col not in rcols:
            c.execute(f'ALTER TABLE runs ADD COLUMN {col} INTEGER')
    return c


# ---------- import from the markdown checklist ----------

TASK_RE = re.compile(r'^(\d+)\. - \[( |x)\] \((agent|user)\) (.*)$')
# The dependency line render writes into Details (`Depends: 3, 4.`) and the prose form (`Depends on 3 to 5.`):
# import reads either into `deps` rows and strips it, so a `tm.py render` export imports back to the same DAG.
DEP_LINE = re.compile(r'^Depends: ([^\n]*)\.$', re.M)
DEP_SENT = re.compile(r'\s*(?:Depends on [^.]*\.|No dependencies\.|^Depends: [^\n]*\.$)', re.M)


def split_suffix(title, closed):
    """Split a closed task line's trailing `(reason)` from its title."""
    if not closed:
        return title, ''
    m = re.search(r' \(([^()]*(?:\([^()]*\)[^()]*)*)\)$', title)
    return (title[:m.start()], m.group(1)) if m else (title, '')


def status_of(closed, suffix):
    if not closed:
        return 'open'
    s = suffix.lower()
    for k in ('superseded', 'skipped', 'obsolete'):
        if s.startswith(k):
            return k
    return 'done'


def split_fields(text):
    """Split a section body at the label lines; None when it is not in the standard order."""
    parts, cur, buf, seen = {}, None, [], []
    pre = []
    for line in text.split('\n'):
        lab = line[:-1] if line.endswith(':') and line[:-1] in LABELS else None
        if lab:
            if cur: parts[cur] = '\n'.join(buf).strip('\n')
            elif any(l.strip() for l in buf): return None
            if lab in seen: return None
            seen.append(lab); cur, buf = lab, []
        else:
            buf.append(line)
    if cur: parts[cur] = '\n'.join(buf).strip('\n')
    if [l for l in LABELS if l in seen] != seen or not seen:
        return None
    return parts


def parse_deps(details):
    """Dependency ids named in a section's Details: the rendered `Depends:` line first, else a `Depends on` sentence."""
    m = DEP_LINE.search(details or '') or re.search(r'Depends on ([^.]*)\.', details or '')
    if not m:
        return []
    ids = []
    for a, b in re.findall(r'(\d+)(?:\s*to\s*(\d+))?', re.sub(r'\bM\d+\b', '', m.group(1))):
        ids += list(range(int(a), int(b or a) + 1))
    return ids


def has_tasks():
    """Whether the database holds any task: an empty one, as `init` leaves it, may be replaced by an import."""
    if not os.path.exists(DB): return False
    c = host.connect_ro(60)
    try: return bool(c.execute('SELECT count(*) FROM tasks').fetchone()[0])
    except sqlite3.OperationalError: return False
    finally: c.close()


def cmd_init(a):
    """Create an empty task database and the runs dir for a fresh project; an existing database is kept as it is."""
    os.makedirs(host.RUNS, exist_ok=True); os.makedirs(os.path.dirname(DB), exist_ok=True)
    new = not os.path.exists(DB)
    c = connect()
    for k, v in (('max_running', MAX_RUNNING), ('max_opus', MAX_OPUS)):
        c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (k, str(v)))
    print(f"{'created' if new else 'kept'} {DB}")


def cmd_import(a):
    """Build the database from a markdown checklist (the format is in CHECKLIST_FORMAT.md).

    The file is parsed whole before the database is touched: a malformed checklist prints every problem as
    `<file>:<line>: <message>` and exits 1, leaving any existing database as it was.
    """
    if has_tasks() and not a.force:
        sys.exit(f'{DB} holds tasks; pass --force to replace it')
    try: lines = open(a.md).read().split('\n')
    except OSError as e: sys.exit(f'import: cannot read {a.md}: {e.strerror}')
    errs = []
    err = lambda i, msg: errs.append(f'{a.md}:{i + 1}: {msg}')
    i = 0; ms = None; t_ord = 0; pre = []; head = False
    mss = []; notes = {}; tasks = {}; at = {}
    # Task lists: from `## Tasks` up to the next `## ` heading (`## Context`, or the first task section).
    while i < len(lines) and not (lines[i].startswith('## ') and lines[i] != '## Tasks'):
        l = lines[i]
        m = re.match(r'^### (M\d+)\. (.+)$', l)
        if l == '## Tasks':
            head = True
        elif m:
            ms = m.group(1); t_ord = 0
            if ms in notes: err(i, f'milestone {ms} appears twice')
            else: mss.append((ms, m.group(2))); notes[ms] = []
        elif l.startswith('### '):
            err(i, 'a milestone heading must read `### M<k>. <title>`')
        elif TASK_RE.match(l):
            n, x, kind, title = TASK_RE.match(l).groups()
            title, suffix = split_suffix(title, x == 'x')
            t_ord += 1
            if ms is None: err(i, f'task {n} comes before any `### M<k>. <title>` milestone heading')
            elif int(n) in tasks: err(i, f'task id {n} appears twice (first on line {at[int(n)] + 1})')
            else:
                at[int(n)] = i
                tasks[int(n)] = dict(id=int(n), milestone=ms, ord=t_ord, kind=kind,
                                     title=title, status=status_of(x == 'x', suffix), suffix=suffix)
        elif re.match(r'^\s*\d+\.\s', l) or re.match(r'^\s*- \[', l):
            err(i, 'a task line must read `<n>. - [ ] (agent) <title>` or `<n>. - [ ] (user) <title>`, with `[x]` when closed')
        elif ms and l.strip():
            notes[ms].append(l)
        elif not ms and not l.startswith('<!-- Exported by tm.py render'):
            pre.append(l)
        i += 1
    if not head: err(0, 'no `## Tasks` heading before the task lists')
    if not tasks and not errs: err(0, 'no task line found under `## Tasks`')
    # Context (optional): prose before the first list item is kept verbatim as the intro (blank lines and code
    # blocks included); each top-level list item after it, with its nested lines, is one decision-log entry.
    intro = []; ctx = []
    if i < len(lines) and lines[i] == '## Context':
        i += 1
        while i < len(lines) and not lines[i].startswith('## '):
            l = lines[i]
            if l.startswith('- '): ctx.append(l)
            elif ctx and l.strip(): ctx[-1] += '\n' + l
            elif not ctx: intro.append(l)
            i += 1
    # Any other `## ` part before the first task section (open questions, notes), kept verbatim.
    oq = []
    while i < len(lines) and not re.match(r'^## \d+\. ', lines[i]):
        oq.append(lines[i]); i += 1
    # Sections.
    sec_ord = 0
    while i < len(lines):
        m = re.match(r'^## (\d+)\. (.*)$', lines[i])
        j = i + 1
        while j < len(lines) and not re.match(r'^## \d+\. ', lines[j]): j += 1
        body = '\n'.join(lines[i + 1:j]).strip('\n')
        n = int(m.group(1)); sec_ord += 1
        t = tasks.get(n)
        if t is None:
            err(i, f'section `## {n}.` has no task line under `## Tasks`')
        elif 'sec_ord' in t:
            err(i, f'task {n} has two sections')
        else:
            t['sec_ord'] = sec_ord
            if m.group(2) != t['title']: t['sec_title'] = m.group(2)
            f = split_fields(body)
            if f is None:
                t['body'] = body
            else:
                for lab, v in f.items(): t[COLS[lab]] = v
            t['_deps'] = parse_deps(t.get('details') or body)
            for d in t['_deps']:
                if d == n: err(i, f'task {n} depends on itself')
                elif d not in tasks: err(i, f'task {n} depends on task {d}, which has no task line')
            if f is not None and t.get('details') is not None:
                t['details'] = DEP_SENT.sub('', t['details']).strip()
        i = j
    # An open agent task with no section gives a worker nothing to do.
    for n, t in tasks.items():
        if t['status'] == 'open' and t['kind'] == 'agent' and 'sec_ord' not in t:
            err(at[n], f'open agent task {n} has no `## {n}. <title>` section')
    if errs:
        sys.exit('\n'.join(errs + [f'import: {len(errs)} problem(s) in {a.md}; nothing was imported']))
    if os.path.exists(DB):
        for s in ('', '-wal', '-shm'):
            if os.path.exists(DB + s): os.remove(DB + s)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = connect(); now = time.time()
    c.execute('BEGIN')
    for o, (mid, title) in enumerate(mss, 1):
        c.execute('INSERT INTO milestones VALUES (?,?,?,?)', (mid, title, ''.join(x + '\n' for x in notes[mid]), o))
    for k, v in (('preamble', '\n'.join(pre).strip('\n')), ('max_running', str(MAX_RUNNING)), ('max_opus', str(MAX_OPUS)),
                 ('context_intro', '\n'.join(intro).strip('\n')), ('tail', '\n'.join(oq).strip('\n'))):
        c.execute('INSERT INTO meta VALUES (?,?)', (k, v))
    for t in ctx:
        c.execute('INSERT INTO context (text, added) VALUES (?,?)', (t, now))
    for t in tasks.values():
        deps = t.pop('_deps', [])
        t['updated'] = now
        cols = ','.join(t); q = ','.join('?' * len(t))
        c.execute(f'INSERT INTO tasks ({cols}) VALUES ({q})', list(t.values()))
        t['_deps'] = deps
    for t in tasks.values():
        for d in t['_deps']:
            c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (t['id'], d))
    # A dependency on a task superseded by a split points at the tasks that replaced it.
    for _ in range(3):
        rows = c.execute("""SELECT d.task, d.dep, u.suffix FROM deps d JOIN tasks u ON u.id=d.dep
                            WHERE u.status='superseded'""").fetchall()
        for task, dep, suf in rows:
            m = re.search(r'split into (\d+)(?: to (\d+))?', suf)
            if not m: continue
            c.execute('DELETE FROM deps WHERE task=? AND dep=?', (task, dep))
            for r in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1):
                if r != task and r in tasks:
                    c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (task, r))
    c.execute('COMMIT')
    print(f'imported {len(tasks)} tasks, {len(mss)} milestones, {len(ctx)} context entries')


# ---------- render ----------

def deps_of(c, n):
    return [r[0] for r in c.execute('SELECT dep FROM deps WHERE task=? ORDER BY dep', (n,))]


def task_line(t):
    box = ' ' if t['status'] in ('open', 'running', 'blocked') else 'x'
    suf = f" ({t['suffix']})" if t['suffix'] else ''
    return f"{t['id']}. - [{box}] ({t['kind']}) {t['title']}{suf}"


def section(c, t):
    out = [f"## {t['id']}. {t['sec_title'] or t['title']}", '']
    if t['body'] is not None:
        out.append(t['body'])
        return '\n'.join(out)
    d = deps_of(c, t['id'])
    dep_line = 'Depends: ' + (', '.join(map(str, d)) if d else 'none') + '.'
    for lab in LABELS:
        v = t[COLS[lab]]
        if lab == 'Details':
            v = (v + '\n' if v else '') + dep_line
        if v is None:
            continue
        out += [f'{lab}:', v, '']
    return '\n'.join(out).rstrip('\n')


def render(c):
    out = ['<!-- Exported by tm.py render from the task database; a snapshot, not maintained. -->', '']
    pre = c.execute("SELECT value FROM meta WHERE key='preamble'").fetchone()
    if pre and pre[0]: out += [pre[0], '']
    out += ['## Tasks', '']
    for ms in c.execute('SELECT * FROM milestones ORDER BY ord'):
        out.append(f"### {ms['id']}. {ms['title']}")
        if ms['note']: out.append(ms['note'].rstrip('\n'))
        for t in c.execute('SELECT * FROM tasks WHERE milestone=? ORDER BY ord', (ms['id'],)):
            out.append(task_line(t))
        out.append('')
    out += ['## Context', '']
    intro = c.execute("SELECT value FROM meta WHERE key='context_intro'").fetchone()
    if intro and intro[0]: out += [intro[0], '']
    out += [r[0] for r in c.execute('SELECT text FROM context ORDER BY id')]
    out.append('')
    tail = c.execute("SELECT value FROM meta WHERE key='tail'").fetchone()
    if tail and tail[0]: out += [tail[0], '']
    for t in c.execute('SELECT * FROM tasks WHERE sec_ord IS NOT NULL ORDER BY sec_ord'):
        out += [section(c, t), '']
    return '\n'.join(out).rstrip('\n') + '\n'


def cmd_render(a):
    """Export the DAG as a markdown checklist, on demand: to stdout, or to `--out <file>`.

    Nothing maintains the export: the database is the source of truth and the page is the view, so no other
    command writes a checklist. The export is in the import format, for a backup or a move to another project.
    """
    txt = render(connect(ro=True))
    if a.out:
        with open(a.out, 'w') as f: f.write(txt)
    else:
        sys.stdout.write(txt)


def checkpoint(c):
    # Fold the WAL into tasks.db so a git commit of the file carries every change.
    c.execute('PRAGMA wal_checkpoint(PASSIVE)')


# ---------- queries ----------

def get(c, n):
    t = c.execute('SELECT * FROM tasks WHERE id=?', (n,)).fetchone()
    if not t: sys.exit(f'no task {n}')
    return t


def held(c):
    """Task ids on hold: open tasks no one may claim until a follow-up task is wired in ahead of them."""
    r = c.execute("SELECT value FROM meta WHERE key='hold'").fetchone()
    return [int(x) for x in r[0].split(',') if x] if r and r[0] else []


def set_held(c, ids):
    c.execute("INSERT OR REPLACE INTO meta VALUES ('hold', ?)", (','.join(str(i) for i in sorted(set(ids))),))


def ready_rows(c, user=False):
    q = f"""SELECT t.* FROM tasks t JOIN milestones m ON m.id=t.milestone
            WHERE t.status='open' {'' if user else "AND t.kind='agent'"}
            AND NOT EXISTS (SELECT 1 FROM deps d JOIN tasks u ON u.id=d.dep
                            WHERE d.task=t.id AND u.status NOT IN {CLOSED})
            ORDER BY m.ord, t.ord"""
    # A held task is not ready: `close --hold` parks the dependents of the task it closes, so no worker claims
    # one in the seconds before the follow-up task and its dependency exist.
    hold = held(c)
    rows = [r for r in c.execute(q).fetchall() if r['id'] not in hold]
    # Within a milestone, start the task that unblocks the most work: rank by every open task downstream of it
    # (transitive), not only direct dependents, so the head of a long chain is not left behind a leaf.
    kids = {}
    for d, k in c.execute("""SELECT d.dep, d.task FROM deps d JOIN tasks u ON u.id=d.task
                              WHERE u.status IN ('open', 'blocked')"""):
        kids.setdefault(d, []).append(k)
    def downstream(i):
        seen, todo = set(), list(kids.get(i, []))
        while todo:
            x = todo.pop()
            if x not in seen: seen.add(x); todo.extend(kids.get(x, []))
        return len(seen)
    mo = {m: o for m, o in c.execute('SELECT id, ord FROM milestones')}
    return sorted(rows, key=lambda r: (mo[r['milestone']], -downstream(r['id']), r['ord']))


def cmd_show(a):
    c = connect(ro=True); t = get(c, a.id)
    if a.json:
        d = dict(t); d['deps'] = deps_of(c, a.id); print(json.dumps(d, indent=1))
    else:
        print(task_line(t)); print(); print(section(c, t) if t['sec_ord'] else '(no section)')


def cmd_ready(a):
    c = connect(ro=True)
    for t in ready_rows(c, a.user):
        print(f"{t['id']}\t{t['milestone']}\t{t['title']}")


def cmd_list(a):
    c = connect(ro=True); q = 'SELECT t.* FROM tasks t JOIN milestones m ON m.id=t.milestone WHERE 1=1'; p = []
    if a.ms: q += ' AND t.milestone=?'; p.append(a.ms)
    if a.status: q += ' AND t.status=?'; p.append(a.status)
    for t in c.execute(q + ' ORDER BY m.ord, t.ord', p):
        print(f"{t['id']}\t{t['milestone']}\t{t['status']}\t{t['title']}")


def cmd_stats(a):
    c = connect(ro=True)
    for r in c.execute("""SELECT milestone, status, count(*) n FROM tasks t JOIN milestones m ON m.id=t.milestone
                          GROUP BY milestone, status ORDER BY m.ord, status"""):
        print(f"{r['milestone']}\t{r['status']}\t{r['n']}")


def cmd_cost(a):
    """Tokens and cost per closed task in one milestone, summed from the per-step counts in `runs`.

    Prices are USD per million tokens. A step whose count is NULL sums as 0, so a milestone whose steps
    predate the token columns prints zeros rather than dropping its tasks; the total line sums the printed
    rows, so closed tasks only, and the columns add up for a reader who checks them by hand.
    """
    c = connect(ro=True)
    rows = c.execute(f"""SELECT t.id, count(r.id) steps, coalesce(sum(r.tokens_in), 0) tin,
                              coalesce(sum(r.tokens_out), 0) tout
                         FROM tasks t LEFT JOIN runs r ON r.task=t.id
                        WHERE t.milestone=? AND t.status IN ({','.join('?' * len(CLOSED))})
                        GROUP BY t.id ORDER BY t.ord""", (a.ms, *CLOSED)).fetchall()
    pin, pout = a.price_in / 1e6, a.price_out / 1e6
    steps = tin = tout = 0; cost = 0.0
    for r in rows:
        c_in, c_out = r['tin'] * pin, r['tout'] * pout
        print(f"{r['id']}\t{r['steps']}\t{r['tin']}\t{r['tout']}\t{r['tin'] + r['tout']}\t{c_in + c_out:.6f}")
        steps += r['steps']; tin += r['tin']; tout += r['tout']; cost += c_in + c_out
    print(f"total\t{steps}\t{tin}\t{tout}\t{tin + tout}\t{cost:.6f}")


def cmd_check(a):
    c = connect(ro=True); bad = 0
    for r in c.execute('SELECT task, dep FROM deps WHERE dep NOT IN (SELECT id FROM tasks)'):
        print(f'dangling dep {r[0]} -> {r[1]}'); bad += 1
    # Cycle check by DFS over the dependency rows.
    g = {}
    for r in c.execute('SELECT task, dep FROM deps'): g.setdefault(r[0], []).append(r[1])
    state = {}
    def dfs(u, path):
        nonlocal bad
        state[u] = 1
        for v in g.get(u, []):
            if state.get(v) == 1: print('cycle: ' + ' -> '.join(map(str, path + [u, v]))); bad += 1
            elif not state.get(v): dfs(v, path + [u])
        state[u] = 2
    sys.setrecursionlimit(10000)
    for u in list(g):
        if not state.get(u): dfs(u, [])
    for t in c.execute(f"SELECT id FROM tasks WHERE status='open' AND id IN (SELECT task FROM deps WHERE dep IN (SELECT id FROM tasks WHERE status='superseded'))"):
        print(f'open task {t[0]} depends on a superseded task'); bad += 1
    # The 30 min rule: an estimate above 30 min needs a recorded `exception:` in Details.
    for t in c.execute("SELECT id, details FROM tasks WHERE status IN ('open','blocked') AND kind='agent'"):
        m = re.search(r'estimate (\d+)(?:\s*(?:to|-)\s*(\d+))? min', t[1] or '')
        if m and int(m.group(2) or m.group(1)) > 30 and 'exception:' not in (t[1] or ''):
            print(f'open task {t[0]} estimates over 30 min with no exception'); bad += 1
    for t in c.execute("SELECT id FROM tasks WHERE status='open' AND kind='agent' AND sec_ord IS NULL"):
        print(f'open task {t[0]} has no section'); bad += 1
    print('ok' if not bad else f'{bad} problem(s)'); sys.exit(1 if bad else 0)


# A stale reference must be declared as one: `task 12`, `task_12`, `tasks 12`. A bare number is not a
# reference, because time budgets, line counts and scores are bare numbers too.
LINT_REF = re.compile(r'\btasks?[ _](\d+)\b')


# A repo-relative path token in a What: a word with a `/` in it, so `dag/tm.py` and `reviews/` count. The
# lookbehind drops the tail of a URL or of an absolute path (`://x/y`, `/usr/bin`), and `is_path` drops prose
# a slash happens to join (`yes/no`, `projects/wt`), which would otherwise read as a missing input.
PATH_TOKEN = re.compile(r'(?<![\w/.:-])[A-Za-z0-9_.+@*-]+(?:/[A-Za-z0-9_.+@*-]+)+')

# A path after one of these verbs on the same What line is what the task writes, not an input it reads, so the
# path check must not ask for it. `to` covers the destination form (`raw output to `x.txt`); the check reads only
# the text before the token, so `Read `x.py` and write nothing` keeps `x.py` an input.
WRITE_WORDS = re.compile(r"\b(?:write|writes|add|create|to|into|save|saves)\b", re.I)
# A `Done when` heading inside What: every path under it is an artifact the task leaves behind.
DONE_WHEN_HEAD = re.compile(r'^\s*done when\b', re.I)


def is_path(p):
    """Whether one matched token is shaped like a path: a dotted segment (`tm.py`) or a trailing `/` (a directory)."""
    return p.endswith('/') or any('.' in seg for seg in p.split('/'))


def path_forms(p):
    """The absolute path one relative token means: the same string under the project root.

    Comparing forms rather than strings lets two spellings of one file (`a/b.md`, `a/./b.md`) be recognised as
    the same path in `lint_failures`.
    """
    return {os.path.realpath(f'{ROOT}/{p}')}


def path_exists(p):
    """Whether one input path token resolves, with a glob expanded against the project root.

    A plain token is its own single match, so one function answers both; a glob that matches nothing is still a
    missing input, which is what `dag/*.py` must not become.
    """
    return any(glob.glob(f) for f in path_forms(p))


def written_paths(t):
    """The paths a task names as its own output: every token in `Done when`, after a `Done when` heading in What,
    and any token a write verb precedes on its What line.

    `lint_failures` skips these, so a task that names the file it writes passes while one that only reads a
    missing file still fails.
    """
    out = set(path_tokens(t['done_when'] or '', outputs=True))
    after_head = False
    for line in (t['what'] or '').split('\n'):
        if DONE_WHEN_HEAD.match(line): after_head = True
        for m in PATH_TOKEN.finditer(line):
            p = m.group(0).rstrip('.,:;')
            if is_path(p) and (after_head or WRITE_WORDS.search(line[:m.start()])): out.add(p)
    return out


def path_tokens(text, outputs=False):
    """The repo-relative path tokens in a task text, in order, without duplicates.

    `outputs=False` drops a line that marks its tokens as the task's own output (`Output:`) and every token
    under the runs dir, so a task is never failed for a working file it writes itself.
    """
    out = []
    for line in (text or '').split('\n'):
        if not outputs and 'Output:' in line: continue
        for m in PATH_TOKEN.finditer(line):
            p = m.group(0).rstrip('.,:;')
            if f'{RUNS}/' in p or p.startswith('/') or not is_path(p): continue
            if p not in out: out.append(p)
    return out


def dep_path_tokens(c, n):
    """Every path token named in the What, Details, Evidence or body of one of `n`'s transitive dependencies.

    A dependency names its own output there, so its `Output:` lines count, unlike for the task being linted.
    """
    out = set()
    for d in ancestors(c, n):
        r = c.execute('SELECT what, details, evidence, body FROM tasks WHERE id=?', (d,)).fetchone()
        for txt in (r or ()): out.update(path_tokens(txt, outputs=True))
    return out


def lint_failures(c, t):
    """The lint failure lines for one task row, each prefixed with its id; empty when every check passes.

    `cmd_lint` prints them and `cmd_claim` skips a row that has any, so a check is written once and cannot
    disagree between the two callers."""
    n = t['id']; bad = []
    det = t['details'] or ''
    # Same estimate form as `check`: the upper bound of `estimate 10 to 15 min` is what the 30 min rule judges.
    m = re.search(r'estimate (\d+)(?:\s*(?:to|-)\s*(\d+))? min', det)
    if m and int(m.group(2) or m.group(1)) > 30 and 'exception:' not in det:
        bad.append(f'{n}: estimate {m.group(2) or m.group(1)} min is over 30 with no exception: in Details')
    for ref in sorted({int(r) for r in LINT_REF.findall((t['what'] or '') + '\n' + det)}):
        r = c.execute('SELECT status FROM tasks WHERE id=?', (ref,)).fetchone()
        if r and r['status'] in ('superseded', 'obsolete'):
            bad.append(f'{n}: names task {ref}, which is {r["status"]}')
    if t['kind'] == 'user':
        # One line for the task, not one per missing field, so each check reports at most one failure.
        cols = t.keys()
        miss = [k for k, v in (('question', (t['question'] if 'question' in cols else None) or ''),
                               ('options', (t['options'] if 'options' in cols else None) or '')) if not v.strip()]
        if miss: bad.append(f'{n}: user task has no {" or ".join(miss)}')
    # Path check: every input path a What names must exist now or come from a dependency, so a task whose input
    # was never written fails before a worker claims it. Only a missing path costs the ancestor walk.
    writes = set()
    for p in written_paths(t): writes |= path_forms(p)
    missing = [p for p in path_tokens(t['what'] or '')
               if not path_forms(p) & writes and not path_exists(p)]
    if missing:
        named = dep_path_tokens(c, n)
        bad.extend(f'{n}: names {p}, which is missing under the root and no dependency produces'
                   for p in missing if p not in named)
    return bad


def cmd_lint(a):
    """Static checks on one task: an estimate over 30 min with no `exception:`, a reference to a superseded or
    obsolete task, a user task missing its question or options, and an input path in What that neither exists under
    the root nor comes from a dependency. Prints one line per failure; exits 1 if any."""
    c = connect(ro=True); bad = lint_failures(c, get(c, a.id))
    for line in bad: print(line)
    print('ok' if not bad else f'{len(bad)} problem(s)')
    sys.exit(1 if bad else 0)


def cmd_graph(a):
    c = connect(ro=True); q = "SELECT id, title, status, milestone FROM tasks"
    if a.ms: q += f" WHERE milestone='{a.ms}'"
    ids = set(); print('digraph tm {'); print('  rankdir=LR;')
    for t in c.execute(q):
        ids.add(t[0]); col = 'gray' if t[2] in CLOSED else ('red' if t[2] == 'blocked' else 'black')
        print(f'  t{t[0]} [label="{t[0]}", color={col}, tooltip="{t[1][:60]}"];')
    for r in c.execute('SELECT task, dep FROM deps'):
        if r[0] in ids: print(f'  t{r[1]} -> t{r[0]};')
    print('}')


def run_metrics(c, ids):
    """Three metrics-card numbers over one set of task ids: each a number or None when its inputs are missing.

    A single query per number so the export stays one read-only snapshot: throughput from the close runs' span,
    first-review pass from review1 verdicts, tokens from the run columns (None until a step records them)."""
    ids = list(ids)
    m = {'closed_per_hour': None, 'first_review_pass': None, 'tokens_per_task': None}
    if not ids: return m
    ph = ','.join('?' * len(ids))
    # Throughput: closed tasks that recorded a close run, over the hours from the first close to the last.
    n, lo, hi = c.execute(f"""SELECT count(DISTINCT task), min(started), max(coalesce(ended, started)) FROM runs
                            WHERE step='close' AND task IN ({ph})""", ids).fetchone()
    if n and lo is not None and hi is not None and hi > lo:
        m['closed_per_hour'] = round(n / ((hi - lo) / 3600), 2)
    p, f = c.execute(f"""SELECT sum(verdict='PASS'), sum(verdict='FAIL') FROM runs
                        WHERE step='review1' AND task IN ({ph})""", ids).fetchone()
    if (p or 0) + (f or 0): m['first_review_pass'] = round(p / (p + f), 3)
    rcols = {r[1] for r in c.execute('PRAGMA table_info(runs)')}
    closed = [r[0] for r in c.execute(f"SELECT id FROM tasks WHERE status IN {CLOSED} AND id IN ({ph})", ids)]
    if closed and {'tokens_in', 'tokens_out'} <= rcols:
        tot, ntok = c.execute(f"""SELECT sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)), count(tokens_in) + count(tokens_out)
                                  FROM runs WHERE task IN ({','.join('?' * len(closed))})""", closed).fetchone()
        # Null rather than 0 until some run of a closed task actually recorded a token count.
        if ntok: m['tokens_per_task'] = round(tot / len(closed))
    return m


def cmd_export(a):
    """JSON snapshot of milestones, tasks and dependency links for viewers; read-only."""
    c = connect(ro=True)
    ms = [dict(r) for r in c.execute('SELECT id, title, ord FROM milestones ORDER BY ord')]
    keep = a.ms.split(',') if a.ms else [m['id'] for m in ms]
    tcols = {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
    qcols = ''.join(f'{k},' if k in tcols else f'NULL AS {k},' for k in ('question', 'options', 'summary', 'facts'))
    ts = [dict(r) for r in c.execute(f"""SELECT id, milestone, ord, kind, title, status, suffix, worker, updated, {qcols}
             coalesce(details, body, '') AS det FROM tasks WHERE milestone IN ({','.join('?' * len(keep))})""", keep)]
    for t in ts:
        # Estimate from the task text: "estimate 10 min" or "estimate 10 to 15 min" (upper bound).
        m = re.search(r'estimate (\d+)(?:\s*(?:to|-)\s*(\d+))? min', t.pop('det'))
        t['est'] = int(m.group(2) or m.group(1)) if m else None
        t['options'] = json.loads(t['options']) if t.get('options') else None
    ids = {t['id'] for t in ts}
    es = [[r[0], r[1]] for r in c.execute('SELECT task, dep FROM deps') if r[0] in ids and r[1] in ids]
    now = time.time(); runs = {}
    # pi time per task; a step still running counts up to now. first = when the task's first step began.
    for r in c.execute('SELECT task, sum(coalesce(ended, ?) - started), min(started) FROM runs GROUP BY task', (now,)):
        runs[r[0]] = (round(r[1] or 0), r[2])
    for t in ts:
        t['secs'], t['first'] = runs.get(t['id'], (None, None))
    meta = dict(c.execute("SELECT key, value FROM meta WHERE key LIKE 'max_%'"))
    # Claim's defaults when a cap was never set; the page shows the limits claim enforces.
    lim = {k: int(meta.get(k, d)) for k, d in (('max_running', MAX_RUNNING), ('max_opus', MAX_OPUS))}
    # The metrics card's three numbers per milestone, and the same three for every milestone in the export.
    by_ms = {m['id']: [] for m in ms if m['id'] in keep}
    for t in ts: by_ms[t['milestone']].append(t['id'])
    for m in ms:
        if m['id'] in by_ms: m['metrics'] = run_metrics(c, by_ms[m['id']])
    tot = run_metrics(c, [t['id'] for t in ts])
    # The decision log and the background it follows, so the page carries what the checklist's Context did.
    intro = c.execute("SELECT value FROM meta WHERE key='context_intro'").fetchone()
    log = [{'text': r[0], 'task': r[1]} for r in c.execute('SELECT text, task FROM context ORDER BY id')]
    json.dump({'at': time.time(), 'name': host.NAME, 'intro': intro[0] if intro else '', 'context': log, 'cap': lim['max_running'], 'limits': lim, 'totals': tot, 'milestones': [m for m in ms if m['id'] in keep], 'tasks': ts, 'links': es},
              sys.stdout, separators=(',', ':'))


def task_detail(n):
    """One task's full record for the page: section fields, Result, dependencies, record files in the runs dir,
    pi steps and the decisions logged against it. None when there is no such task.

    Fetched per selected task rather than sent with every poll, so the 5 s snapshot stays small on a large DAG.
    """
    c = connect(ro=True)
    t = c.execute('SELECT * FROM tasks WHERE id=?', (n,)).fetchone()
    if not t: return None
    d = {k: t[k] for k in ('id', 'title', 'sec_title', 'why', 'what', 'details', 'done_when', 'evidence', 'result', 'body')}
    d['deps'] = deps_of(c, n)
    try: names = os.listdir(host.RUNS)
    except OSError: names = []
    # Records only: the commit list is scratch, and step logs are named `<n>_<step>.*`, so the prefix excludes them.
    d['files'] = sorted(f for f in names if re.fullmatch(rf'task_{n}_[\w.-]+', f) and not f.endswith('_commit.txt'))
    d['runs_dir'] = RUNS
    d['steps'] = [{'step': r[0], 'rc': r[1], 'verdict': r[2], 'secs': round(r[3]) if r[3] is not None else None,
                   'tokens': (r[4] or 0) + (r[5] or 0) if r[4] is not None or r[5] is not None else None}
                  for r in c.execute('SELECT step, rc, verdict, ended - started, tokens_in, tokens_out FROM runs WHERE task=? ORDER BY id', (n,))]
    d['context'] = [r[0] for r in c.execute('SELECT text FROM context WHERE task=? ORDER BY id', (n,))]
    return d


def cmd_serve(a):
    """Serve the DAG page with live data on localhost; every request is a fresh read-only query.

    `/data.json` is the snapshot the page polls, `/task.json?id=<n>` one task's full record, and `/runs/<file>`
    a record file from the runs dir as plain text.
    """
    import html, http.server, io, contextlib, urllib.parse

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path); path = url.path
            if path == '/data.json':
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cmd_export(argparse.Namespace(ms=a.ms))
                body, ctype = buf.getvalue().encode(), 'application/json'
            elif path == '/task.json':
                tid = urllib.parse.parse_qs(url.query).get('id', [''])[0]
                d = task_detail(int(tid)) if tid.isdigit() else None
                if d is None:
                    self.send_error(404); return
                body, ctype = json.dumps(d, separators=(',', ':')).encode(), 'application/json'
            elif path.startswith('/runs/'):
                # A bare file name only, so a request can never leave the runs dir.
                name = urllib.parse.unquote(path[len('/runs/'):])
                full = os.path.join(host.RUNS, name)
                if not re.fullmatch(r'[\w.-]+', name) or name.startswith('.') or not os.path.isfile(full):
                    self.send_error(404); return
                with open(full, 'rb') as fh: body = fh.read()
                ctype = 'text/plain; charset=utf-8'
            elif path in ('/', '/index.html'):
                body, ctype = open(f'{HERE}/dag_view.html').read().replace('__DATA__', 'null').replace('__TITLE__', html.escape(host.NAME)).encode(), 'text/html; charset=utf-8'
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', a.port), H)
    print(f'serving http://127.0.0.1:{a.port}/ (read-only, Ctrl-C to stop)')
    srv.serve_forever()


# ---------- edits ----------

def read_arg(v):
    """`@path` reads the value from a file; `-` from stdin."""
    if v is None: return None
    if v == '-': return sys.stdin.read().rstrip('\n')
    if v.startswith('@'): return open(v[1:]).read().rstrip('\n')
    return v


def parse_ids(s):
    out = []
    for part in (s or '').replace(' ', '').split(','):
        if not part: continue
        if '-' in part:
            a, b = part.split('-'); out += list(range(int(a), int(b) + 1))
        else: out.append(int(part))
    return out


def set_deps(c, n, ids):
    for d in ids:
        get(c, d)
    c.execute('DELETE FROM deps WHERE task=?', (n,))
    for d in ids:
        c.execute('INSERT INTO deps VALUES (?,?)', (n, d))


def cmd_add(a):
    c = connect(); c.execute('BEGIN IMMEDIATE')
    if not c.execute('SELECT 1 FROM milestones WHERE id=?', (a.ms,)).fetchone(): sys.exit(f'no milestone {a.ms}')
    n = a.id or c.execute('SELECT coalesce(max(id),0)+1 FROM tasks').fetchone()[0]
    o = c.execute('SELECT coalesce(max(ord),0)+1 FROM tasks WHERE milestone=?', (a.ms,)).fetchone()[0]
    so = c.execute('SELECT coalesce(max(sec_ord),0)+1 FROM tasks').fetchone()[0]
    c.execute("""INSERT INTO tasks (id, milestone, ord, sec_ord, kind, title, status, why, what, details,
                 done_when, evidence, updated) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?)""",
              (n, a.ms, o, so, 'user' if a.user else 'agent', a.title, read_arg(a.why),
               read_arg(a.what), read_arg(a.details), read_arg(a.done_when), read_arg(a.evidence), time.time()))
    set_deps(c, n, parse_ids(a.deps))
    if a.assessed: c.execute('UPDATE tasks SET assessed=1 WHERE id=?', (n,))
    if a.question or a.option:
        c.execute('UPDATE tasks SET question=?, options=? WHERE id=?', (a.question, json.dumps(a.option) if a.option else None, n))
    c.execute('COMMIT'); checkpoint(c); print(n)


def ancestors(c, n):
    """Every task `n` waits on, directly or through others."""
    seen, todo = set(), [n]
    while todo:
        for (d,) in c.execute('SELECT dep FROM deps WHERE task=?', (todo.pop(),)):
            if d not in seen: seen.add(d); todo.append(d)
    return seen


# A dependent's prose names the task it waited on (`task 12`, `task_12`, `tasks 12`, or the bare id), so after a
# split it must name the merge: lint fails the `task <n>` forms, and the bare form would leave the reader looking at a
# superseded task. The digit lookarounds keep a longer number (`120`, `312`) intact.
SPLIT_REF = r'(?:(?P<label>\btasks?)(?P<sep>[ _]))?(?<!\d){}(?!\d)'


def repoint_text(text, old, new, bare=True):
    """One text field with every mention of task `old` replaced by `new`, keeping the `task ` or `task_` form used.

    `bare=False` leaves a bare `old` alone: a part's own text is full of line counts and estimates, and only the
    labelled forms are references (the ones lint fails)."""
    if not text:
        return text
    return re.sub(SPLIT_REF.format(old),
                  lambda m: f'{m.group("label")}{m.group("sep")}{new}' if m.group('label')
                  else str(new) if bare else m.group(0), text)


def cmd_split(a):
    """Replace task `id` by chunk tasks and a merge task: chunks inherit its dependencies, the merge waits on every
    chunk, every open task that waited on `id` waits on the merge instead and names it in its own text, the chunks and the
    merge name the merge where they named `id`, and `id` becomes superseded."""
    c = connect(); c.execute('BEGIN IMMEDIATE'); get(c, a.id)
    chunks = parse_ids(a.into)
    for i in chunks + [a.merge]: get(c, i)
    deps = [d for (d,) in c.execute('SELECT dep FROM deps WHERE task=?', (a.id,))]
    for i in chunks:
        for d in deps: c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (i, d))
        c.execute('UPDATE tasks SET assessed=1 WHERE id=?', (i,))
        c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (a.merge, i))
    c.execute('UPDATE tasks SET assessed=1 WHERE id=?', (a.merge,))
    for (t,) in c.execute("""SELECT d.task FROM deps d JOIN tasks u ON u.id=d.task
                            WHERE d.dep=? AND u.status IN ('open','blocked','running')""", (a.id,)).fetchall():
        if t in chunks or t == a.merge: continue
        c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (t, a.merge)); c.execute('DELETE FROM deps WHERE task=? AND dep=?', (t, a.id))
        # The dependency now points at the merge, so the text that described it must too; lint skips a dependent
        # whose What or Details still names the superseded id.
        row = c.execute('SELECT what, details, done_when FROM tasks WHERE id=?', (t,)).fetchone()
        new = [repoint_text(v, a.id, a.merge) for v in row]
        if new != list(row):
            c.execute('UPDATE tasks SET what=?, details=?, done_when=?, updated=? WHERE id=?', (*new, time.time(), t))
    # Parts and merge name the merge too. The assess step writes them before the split, so their paths carry the
    # split id (`task_8_part2_x.md`); lint then fails each part for naming a superseded task and no worker claims
    # it. One rewrite across all of them keeps a part file's name equal where it is written and where it is read.
    # The exec file is the exception: each part names its own. Pointed at the merge's, every part would log into
    # one file that the scheduler renames to `_prev1` when the merge starts, leaving the parts' records dangling.
    for i in chunks + [a.merge]:
        row = c.execute('SELECT what, details, done_when FROM tasks WHERE id=?', (i,)).fetchone()
        new = [repoint_text(re.sub(rf'\btask_{a.id}_exec(?![0-9])', f'task_{i}_exec', v or '') or v, a.id, a.merge, bare=False)
               for v in row]
        if new != list(row):
            c.execute('UPDATE tasks SET what=?, details=?, done_when=?, updated=? WHERE id=?', (*new, time.time(), i))
    # A task that a chunk now waits on (inherited from the split task) must not also wait on that chunk.
    for i in chunks + [a.merge]:
        up = ancestors(c, i)
        for (cid,) in c.execute('SELECT task FROM deps WHERE dep=?', (i,)).fetchall():
            if cid in up: c.execute('DELETE FROM deps WHERE task=? AND dep=?', (cid, i))
    c.execute("UPDATE tasks SET status='superseded', worker=NULL, updated=?, result=? WHERE id=?",
              (time.time(), f'- Split into {", ".join(map(str, chunks))}; merged by {a.merge}.', a.id))
    c.execute('COMMIT'); checkpoint(c); print(f'{a.id} split into {chunks}, merge {a.merge}')


def cmd_edit(a):
    c = connect(); c.execute('BEGIN IMMEDIATE'); t = get(c, a.id); sets = {}
    for f in ('title', 'why', 'what', 'details', 'done_when', 'evidence', 'result', 'suffix'):
        v = read_arg(getattr(a, f))
        if v is not None: sets[f] = v
    if a.append_result:
        v = read_arg(a.append_result); sets['result'] = ((t['result'] + '\n') if t['result'] else '') + v
    if a.ms: sets['milestone'] = a.ms
    if a.status:
        if a.status not in STATUSES: sys.exit(f'status must be one of {STATUSES}')
        # A worker owns a running task; changing its status under it could let a second worker claim it.
        if t['status'] == 'running' and a.status != 'running' and not a.force:
            sys.exit(f'task {a.id} is running (worker {t["worker"]}); pass --force to change its status')
        sets['status'] = a.status
    if a.assessed is not None: sets['assessed'] = int(a.assessed == 'yes')
    if a.kind: sets['kind'] = a.kind
    if sets:
        sets['updated'] = time.time()
        c.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), a.id])
    if a.deps is not None: set_deps(c, a.id, parse_ids(a.deps))
    for d in parse_ids(a.add_deps): get(c, d); c.execute('INSERT OR IGNORE INTO deps VALUES (?,?)', (a.id, d))
    # A hold exists to buy time for this edit: once the new dependency gates the task, the hold has no job left,
    # and clearing it here means a forgotten `hold --clear` cannot strand the task.
    if parse_ids(a.add_deps) and a.id in held(c): set_held(c, [i for i in held(c) if i != a.id])
    for d in parse_ids(a.rm_deps): c.execute('DELETE FROM deps WHERE task=? AND dep=?', (a.id, d))
    c.execute('COMMIT'); checkpoint(c)


def cmd_close(a):
    c = connect(); c.execute('BEGIN IMMEDIATE'); get(c, a.id)
    st = a.status or 'done'
    if st not in CLOSED: sys.exit(f'close status must be one of {CLOSED}')
    # Hold in the same transaction as the close: a dependent freed by this close is claimed within seconds, so a
    # hold placed by a second command would lose the race it exists to win.
    if a.hold:
        ids = parse_ids(a.hold)
        for i in ids: get(c, i)
        set_held(c, held(c) + ids)
    c.execute('UPDATE tasks SET status=?, suffix=?, worker=NULL, updated=? WHERE id=?',
              (st, a.reason or '', time.time(), a.id))
    if a.result:
        t = get(c, a.id); v = read_arg(a.result)
        c.execute('UPDATE tasks SET result=? WHERE id=?', (((t['result'] + '\n') if t['result'] else '') + v, a.id))
    c.execute('COMMIT'); checkpoint(c)


def cmd_ms(a):
    c = connect(); c.execute('BEGIN IMMEDIATE')
    if a.op == 'add':
        o = c.execute('SELECT coalesce(max(ord),0)+1 FROM milestones').fetchone()[0]
        if a.after:
            o = c.execute('SELECT ord FROM milestones WHERE id=?', (a.after,)).fetchone()[0] + 1
            for r in c.execute('SELECT id, ord FROM milestones WHERE ord>=? ORDER BY ord DESC', (o,)).fetchall():
                c.execute('UPDATE milestones SET ord=? WHERE id=?', (r[1] + 1, r[0]))
        c.execute('INSERT INTO milestones VALUES (?,?,?,?)', (a.id, a.title, read_arg(a.note) or '', o))
    elif a.op == 'edit':
        if a.title: c.execute('UPDATE milestones SET title=? WHERE id=?', (a.title, a.id))
        if a.note is not None: c.execute('UPDATE milestones SET note=? WHERE id=?', (read_arg(a.note), a.id))
    elif a.op == 'list':
        for r in c.execute('SELECT id, title FROM milestones ORDER BY ord'): print(f'{r[0]}\t{r[1]}')
    c.execute('COMMIT')
    if a.op != 'list': checkpoint(c)


def cmd_context(a):
    c = connect(); text = read_arg(a.text)
    if not text.startswith('- '): text = '- ' + text
    c.execute('INSERT INTO context (text, task, added) VALUES (?,?,?)', (text, a.task, time.time()))
    checkpoint(c)


def cmd_claim(a):
    """Atomically move the first ready task that passes `lint` to running for one worker; print its id."""
    c = connect(); c.execute('BEGIN IMMEDIATE')
    # Concurrency cap, adjustable live with `tm.py cap <n>`: no claim while that many tasks run.
    cap = c.execute("SELECT value FROM meta WHERE key='max_running'").fetchone()
    if c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] >= int(cap[0] if cap else MAX_RUNNING):
        c.execute('COMMIT'); sys.exit(3)
    allow = a.ms.split(',') if a.ms else None
    # `[opus]` and `[orch]` tasks are the orchestrator's (an Opus sub-agent, or tools a headless pi lacks), never a
    # pi worker's. Priority tasks (`tm.py prio <ids>`) go first, and any worker may take one whatever milestones it
    # serves, so a task that unblocks others does not wait behind a milestone's queue.
    pr = c.execute("SELECT value FROM meta WHERE key='priority'").fetchone()
    prio = [int(x) for x in pr[0].split(',') if x] if pr and pr[0] else []
    rows = [r for r in ready_rows(c) if (not allow or r['milestone'] in allow or r['id'] in prio)
            and not r['title'].startswith(('[opus]', '[orch]'))]
    rows.sort(key=lambda r: prio.index(r['id']) if r['id'] in prio else len(prio))
    if not rows:
        c.execute('COMMIT'); sys.exit(3)
    # A row that fails `lint` is never moved to `running`: record why in `meta` as `claim_skip:<id>` and try the
    # next row, so a broken task cannot occupy a worker and the scheduler can read the reason it was passed over.
    t = None
    for r in rows:
        # The path check tells a task's outputs from its inputs, so claim runs it: a What naming the file it
        # writes passes.
        bad = lint_failures(c, r)
        if not bad: t = r; break
        c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                  (f"claim_skip:{r['id']}", time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + '\n'.join(bad)))
    if t is None:
        c.execute('COMMIT'); sys.exit(3)
    c.execute("UPDATE tasks SET status='running', worker=?, updated=? WHERE id=?", (a.worker, time.time(), t['id']))
    c.execute('COMMIT'); print(t['id'])


def cmd_release(a):
    """Return a running task to open or blocked, for a failed or abandoned run."""
    c = connect()
    c.execute("UPDATE tasks SET status=?, worker=NULL, updated=? WHERE id=? AND status='running'",
              (a.status, time.time(), a.id))
    checkpoint(c)


def cmd_hold(a):
    """Show the held task ids, hold more, or clear some (`--clear all` clears every hold)."""
    c = connect(); c.execute('BEGIN IMMEDIATE'); cur = held(c)
    if a.clear:
        cur = [] if a.clear == 'all' else [i for i in cur if i not in parse_ids(a.clear)]
    for i in parse_ids(','.join(a.ids)): get(c, i); cur.append(i)
    if a.clear or a.ids: set_held(c, cur)
    c.execute('COMMIT'); print(','.join(str(i) for i in sorted(set(cur))) or 'none')


def cmd_prio(a):
    """Set or show the priority task ids that claim hands out first; `prio 0` clears the list."""
    c = connect()
    if not a.ids:
        r = c.execute("SELECT value FROM meta WHERE key='priority'").fetchone(); print(r[0] if r else 'none'); return
    v = '' if a.ids == ['0'] else ','.join(a.ids)
    c.execute("INSERT OR REPLACE INTO meta VALUES ('priority', ?)", (v,)); print(v or 'none')


def cmd_cap(a):
    """Set or show a live cap: running tasks, or with `--opus` the orchestrator's Opus tasks; 0 restores the default."""
    c = connect()
    key, default = ('max_opus', MAX_OPUS) if a.opus else ('max_running', MAX_RUNNING)
    if a.n is None:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone(); print(r[0] if r else default); return
    if a.n == 0: c.execute("DELETE FROM meta WHERE key=?", (key,))
    else: c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(a.n)))
    print(a.n or default)


def cmd_decide(a):
    """Set a user task's question and options (each option `label: description`)."""
    c = connect(); get(c, a.id)
    sets = {}
    if a.question: sets['question'] = a.question
    if a.option: sets['options'] = json.dumps(a.option)
    if a.summary: sets['summary'] = a.summary
    # Details come from a file, so multi-line evidence needs no shell quoting.
    if a.facts: sets['facts'] = sys.stdin.read() if a.facts == '-' else open(a.facts).read()
    if sets:
        sets['updated'] = time.time()
        c.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), a.id])
    checkpoint(c)


def lock_try(c, name, holder, ttl, cmd):
    """Take the lock if free or stale (holder process gone on this host, or past its expiry)."""
    import socket
    host, now = socket.gethostname(), time.time()
    c.execute('BEGIN IMMEDIATE')
    r = c.execute('SELECT host, pid, expires FROM locks WHERE name=?', (name,)).fetchone()
    stale = False
    if r:
        stale = r['expires'] < now
        if not stale and r['host'] == host:
            try: os.kill(r['pid'], 0)
            except ProcessLookupError: stale = True
            except PermissionError: pass
    if r and not stale:
        c.execute('COMMIT'); return False
    c.execute('INSERT OR REPLACE INTO locks VALUES (?, ?, ?, ?, ?, ?, ?)', (name, holder, host, os.getpid(), now, now + ttl, cmd))
    c.execute('COMMIT'); return True


def cmd_lock(a):
    """Named lock in tasks.db: `run NAME -- cmd...` holds it while cmd runs; list shows holders."""
    import signal, socket
    c = connect()
    if a.op == 'list':
        for r in c.execute('SELECT * FROM locks ORDER BY name'):
            print(f"{r['name']}\t{r['holder']}\t{r['host']}:{r['pid']}\theld {int(time.time() - r['at'])}s\t{r['cmd'] or ''}")
        return
    if a.op == 'release':
        c.execute('DELETE FROM locks WHERE name=?', (a.name,)); return
    if a.name and a.name.startswith('-'):
        a.cmd = [a.name] + a.cmd; a.name = None
    # Options may come before or after the lock name; everything after `--` is the command.
    # argparse drops the `--` when the name comes first, so `run NAME -- cmd` arrives as name + cmd;
    # a `--` before any name means no name was given.
    raw = sys.argv[sys.argv.index('run') + 1:] if 'run' in sys.argv else []
    if raw[:1] == ['--']: sys.exit('lock run: usage: tm.py lock run NAME [--holder H] [--wait S] [--ttl S] -- cmd...')
    toks = ([a.name] if a.name else []) + a.cmd
    if '--' in toks:
        head, cmd = toks[:toks.index('--')], toks[toks.index('--') + 1:]
    elif a.name and a.cmd:
        head, cmd = [a.name], a.cmd
    else:
        sys.exit('lock run: usage: tm.py lock run NAME [--holder H] [--wait S] [--ttl S] -- cmd...')
    pa = argparse.ArgumentParser(prog='tm.py lock run'); pa.add_argument('name'); pa.add_argument('--holder')
    pa.add_argument('--wait', type=int); pa.add_argument('--ttl', type=int)
    o = pa.parse_args(head)
    a.name = o.name; a.holder = o.holder or a.holder
    a.wait = o.wait if o.wait is not None else a.wait; a.ttl = o.ttl if o.ttl is not None else a.ttl
    if not cmd: sys.exit('lock run: give the command after --')
    deadline = time.time() + a.wait
    while not lock_try(c, a.name, a.holder or os.environ.get('DAG_HOLDER', 'unknown'), a.ttl, ' '.join(cmd)[:500]):
        if time.time() > deadline: sys.exit(f'lock {a.name}: still held after {a.wait}s')
        time.sleep(1)
    # Release on every exit path, including a SIGTERM from a timeout, so a killed run never strands the lock.
    # The command gets the SIGTERM too, so it never runs on after its lock is gone.
    try:
        proc = subprocess.Popen(cmd)
    except OSError as e:
        c.execute('DELETE FROM locks WHERE name=? AND host=? AND pid=?', (a.name, socket.gethostname(), os.getpid()))
        sys.exit(f'lock run: {e}')
    def term(*_):
        proc.terminate()
        try: proc.wait(10)
        except subprocess.TimeoutExpired: proc.kill()
        sys.exit(143)
    signal.signal(signal.SIGTERM, term)
    try:
        rc = proc.wait()
    finally:
        c.execute('DELETE FROM locks WHERE name=? AND host=? AND pid=?', (a.name, socket.gethostname(), os.getpid()))
    sys.exit(rc)


def cmd_block(a):
    """Record a block or an unblock attempt; `tm.py block list` prints the audit trail."""
    c = connect()
    if a.op == 'list':
        for r in c.execute('SELECT id, task, datetime(at, "unixepoch", "localtime"), kind, attempt, outcome, substr(reason, 1, 80) FROM blocks ORDER BY id'):
            print('\t'.join(str(x) for x in r))
        return
    if a.op == 'add':
        cur = c.execute('INSERT INTO blocks (task, at, kind, reason, attempt, outcome) VALUES (?,?,?,?,?,?)',
                        (a.task, time.time(), a.kind, a.reason or '', a.attempt or 0, a.outcome or 'pending'))
        print(cur.lastrowid)
    elif a.op == 'end':
        c.execute('UPDATE blocks SET outcome=? WHERE id=?', (a.outcome, a.id))


def cmd_run(a):
    c = connect()
    if a.op == 'start':
        cur = c.execute('INSERT INTO runs (task, step, worker, started) VALUES (?,?,?,?)',
                        (a.task, a.step, a.worker, time.time()))
        print(cur.lastrowid)
    else:
        # coalesce keeps an earlier count: a step that ends without tokens must not zero one already stored.
        c.execute('UPDATE runs SET ended=?, rc=?, verdict=?, tokens_in=coalesce(?, tokens_in), '
                  'tokens_out=coalesce(?, tokens_out) WHERE id=?',
                  (time.time(), a.rc, a.verdict, a.tokens_in, a.tokens_out, a.run))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    x = s.add_parser('init', help='create an empty tasks.db and runs/ for a fresh project'); x.set_defaults(f=cmd_init)
    x = s.add_parser('import', help='build tasks.db from a markdown checklist'); x.add_argument('md'); x.add_argument('--force', action='store_true'); x.set_defaults(f=cmd_import)
    x = s.add_parser('render', help='export the DAG as a markdown checklist to stdout, or to --out FILE; optional, nothing maintains it')
    x.add_argument('--out'); x.set_defaults(f=cmd_render)
    x = s.add_parser('show', help='print one task'); x.add_argument('id', type=int); x.add_argument('--json', action='store_true'); x.set_defaults(f=cmd_show)
    x = s.add_parser('ready', help='open tasks whose deps are all closed'); x.add_argument('--user', action='store_true'); x.set_defaults(f=cmd_ready)
    x = s.add_parser('list'); x.add_argument('--ms'); x.add_argument('--status'); x.set_defaults(f=cmd_list)
    x = s.add_parser('stats'); x.set_defaults(f=cmd_stats)
    x = s.add_parser('cost', help='tokens and cost per closed task in one milestone; --price-* are USD per million')
    x.add_argument('--ms', required=True)
    x.add_argument('--price-in', type=float, default=0.0); x.add_argument('--price-out', type=float, default=0.0)
    x.set_defaults(f=cmd_cost)
    x = s.add_parser('check', help='dangling deps, cycles, missing sections, estimates over 30 min'); x.set_defaults(f=cmd_check)
    x = s.add_parser('lint', help='static checks on one task: estimate, stale reference, user task data, input paths'); x.add_argument('id', type=int); x.set_defaults(f=cmd_lint)
    x = s.add_parser('graph', help='Graphviz dot of the DAG'); x.add_argument('--ms'); x.set_defaults(f=cmd_graph)
    x = s.add_parser('export', help='JSON snapshot for viewers (read-only)'); x.add_argument('--ms'); x.set_defaults(f=cmd_export)
    x = s.add_parser('serve', help='live DAG page on localhost (read-only)'); x.add_argument('--port', type=int, default=8766)
    x.add_argument('--ms'); x.set_defaults(f=cmd_serve)
    x = s.add_parser('add', help='add a task; values may be @file or -')
    x.add_argument('--ms', required=True); x.add_argument('--title', required=True); x.add_argument('--id', type=int)
    x.add_argument('--deps', default=''); x.add_argument('--user', action='store_true')
    x.add_argument('--question'); x.add_argument('--option', action='append')
    x.add_argument('--assessed', action='store_true', help='already split-assessed: the scheduler runs it without asking again')
    for f in ('why', 'what', 'details', 'done-when', 'evidence'): x.add_argument('--' + f)
    x.set_defaults(f=cmd_add)
    x = s.add_parser('split', help='replace a task by chunk tasks plus a merge task and rewire its dependents')
    x.add_argument('id', type=int); x.add_argument('--into', required=True, help='chunk task ids, comma separated')
    x.add_argument('--merge', type=int, required=True); x.set_defaults(f=cmd_split)
    x = s.add_parser('edit', help='edit task fields; values may be @file or -'); x.add_argument('id', type=int)
    for f in ('title', 'why', 'what', 'details', 'done-when', 'evidence', 'result', 'append-result', 'suffix', 'ms', 'status', 'kind', 'deps', 'add-deps', 'rm-deps'):
        x.add_argument('--' + f)
    x.add_argument('--force', action='store_true')
    x.add_argument('--assessed', choices=['yes', 'no']); x.set_defaults(f=cmd_edit)
    x = s.add_parser('close', help='close a task'); x.add_argument('id', type=int); x.add_argument('--status', choices=CLOSED)
    x.add_argument('--reason'); x.add_argument('--result', help='Result block text, @file or -')
    x.add_argument('--hold', help='task ids to hold in the same step, so no worker claims them before a follow-up task gates them')
    x.set_defaults(f=cmd_close)
    x = s.add_parser('ms', help='milestones: add, edit, list'); x.add_argument('op', choices=['add', 'edit', 'list']); x.add_argument('id', nargs='?')
    x.add_argument('--title'); x.add_argument('--note'); x.add_argument('--after'); x.set_defaults(f=cmd_ms)
    x = s.add_parser('context', help='append a decision-log entry'); x.add_argument('text'); x.add_argument('--task', type=int); x.set_defaults(f=cmd_context)
    x = s.add_parser('claim', help='claim the next ready task'); x.add_argument('--worker', required=True); x.add_argument('--ms'); x.set_defaults(f=cmd_claim)
    x = s.add_parser('release'); x.add_argument('id', type=int); x.add_argument('--status', default='open', choices=['open', 'blocked']); x.set_defaults(f=cmd_release)
    x = s.add_parser('hold', help='task ids no worker may claim; `edit --add-deps` or `--clear` lifts a hold'); x.add_argument('ids', nargs='*')
    x.add_argument('--clear', help='ids to release, or `all`'); x.set_defaults(f=cmd_hold)
    x = s.add_parser('prio', help='priority task ids claim hands out first (0 clears)'); x.add_argument('ids', nargs='*'); x.set_defaults(f=cmd_prio)
    x = s.add_parser('cap', help='live cap on running tasks (0 restores the default); --opus sets the Opus cap'); x.add_argument('n', type=int, nargs='?')
    x.add_argument('--opus', action='store_true', help='Opus tasks the orchestrator runs at once'); x.set_defaults(f=cmd_cap)
    x = s.add_parser('decide', help="set a user task's question and options"); x.add_argument('id', type=int)
    x.add_argument('--question'); x.add_argument('--option', action='append', help='repeatable: "label: description"')
    x.add_argument('--summary', help='one paragraph: what the evidence says'); x.add_argument('--facts', help='details file (- for stdin): "- " lines, nested by indent')
    x.set_defaults(f=cmd_decide)
    x = s.add_parser('lock', help='named lock in tasks.db: run NAME -- cmd, list, release NAME'); x.add_argument('op', choices=['run', 'list', 'release'])
    x.add_argument('name', nargs='?'); x.add_argument('cmd', nargs=argparse.REMAINDER); x.add_argument('--holder')
    x.add_argument('--wait', type=int, default=3600, help='seconds to wait for the lock'); x.add_argument('--ttl', type=int, default=1800, help='seconds before a held lock counts as stale')
    x.set_defaults(f=cmd_lock)
    x = s.add_parser('block', help='block audit trail: add, end, list'); x.add_argument('op', choices=['add', 'end', 'list'])
    x.add_argument('--task', type=int); x.add_argument('--kind'); x.add_argument('--reason'); x.add_argument('--attempt', type=int)
    x.add_argument('--outcome'); x.add_argument('--id', type=int); x.set_defaults(f=cmd_block)
    x = s.add_parser('run', help='record a run step: start or end'); x.add_argument('op', choices=['start', 'end'])
    x.add_argument('--task', type=int); x.add_argument('--step'); x.add_argument('--worker'); x.add_argument('--run', type=int)
    x.add_argument('--rc'); x.add_argument('--verdict')
    x.add_argument('--tokens-in', type=int); x.add_argument('--tokens-out', type=int); x.set_defaults(f=cmd_run)
    a = p.parse_args()
    for k in list(vars(a)):
        if '-' in k: setattr(a, k.replace('-', '_'), getattr(a, k))
    a.f(a)


if __name__ == '__main__':
    main()

"""Host facts for the DAG scripts: paths derived from this file's location, project facts from `project.json`.

The DAG folder is the parent of this `src/` directory and the project root is its parent, so the folder runs
unchanged wherever it is copied and no user's home is written here. Everything a project changes lives in
`project.json` at the top of the DAG folder; with no file, the defaults below apply. We read JSON because Python 3.9 has no `tomllib`.

`project.json` keys, all optional:
- `name`: the project name; the page title uses it. Default: `Task DAG`.
- `rules`: the project's rules file every worker reads first, relative to the root. Default: `CLAUDE.md`.
- `model`: the model every pi step uses. Default: `zoltar/coding-deepseek`.
- `caps`: `max_running` and `max_opus` defaults. Default: 6 and 2.
- `cite_roots`: directory globs (`~` allowed) a cited `path:line` may resolve under, beside the root. Default: none.

Env overrides, for tests and scratch runs:
- `DAG_DB`: the task database. Default: `db/tasks.db` in the DAG folder.
- `DAG_RUNS`: task records and runtime state (exec and review files, logs, STOP, COMMIT). Default: `runs/` in the DAG folder.
- `DAG_CITE_ROOTS`: comma-separated directories that replace the config's `cite_roots`.
- `DAG_PI`: the `pi` binary the scheduler runs. Default: `pi` on `PATH`.
"""
import glob, json, os, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DAG = os.path.dirname(HERE)
ROOT = os.path.dirname(DAG)

DEFAULTS = {'name': 'Task DAG', 'rules': 'CLAUDE.md',
            'model': 'zoltar/coding-deepseek', 'caps': {'max_running': 6, 'max_opus': 2}, 'cite_roots': []}


def load_config():
    """The defaults overlaid with `project.json`; a missing file is an unconfigured project, not an error."""
    cfg = dict(DEFAULTS)
    try:
        with open(f'{DAG}/project.json') as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    cfg.update({k: v for k, v in user.items() if k != 'caps'})
    cfg['caps'] = {**DEFAULTS['caps'], **user.get('caps', {})}
    return cfg


CFG = load_config()
NAME = CFG['name']
RULES = CFG['rules']
MODEL = CFG['model']
CAPS = CFG['caps']

DB = os.path.abspath(os.environ.get('DAG_DB') or f'{DAG}/db/tasks.db')
# One directory for what a task leaves behind and what the daemons need while they run: records beside state.
RUNS = os.environ.get('DAG_RUNS') or f'{DAG}/runs'
PI = os.environ.get('DAG_PI') or 'pi'


def cite_roots():
    """The directories a cited `path:line` may resolve under, beside the root.

    Read on each call rather than at import, because a task may create them after the daemons start.
    """
    env = os.environ.get('DAG_CITE_ROOTS')
    if env:
        return [os.path.expanduser(p) for p in env.split(',') if p]
    return sorted(d for g in CFG['cite_roots'] for d in glob.glob(os.path.expanduser(g)) if os.path.isdir(d))


def is_git():
    """Whether the root itself is a git repo: commits are skipped without one."""
    return os.path.exists(f'{ROOT}/.git')


def connect_ro(timeout=60):
    """A connection for reading the task database: read-only when possible.

    A WAL database whose `-wal` and `-shm` files are gone (a clean close or a checkpointed move removes them)
    cannot be opened read-only, because the open has to create them; we then fall back to a normal connection
    that the caller only reads from.
    """
    if not os.path.exists(DB):
        raise sqlite3.OperationalError(f'no task database at {DB}')
    try:
        c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=timeout)
        c.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return c
    except sqlite3.OperationalError:
        return sqlite3.connect(DB, timeout=timeout)

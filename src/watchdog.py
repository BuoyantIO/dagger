#!/usr/bin/env python3
"""Watch the task scheduler; exit with a report on a new anomaly, or after 5 min with a status line.

Each exit wakes the orchestrator, which intervenes and restarts this script. Anomalies already
reported are kept in runs/watchdog_seen.json so each one is raised once.
"""
import json, os, re, socket, sqlite3, subprocess, sys, time

S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, S)
import host
os.makedirs(host.RUNS, exist_ok=True)
SEEN = f'{host.RUNS}/watchdog_seen.json'
HEARTBEAT = int(os.environ.get('DAG_HEARTBEAT') or 300)  # 5 min: an idle orchestrator still hears from the loop
CAP_MIN = 30  # every pi step is capped at 30 min or less


def q(sql, *a):
    c = host.connect_ro(30)
    try: return c.execute(sql, a).fetchall()
    finally: c.close()


def est_min(det):
    m = re.search(r'estimate (\d+)(?:\s*(?:to|-)\s*(\d+))? min', det or '')
    return int(m.group(2) or m.group(1)) if m else None


def sched_alive():
    """Whether a scheduler runs: a `sched:<prefix>` row in the locks table whose process is still there.

    The row, not a process-name match, is the test, because other projects on this host run a `sched.py` too.
    """
    for name, h, pid in q("SELECT name, host, pid FROM locks WHERE name LIKE 'sched:%'"):
        if h != socket.gethostname(): return True
        try: os.kill(pid, 0); return True
        except ProcessLookupError: pass
        except PermissionError: return True
    return False


def check(seen):
    now = time.time(); out = []
    run = q("""SELECT t.id, t.title, t.worker, coalesce(t.details, t.body, ''),
                      (SELECT max(started) FROM runs r WHERE r.task=t.id AND r.step='exec'),  -- latest attempt only
                      (SELECT step || '|' || started FROM runs r WHERE r.task=t.id AND ended IS NULL ORDER BY id DESC LIMIT 1)
               FROM tasks t WHERE t.status='running'""")
    for tid, title, w, det, first, cur in run:
        e = est_min(det)
        # Overrun from review and fix rounds is routine; only an overrun still in exec, or a step near the cap,
        # says something is wrong.
        in_exec = bool(cur) and cur.split('|')[0] in ('exec', 'assess')
        if first and e and in_exec and now - first > 1.5 * e * 60 and f'over:{tid}' not in seen:  # 1.5x: at 1x, normal fix rounds alert with nothing to do
            out.append(('over', tid, f'{tid} ({w}) running {int((now - first) / 60)} min, written estimate {e} min: {title[:70]}'))
        if cur:
            step, st = cur.split('|'); age = now - float(st)
            if age > (CAP_MIN - 5) * 60 and f'cap:{tid}:{step}' not in seen:
                out.append(('cap', f'{tid}:{step}', f'{tid} ({w}) step {step} at {int(age / 60)} min, near the {CAP_MIN} min cap: {title[:70]}'))
    # The unblock daemon owns ordinary blocks; orchestrator and Opus blocks wake us here, and escalations
    # (two failed attempts) are raised below.
    for (tid, title) in q("SELECT id, title FROM tasks WHERE status='blocked' AND (title LIKE '[orch]%' OR title LIKE '[opus]%')"):
        if f'blocked:{tid}' not in seen:
            out.append(('blocked', tid, f'{tid} blocked: {title[:80]}'))
    # Any task blocked over 5 min with no unblock attempt in the last 10 min has no owner: wake the orchestrator.
    for (tid, title, upd) in q("SELECT id, title, updated FROM tasks WHERE status='blocked'"):
        recent = q('SELECT count(*) FROM blocks WHERE task=? AND at > ?', tid, now - 600)[0][0]
        if now - (upd or now) > 300 and not recent and f'unowned:{tid}' not in seen:
            out.append(('unowned', tid, f'{tid} blocked {int((now - upd) / 60)} min with no unblock attempt: {title[:70]}'))
    last = q('SELECT max(max(started), max(coalesce(ended, 0))) FROM runs')[0][0] or 0
    # Only tasks a worker holds count: an `[orch]` task set running by hand records no steps.
    held = [r for r in run if r[2]]
    if held and now - last > 15 * 60 and 'stall' not in seen:
        out.append(('stall', '', f'no step started or ended for {int((now - last) / 60)} min while {len(held)} tasks run'))
    # A step process of ours alive for over 45 min outlived the 30 min cap: an orphan that burns CPU. The root
    # in the command line tells our steps from another project's.
    ps = subprocess.run(['ps', '-A', '-o', 'pid=,etime=,command='], capture_output=True, text=True).stdout
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3: continue
        pid, et, cmd = parts
        mins = sum(int(x) * m for x, m in zip(reversed(re.split('[-:]', et)), (1/60, 1, 60, 1440)))
        if mins > 45 and host.ROOT in cmd and f'{os.path.basename(host.RUNS)}/task_' in cmd and f'orphan:{pid}' not in seen:
            out.append(('orphan', pid, f'orphan process {pid} running {int(mins)} min: {cmd[:90]}'))
    # Orchestrator and Opus tasks wait on the orchestrator: wake it as soon as one is ready.
    for (tid, title) in q("""SELECT t.id, t.title FROM tasks t WHERE t.status='open' AND (t.title LIKE '[orch]%' OR t.title LIKE '[opus]%')
                           AND NOT EXISTS (SELECT 1 FROM deps d JOIN tasks u ON u.id=d.dep WHERE d.task=t.id
                                           AND u.status NOT IN ('done','superseded','skipped','obsolete'))"""):
        if f'orch:{tid}' not in seen:
            out.append(('orch', tid, f'{tid} ready for the orchestrator: {title[:80]}'))
    # A (user) task whose dependencies are closed waits on the user: say so once, so the orchestrator can ask.
    for (tid, title) in q("""SELECT t.id, t.title FROM tasks t WHERE t.status='open' AND t.kind='user'
                           AND NOT EXISTS (SELECT 1 FROM deps d JOIN tasks u ON u.id=d.dep WHERE d.task=t.id
                                           AND u.status NOT IN ('done','superseded','skipped','obsolete'))"""):
        if f'user:{tid}' not in seen:
            out.append(('user', tid, f'{tid} waits on the user: {title[:80]}'))
    # A held task is claimed by no one: report it once its dependencies are closed, so a hold left behind after
    # `close --hold` cannot stall the DAG unseen.
    hold = [x for (v,) in q("SELECT value FROM meta WHERE key='hold'") for x in v.split(',') if x]
    for tid in hold:
        open_deps = q("""SELECT count(*) FROM deps d JOIN tasks u ON u.id=d.dep WHERE d.task=?
                         AND u.status NOT IN ('done','superseded','skipped','obsolete')""", int(tid))[0][0]
        st = q('SELECT status FROM tasks WHERE id=?', int(tid))
        if st and st[0][0] == 'open' and not open_deps and f'held:{tid}' not in seen:
            out.append(('held', tid, f'{tid} is held and otherwise ready: add its dependency or `tm.py hold --clear {tid}`'))
    for (tid,) in q("SELECT DISTINCT task FROM blocks WHERE outcome='escalated'"):
        if f'escalated:{tid}' not in seen:
            out.append(('escalated', tid, f'{tid} escalated after two unblock attempts'))
    alive = sched_alive()
    # 'dead' is raised once per outage: a running scheduler clears it, so the next exit is reported.
    if alive: seen.discard('dead')
    if not alive and 'dead' not in seen:
        out.append(('dead', '', 'scheduler process is not running'))
    return out, len(run)


def main():
    seen = set(json.load(open(SEEN))) if os.path.exists(SEEN) else set()
    t0 = time.time()
    while True:
        found, nrun = check(seen)
        if found:
            for kind, key, _ in found: seen.add(f'{kind}:{key}' if key != '' else kind)
            json.dump(sorted(seen), open(SEEN, 'w'))
            print('ANOMALY'); [print('- ' + msg) for _, _, msg in found]
            return
        if time.time() - t0 > HEARTBEAT:
            st = dict(q("SELECT status, count(*) FROM tasks GROUP BY status"))  # every milestone, not a fixed list
            done5 = q("SELECT count(*) FROM tasks WHERE status='done' AND updated > ?", time.time() - HEARTBEAT)[0][0]
            print(f'HEARTBEAT running {nrun}, closed in last {HEARTBEAT // 60} min {done5}, open {st.get("open", 0)}, blocked {st.get("blocked", 0)}, done {st.get("done", 0)}')
            return
        time.sleep(min(60, HEARTBEAT))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Set max_running from the model backend's load, so pi tasks never push the shared GPU past its limit.

Every 15 s it reads the backend's in-flight and waiting requests, KV cache share, preemptions, prefix-index churn
and the 1 min TTFT p50 from Prometheus (through Grafana), keeps 10 min of samples, and holds in-flight at 90 % of
the knee: the in-flight count where output peaks. The cap never passes a static ceiling, 90 % of the running
agents at which this GPU degrades. Waiting in 3 of the last 4 polls, a p50 over 3 s twice, preemptions or index
churn cut the cap to 75 % of running. The knee never rises on its own: we control the clients, so we never
cross the limit to find it.

Optional: a project runs it only when its tasks share a GPU backend that exposes vLLM metrics. `project.json`
`autocap` overrides `grafana_url`, `model` (a regex on the model name) and `tip_agents`.
The cap is per project database: two projects on one GPU each believe the whole ceiling is theirs, and only the
live rules hold them back. Sharing the ceiling needs coordination between them, which does not exist yet.

The Grafana token is read on every poll and never written anywhere: from `DAG_GRAFANA_TOKEN_FILE` when set,
else from the macOS keychain (service `zoltar-grafana`); see host.py.
Running tasks are never stopped: the cap only gates new claims.
"""
import json, os, sqlite3, time, urllib.parse, urllib.request

import host

CFG = host.CFG.get('autocap', {})
URL = CFG.get('grafana_url') or 'https://zoltar.buoyant.io/grafana/api/datasources/uid/prometheus/resources/api/v1/query'
_M = CFG.get('model') or 'deepseek.*'
VMODEL, KMODEL = f'model_name=~"{_M}"', f'model=~"{_M}"'   # the vLLM and the router label for the same model
LOG = os.path.join(host.RUNS, 'autocap.log')
# Static ceiling for the GPU: performance degrades at TIP_AGENTS running agents, so the cap never passes 90 % of
# it. Measured 2026-11-12 on DeepSeek V4.1 Flash, by running agents: 35-39 was clean in 21 of 21 two-minute
# windows at 1816 tokens/s; 40-44 queued in 17 of 35 and added 2 % output; 45-49 queued in 29 of 29 at 183
# tokens/s. It is a heuristic for prompts like ours; the rules below only ever lower the cap from it.
TIP_AGENTS = int(CFG.get('tip_agents') or 40)
LO, CUT, P50_HARD = 10, 0.75, 3000
HI = int(os.environ.get('DAG_AUTOCAP_HI') or 0.9 * TIP_AGENTS)
LO = min(LO, HI)


def log(m):
    with open(LOG, 'a') as f:
        f.write(time.strftime('%H:%M:%S ') + m + '\n')


def query(q):
    tok = host.grafana_token()
    req = urllib.request.Request(URL + '?' + urllib.parse.urlencode({'query': q}),
                                 headers={'Authorization': f'Bearer {tok}', 'x-datasource-uid': 'prometheus'})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)['data']['result']
    v = float(res[0]['value'][1]) if res else 0.0
    return 0.0 if v != v else v  # NaN (no traffic) reads as idle


def cap(c, n=None):
    if n is None:
        r = c.execute("SELECT value FROM meta WHERE key='max_running'").fetchone()
        return int(r[0]) if r else HI
    c.execute("INSERT OR REPLACE INTO meta VALUES ('max_running', ?)", (str(n),))


# The controller polls every 15 s and keeps the last 10 min of samples. An earlier rule on a 10 min queue window
# raised 1 a minute into the tipping point (45 running tasks, 40 requests in flight) and lagged it by minutes. Backend in-flight counts every client, so load from
# another pipeline shows in the samples without our seeing its tasks.
# One rule, knee: hold in-flight at 90 % of the knee, the in-flight count at which queueing last began, using
# the 1 min peak load. Sharing between users is left to coordination, not to this controller (2026-11-12):
# other users' bursts are too irregular to remember, so the reserve and auto rules were removed.
# The knee is set at the throughput peak, not the tipping point (measured 2026-11-12, clean samples by in-flight):
# 10-14 in flight gave 1282 tokens/s at e2e p50 4.2 s; 30-34 gave 1844 at 7.1 s; 35-39 fell to 1747 at 8.1 s;
# the backend tipped at 45. Past 34 more agents add latency and no output, so 34 is the most we believe is useful.
KNEE0, KNEE_LO, AIM, K0 = 34, 10, 0.9, 0.85
# The KV cache is the limit behind the knee (measured 2026-11-12): 0.25 full at 14 in flight, 0.70 at 34, then
# 0.77 with the first preemption as queueing began, and generation fell from 1700 to 160 tokens/s for 14 min.
# It counts every client and grows with prompt size, which a request count cannot see, so every knee rule also
# keeps it under 90 % of KV_KNEE, and a preemption counts as queueing.
KV_KNEE = 0.75
CHURN = 150
TICK, KEEP = 15, 600


def new_state(knee=KNEE0, now=0):
    """Controller state: the knee, whether a queueing episode is open, and the time of the last raise."""
    return {'knee': float(knee), 'over': False, 'raised': 0}


def _over(st, hist, running, old, now):
    """Shared overload rule; returns a (cap, tag) cut, a hold, or None when the backend is not queueing.

    Waiting in 3 of the last 4 polls, or a 1 min TTFT p50 over 3 s twice, is an episode. Its first poll cuts to
    75 % of running and moves the knee to the in-flight peak that tipped it, by at most 20 %, because waiting
    also shows in short blips at low load (seen with 1 request in flight).
    """
    last = [h for h in hist if h[0] > now - 60]
    slow = len(last) >= 2 and all(h[3] > P50_HARD for h in last[-2:])
    # Preemptions (h[6]) mean the KV cache is full: two polls of them are an episode before anything waits long.
    evict = sum(1 for h in last if len(h) > 6 and h[6] > 0) >= 2
    # Prefix-index churn (h[7], blocks stored per second): 50 to 90 when healthy, 145 as the backend tipped and
    # 235 through the whole collapse, when cached prompts are dropped and recomputed. Unlike the KV gauge it
    # stays high while the backend is stuck, so it also marks an episode.
    evict = evict or sum(1 for h in last if len(h) > 7 and h[7] > CHURN) >= 2
    if sum(1 for h in last if h[2] > 0) < 3 and not slow and not evict:
        st['over'] = False
        return None
    if not st['over']:
        peak = max(h[1] for h in hist if h[0] > now - 180)
        st['knee'] = max(KNEE_LO, 0.8 * st['knee'], min(st['knee'], peak))
        st['over'] = True
        return max(LO, min(old, int(running * CUT))), 'CUT'
    # Running tasks are never stopped, so cut again only once the last cut is in force.
    return (max(LO, int(old * 0.9)), 'CUT') if running <= old else (old, 'over')


def step(st, hist, running, old, now):
    """Return (cap, tag) for one poll; pure, so the tests and the simulator drive it with made-up samples.

    `hist` is a list of (t, in-flight, waiting, ttft_p50_ms, running[, kv_cache_share, preemptions, index_churn]), oldest
    first, the last one current; without the two optional fields the KV limit is off.
    """
    cut = _over(st, hist, running, old, now)
    if cut:
        return cut
    full = running >= old - 2          # the cap is what limits us, so raising it means something
    # A raise shows in the 1 min load only a minute later: live, 10 % per 30 s went 29 to 42 on stale load and
    # had to come back to 38. So one raise per minute, and none on under a minute of samples (a fresh start).
    due = now - st['raised'] >= 60 and len(hist) >= 4
    last = [h for h in hist if h[0] > now - 60]
    load = max(h[1] + h[2] for h in last)
    # The knee never moves up on its own (2026-11-12): we control the clients, so we never cross the
    # tipping point to find it. An episode lowers it; raising it is a hand edit of `autocap_knee` in meta
    # after a capacity change. Rules that probe upward (aimd, gradient, a knee probe) were removed for this.
    # In-flight requests per running task, from the samples; another client's load inflates it, which
    # converts headroom into fewer tasks, the safe direction.
    ks = sorted(h[1] / h[4] for h in hist if h[4] >= 5)
    k = min(1.0, max(0.5, ks[len(ks) // 2])) if ks else K0
    want = int(running + (AIM * st['knee'] - load) / k)
    # KV limit: cache share per running task from the samples, and the 1 min peak, so one big prompt counts.
    kvs = sorted(h[5] / h[4] for h in hist if len(h) > 5 and h[4] >= 5 and h[5] > 0)
    if kvs:
        kv = max(h[5] for h in last if len(h) > 5)
        want = min(want, int(running + (AIM * KV_KNEE - kv) / kvs[len(kvs) // 2]))
    if want > old and full and due:
        st['raised'] = now
        return min(HI, want, old + max(1, round(old * 0.1))), 'raise'
    if want < old - 2:
        return max(LO, want), 'lower'
    return old, ''


def main():
    """The live controller: every 15 s, `step` moves the cap in tasks.db; the knee persists in meta."""
    c = sqlite3.connect(host.DB, timeout=60, isolation_level=None)
    r = c.execute("SELECT value FROM meta WHERE key='autocap_knee'").fetchone()
    c.close()
    st = new_state(float(r[0]) if r else KNEE0, time.time())
    hist = []
    log(f'START knee={st["knee"]:.0f}')
    while True:
        try:
            vrun = query(f'sum(vllm:num_requests_running{{{VMODEL}}})')
            wait = query(f'sum(vllm:num_requests_waiting{{{VMODEL}}})')
            p50 = 1000 * query(f'histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{VMODEL}}}[1m])))')
            kv = query(f'max(vllm:kv_cache_usage_perc{{{VMODEL}}})')
            pre = query(f'sum(increase(vllm:num_preemptions_total{{{VMODEL}}}[1m]))')
            # Tenants that sent a request in the last 2 min: how many clients share the backend right now.
            users = query(f'count(sum by (tenant) (rate(kinference_request_completions_total{{{KMODEL}}}[2m])) > 0)')
            churn = query('sum(rate(kinference_index_blocks_stored_total[1m]))')
            now = time.time()
            c = sqlite3.connect(host.DB, timeout=60, isolation_level=None)
            running = c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
            # Read the stored cap each poll, so a hand-set `tm.py cap` is the new starting point.
            old = cap(c)
            hist = [h for h in hist if h[0] > now - KEEP] + [(now, vrun, wait, p50, running, kv, pre, churn)]
            knee = st['knee']
            new, tag = step(st, hist, running, old, now)
            if new != old:
                cap(c, new)
            if st['knee'] != knee:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('autocap_knee', ?)", (f"{st['knee']:.1f}",))
            c.close()
            log(f'knee={st["knee"]:.0f} vrun={vrun:.0f} wait={wait:.0f} kv={kv:.2f} preempt={pre:.0f} churn={churn:.0f} users={users:.0f} '
                f'p50_1m={p50:.0f}ms '
                f'running={running} cap {old}->{new}{" " + tag if tag else ""}')
        except Exception as e:
            # A failed read leaves the cap as it is; a stale cap is safer than a guessed one.
            log(f'ERROR {type(e).__name__}: {e}')
        time.sleep(TICK)


if __name__ == '__main__':
    main()

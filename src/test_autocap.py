#!/usr/bin/env python3
"""Three cases that fix what `autocap.step` decides from a sample history, with no backend and no database.

A wrong step either starves the DAG or queues every client of the shared backend, and neither shows until a real
overload, so each rule is pinned here on made-up samples: (time, in-flight, waiting, TTFT p50 ms, running).
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autocap as a


def hist(rows, start=1000):
    """Samples 15 s apart from (in-flight, waiting, p50, running) rows; returns (history, time of the last one).

    They start at t=1000 so a fresh state, whose last raise is at 0, is past the one-raise-a-minute wait.
    """
    h = [(start + i * a.TICK, *r) for i, r in enumerate(rows)]
    return h, h[-1][0]


def test_knee_raises_toward_ninety_percent_and_stops():
    """Alone and clean, knee raises a full cap by at most 10 % and never past 90 % of the knee.

    Sets up 2 min of 17 requests in flight for 20 tasks at cap 20, then a load already at the target.
    Catches a raise that jumps to the knee in one step, or one that keeps raising at the target.
    """
    # Headroom of 19 requests would allow about 42 tasks; one step may add only 10 % of the cap.
    h, now = hist([(17, 0, 500, 20)] * 8)
    assert a.step(a.new_state(40, now), h, 20, 20, now) == (22, 'raise')

    # At 36 in flight (90 % of 40) nothing is left to give, so the cap holds.
    h, now = hist([(36, 0, 900, 42)] * 8)
    assert a.step(a.new_state(40, now), h, 42, 42, now) == (42, '')


def test_queueing_cuts_once_and_resets_the_knee():
    """Waiting in 3 of the last 4 polls cuts to 75 % of running and moves the knee to the peak that tipped it.

    Sets up a clean minute peaking at 44 in flight, then 3 polls with requests waiting, with a knee belief of 50.
    Catches a controller that waits out a long window before cutting, and one that cuts again each poll while
    running is still above the new cap, which would compound to the floor.
    """
    # Onset: cap 45 with 45 running goes to int(45 * 0.75), and the knee drops from 50 to the 44 peak.
    h, now = hist([(44, 0, 1200, 45)] * 4 + [(40, 6, 60000, 45)] * 3)
    st = a.new_state(50, now)
    assert a.step(st, h, 45, 45, now) == (33, 'CUT')
    assert st['knee'] == 44

    # Next poll, still queueing and running above the cap: hold, because the first cut is not in force yet.
    h2 = h + [(now + a.TICK, 40, 5, 60000, 45)]
    assert a.step(st, h2, 45, 33, now + a.TICK) == (33, 'over')


def test_a_waiting_blip_at_low_load_moves_the_knee_at_most_twenty_percent():
    """A queueing episode with little in flight lowers the knee by 20 % only, not to the low peak.

    Sets up 3 waiting polls with 5 requests in flight and a knee of 40.
    Catches one blip teaching the controller that the backend serves 5 requests.
    """
    h, now = hist([(5, 2, 800, 12)] * 4)
    st = a.new_state(40, now)
    a.step(st, h, 12, 15, now)
    assert st['knee'] == 32


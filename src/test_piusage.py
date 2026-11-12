#!/usr/bin/env python3
"""The three cases that decide whether piusage.usage can be trusted on a real step log.

A parser that returns nothing for a malformed log is worse than none, so each case says up front what
it must print: two assistant turns sum to their total, the text is the last assistant turn's alone,
and a truncated line is skipped without losing the turns around it. Usage: test_piusage.py; prints
each case and exits 1 unless the three behave as stated.
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from piusage import usage


def event(role, text, tin=None, tout=None):
    """One `message_end` line in the shape pi writes: the usage lives on the message, not the event."""
    msg = {'role': role, 'content': [{'type': 'text', 'text': text}]}
    if tin is not None:
        msg['usage'] = {'input': tin, 'output': tout, 'cacheRead': 0, 'cacheWrite': 0,
                        'totalTokens': tin + tout}
    return json.dumps({'type': 'message_end', 'message': msg})


def fixture(extra=''):
    """A step log with two assistant turns and one user turn, written to a temp path, for one case.

    The user turn carries a usage of its own so an implementation that sums every `message_end`
    instead of the assistant ones overshoots case 1 instead of passing by luck.
    """
    lines = [
        '{"type":"session","version":3,"id":"fixture","cwd":"/tmp"}',
        '{"type":"agent_start"}',
        event('user', 'the prompt', 1000, 1000),
        event('assistant', 'first reply', 53, 3),
        '{"type":"message_update","usage":{"input":9999,"output":9999}}',
        event('assistant', 'second reply', 120, 40),
        '{"type":"agent_end","messages":[],"willRetry":false}',
    ]
    fd, path = tempfile.mkstemp(prefix='piusage_', suffix='.jsonl')
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + ('\n' + extra if extra else '') + '\n')
    return path


def case(n, name, want, got):
    """Print one case as judged: what it must return, what it returned, and PASS or FAIL."""
    ok = got == want
    print(f'case {n}: {name}\n  want: {want}\n  got:  {got}\n  {"PASS" if ok else "FAIL"}\n')
    return ok


def main():
    c1 = c2 = c3 = False
    p = fixture()
    try:
        # Case 1: the two assistant turns sum, and the user turn's usage stays out.
        c1 = case(1, 'two assistant turns sum, the user turn does not', ('second reply', 173, 43),
                  usage(open(p).read().splitlines()))
        # Case 2: the text is the last assistant turn's, not the first one's and not combined.
        c2 = case(2, 'the text is the last assistant message alone', 'second reply', usage(open(p).read().splitlines())[0])
    finally:
        os.unlink(p)
    # Case 3: a truncated final line, which a killed step leaves behind, is skipped and changes nothing.
    p = fixture('{"type":"message_end","message":{"role":"assist')
    try:
        c3 = case(3, 'a line that is not JSON is skipped', ('second reply', 173, 43),
                  usage(open(p).read().splitlines()))
    finally:
        os.unlink(p)
    print(f'{"all three cases hold" if c1 and c2 and c3 else "a case failed"}')
    return 0 if c1 and c2 and c3 else 1


def test_piusage_cases():
    """All three cases hold: the pytest entry for the script, so `python3 -m pytest dag/src/` runs what `python3 dag/src/test_piusage.py` prints."""
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())

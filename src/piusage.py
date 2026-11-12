#!/usr/bin/env python3
"""Sum a `pi --mode json` step log down to its reply text and token counts.

sched.pi keeps only text stdout, so a finished step records no tokens; `pi --mode json` writes one
`message_end` per assistant turn carrying `usage.input` and `usage.output` (docs/json.md). Readers
here tolerate a log that also holds stray lines, because a step log can be wrapped in a banner.
"""
import json


def usage(lines):
    """Return (last assistant text, summed input tokens, summed output tokens) over `lines`.

    Only `message_end` events whose message role is `assistant` count, because the user, system and
    tool turns carry no usage of their own; a line that is not JSON is skipped, and so is a JSON
    value that is not an object.
    """
    text, tin, tout = '', 0, 0
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get('type') != 'message_end':
            continue
        msg = ev.get('message')
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        u = msg.get('usage') or {}
        tin += u.get('input') or 0
        tout += u.get('output') or 0
        # The reply is the text blocks only: thinking and tool-call blocks are the step's trace,
        # not its answer, and a caller logging the answer must not see them run together.
        content = msg.get('content')
        if isinstance(content, list):
            text = ''.join(b.get('text', '') for b in content
                           if isinstance(b, dict) and b.get('type') == 'text')
        elif isinstance(content, str):
            text = content
    return text, tin, tout

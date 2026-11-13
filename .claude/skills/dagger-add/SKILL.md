---
name: dagger-add
description: Install the task-DAG orchestrator into the current project as a `dag/` folder. Use when asked to "add dag to this project", set up the DAG, or install the orchestrator in a repo. The user may name the source, a git URL or a local path; otherwise it is `~/projects/dag` when that exists. Not for running a DAG that is already installed (read `dag/README.md` for that).
---

We install the DAG by following its own README, not from memory: the README is the source of truth and changes with the tool.

## Source

Pick the source in this order:

1. A git URL or local path the user names with the request.
2. `~/projects/dag`, when it exists: that is where it lives on this laptop.
3. Otherwise ask the user for a git URL or a path. Do not guess a URL.

For a git URL, clone it shallow into a new temp directory (`git clone --depth 1 <url> "$(mktemp -d)/dag"`) and use that clone as the source; delete the temp directory when the install is checked.

## Install

1. Read `<source>/README.md` and follow its `## Add dag to this project` section step by step, with `<source>` in place of the path it names.
2. If `<target>/dag` already exists, stop and report what is there. Never overwrite an installed DAG or its database.
3. Run every check the section lists and report each result with its output.

## Limits

- Do not start a scheduler, create tasks, commit or push as part of the install.
- Do not write the source's database, run files or caches into the target.
- End by telling the user the ways to start that the README lists, and stop.

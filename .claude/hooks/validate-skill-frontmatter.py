#!/usr/bin/env python3
"""Validate SKILL.md YAML frontmatter.

Two modes:
  * hook mode (default) — reads Claude Code's PostToolUse JSON on stdin, picks
    the edited file, and validates it if it is a SKILL.md. Exit 2 (with the
    reason on stderr) tells Claude the edit produced broken frontmatter.
  * `--all` — validates every skills/*/SKILL.md in the repo. Use in CI or by
    hand: python3 .claude/hooks/validate-skill-frontmatter.py --all

The failure this exists to catch: an unquoted YAML scalar cannot contain
": " (colon + space) — YAML reads it as a nested mapping key and the whole
frontmatter fails to parse, which breaks GitHub's rendering and `skills add`
for every skill in the repo, not just the malformed one.
"""

import json
import re
import sys
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
REQUIRED_KEYS = ("name", "description")

try:
    import yaml
except ImportError:
    yaml = None


def plain_scalar_hazards(block):
    """Fallback checker for when PyYAML is unavailable.

    Only inspects unquoted (plain) values, which is where these breakages live.
    """
    problems = []
    for lineno, line in enumerate(block.split("\n"), 1):
        m = re.match(r"^([A-Za-z_][\w-]*):\s+(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if not value or value[0] in "\"'|>[{":
            continue  # quoted or block scalar — the hazards below don't apply
        offset = m.start(2)
        hit = re.search(r":(\s|$)", value)
        if hit:
            problems.append(
                (lineno, offset + hit.start() + 1, key, "': ' inside an unquoted value")
            )
        hit = re.search(r"\s#", value)
        if hit:
            problems.append(
                (lineno, offset + hit.start() + 2, key, "' #' starts a YAML comment")
            )
    return problems


def validate(path):
    """Return a list of human-readable problems for one SKILL.md."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"cannot read: {e}"]

    m = FRONTMATTER.match(text)
    if not m:
        return ["no YAML frontmatter (file must start with a --- ... --- block)"]
    block = m.group(1)

    if yaml is None:
        return [
            f"line {ln} col {col}: {why} (key `{key}`) — quote the value or reword"
            for ln, col, key, why in plain_scalar_hazards(block)
        ]

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1} col {mark.column + 1}: " if mark else ""
        detail = getattr(e, "problem", None) or str(e).replace("\n", " ")
        extra = plain_scalar_hazards(block)
        hint = f" — likely {extra[0][3]}" if extra else ""
        return [f"{where}{detail}{hint}"]

    if not isinstance(data, dict):
        return ["frontmatter is not a mapping of key: value pairs"]

    problems = [f"missing required key `{k}`" for k in REQUIRED_KEYS if not data.get(k)]
    name = data.get("name")
    if name and name != path.parent.name:
        problems.append(f"name `{name}` does not match directory `{path.parent.name}`")
    return problems


def report(path, problems):
    root = Path.cwd()
    try:
        shown = path.relative_to(root)
    except ValueError:
        shown = path
    for p in problems:
        print(f"{shown}: {p}", file=sys.stderr)


def main():
    if "--all" in sys.argv[1:]:
        root = Path(__file__).resolve().parents[2]
        failed = 0
        for path in sorted(root.glob("skills/*/SKILL.md")):
            problems = validate(path)
            if problems:
                failed += 1
                report(path, problems)
        if failed:
            print(f"\n{failed} SKILL.md file(s) with invalid frontmatter.", file=sys.stderr)
            return 1
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # not a hook invocation; nothing to check

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    raw = tool_response.get("filePath") or tool_input.get("file_path")
    if not raw:
        return 0

    path = Path(raw)
    if path.name != "SKILL.md" or not path.exists():
        return 0

    problems = validate(path)
    if not problems:
        return 0

    print("Invalid YAML frontmatter — fix before continuing:", file=sys.stderr)
    report(path, problems)
    if any("': '" in p for p in problems):
        print(
            "A `: ` (colon + space) inside an unquoted description breaks the whole "
            "frontmatter, which breaks GitHub rendering and `skills add` for every "
            "skill in the repo. Reword with an em dash or quote the value.",
            file=sys.stderr,
        )
    return 2  # blocking error: stderr is fed back to Claude


if __name__ == "__main__":
    sys.exit(main())

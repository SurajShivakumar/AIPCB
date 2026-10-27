"""Re-emit KiCad S-expression files in KiCad's own layout (tabs, one list per line).

Tools that write .kicad_sch text (the engine included) don't always match
KiCad's formatting. This round-trips a file through a tokenizer and prints it
the way eeschema does, so hand edits and tool edits look identical in the file
and in diffs. format_kicad(original) == original for files saved by KiCad 9.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)', re.S)

# Lists whose list-children stay on the parent's line (KiCad prints these inline).
INLINE_CHILDREN = {"pts"}
WRAP = 110


def parse(text: str):
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"Bad S-expression near offset {pos}: {text[pos:pos + 40]!r}")
        tok = m.group(1)
        pos = m.end()
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("Unbalanced parentheses")
    return stack[0]


def _emit(node, depth: int, out: list[str]) -> None:
    atoms_only = all(not isinstance(c, list) for c in node)
    if atoms_only:
        out.append("(" + " ".join(node) + ")")
        return
    head = []
    i = 0
    while i < len(node) and not isinstance(node[i], list):
        head.append(node[i])
        i += 1
    out.append("(" + " ".join(head))
    if head and head[0] in INLINE_CHILDREN:
        parts = []
        for c in node[i:]:
            sub: list[str] = []
            _emit(c, depth + 1, sub)
            parts.append("".join(sub))
        # KiCad packs points onto a line until it would pass WRAP columns (indent not counted).
        lines = [parts[0]]
        for part in parts[1:]:
            if len(lines[-1]) + 1 + len(part) > WRAP:
                lines.append(part)
            else:
                lines[-1] += " " + part
        for line in lines:
            out.append("\n" + "\t" * (depth + 1) + line)
    else:
        for c in node[i:]:
            out.append("\n" + "\t" * (depth + 1))
            if isinstance(c, list):
                _emit(c, depth + 1, out)
            else:
                out.append(c)
    out.append("\n" + "\t" * depth + ")")


def format_kicad(text: str) -> str:
    """Reformat like KiCad does, keeping the file's line endings (KiCad on Windows may write CRLF)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    roots = parse(text)
    out: list[str] = []
    for r in roots:
        _emit(r, 0, out)
        out.append("\n")
    result = "".join(out)
    return result.replace("\n", newline) if newline != "\n" else result

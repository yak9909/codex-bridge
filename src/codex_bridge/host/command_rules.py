"""Conservative tokenization for command rules. The allowlist uses its own matcher."""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from typing import Literal

Shell = Literal["cmd", "posix", "powershell"]
_WINDOWS_PROGRAM = re.compile(r"""(?i)((?:[a-z]:[\\/]|\\\\)[^:"'\r\n&|;]*?\.(?:exe|cmd|bat|com))(?=\s|$)""")


@dataclass(frozen=True)
class Word:
    value: str
    start: int
    end: int
    complete: bool = True


def lex(command: str, shell: Shell = "cmd", *, pattern: bool = False) -> tuple[list[list[Word]], bool]:
    """Return recognizable commands and whether any part needs manual review."""
    commands: list[list[Word]] = []
    words: list[Word] = []
    parts: list[str] = []
    start = None
    quote = ""
    uncertain = False
    quotes = '"' if shell == "cmd" else "'\""
    escape = {"cmd": "^", "posix": "\\", "powershell": chr(96)}[shell]

    def finish_word(end: int, *, complete: bool = True) -> None:
        nonlocal start
        if start is not None:
            words.append(Word("".join(parts), start, end, complete))
            parts.clear()
            start = None

    i = 0
    while i < len(command):
        char = command[i]
        if not quote and not pattern and char in "&|;\r\n":
            finish_word(i)
            if words:
                commands.append(words)
                words = []
        elif not quote and char.isspace():
            finish_word(i)
        elif not quote and start is None and not pattern and shell != "cmd" and char == "#":
            # A line comment ends at the next separator newline.
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        else:
            if start is None:
                start = i
                # Also recognize unquoted absolute Windows executable paths for restrictive rules.
                path = _WINDOWS_PROGRAM.match(command, i) if shell == "cmd" and not words and not pattern else None
                if path:
                    parts.append(path[1])
                    i = path.end()
                    continue
            following = command[i + 1:i + 2]
            escapes = char == escape and (
                not quote or (quote == '"' and shell != "cmd" and (
                    shell == "powershell" or following in ('"', '\\', '$', chr(96), '\n', ''))))
            if escapes and not pattern:
                if not following:
                    uncertain = True
                    finish_word(len(command), complete=False)
                elif not (shell == "posix" and following == "\n"):
                    parts.append(following)
                i += bool(following)
            elif char == quote:
                if shell == "powershell" and following == quote:
                    parts.append(char)
                    i += 1
                else:
                    quote = ""
            elif not quote and char in quotes:
                quote = char
            else:
                if not pattern and not quote and char in "()<>":
                    uncertain = True
                if not pattern and shell != "cmd" and quote != "'" and char == "$":
                    uncertain = True
                parts.append(char)
        i += 1
    if quote:
        uncertain = True
    finish_word(i, complete=not quote)
    if words:
        commands.append(words)
    return commands, uncertain


def program_name(token: str) -> str:
    name = ntpath.basename(token).lower()
    stem, ext = ntpath.splitext(name)
    return stem if ext in (".exe", ".cmd", ".bat", ".com") else name


def normalized_path(token: str) -> str:
    return ntpath.normpath(token).replace("\\", "/") if token else token


def _payload(command: str, words: list[Word], shell: Shell) -> tuple[str | None, Shell, bool]:
    program = program_name(words[0].value)
    if shell == "cmd" and program == "call":
        return (command[words[1].start:words[-1].end] if len(words) > 1 else None), "cmd", len(words) == 1
    for i, word in enumerate(words[1:], start=1):
        flag = word.value.lower()
        if program in ("cmd", "%comspec%") and flag.startswith(("/c", "/k")) and len(flag) > 2:
            raw = command[word.start:words[-1].end]
            if raw[:2].lower() != flag[:2]:
                return None, "cmd", True
            tail = raw[2:].lstrip()
            if tail.startswith('"') and tail.endswith('"'):
                tail = tail[1:-1]
            return tail, "cmd", not tail.strip()
        if program in ("cmd", "%comspec%") and flag in ("/c", "/k"):
            child: Shell = "cmd"
        elif program in ("powershell", "pwsh") and flag.startswith("-"):
            if flag in ("-ec", "-e") or (len(flag) > 1 and "-encodedcommand".startswith(flag)):
                return None, "powershell", True
            if len(flag) < 2 or not "-command".startswith(flag):
                continue
            child = "powershell"
        elif program in ("bash", "sh") and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", flag):
            child = "posix"
        else:
            continue
        if i + 1 == len(words):
            return None, child, True
        first = words[i + 1]
        tail = command[first.start:words[-1].end]
        if child == "posix":
            if shell == "cmd" and tail.startswith("'"):
                return (tail[1:tail.index("'", 1)], child, False) if "'" in tail[1:] else (None, child, True)
            return first.value, child, False
        if child == "cmd":
            if tail.startswith('""') and tail.endswith('"'):
                tail = tail[1:-1]
            elif i + 2 == len(words):
                tail = first.value
        elif tail.startswith("'") and tail.endswith("'"):
            tail = tail[1:-1]
        else:
            tail = first.value + command[first.end:words[-1].end] if command[first.start] == '"' else tail
        return tail, child, not tail.strip()
    return None, shell, False


def inspect_commands(command: str, shell: Shell = "cmd", depth: int = 0) -> tuple[list[list[str | None]], bool]:
    segments, uncertain = lex(command, shell)
    commands = []
    for words in segments:
        commands.append([word.value if word.complete else None for word in words])
        if shell == "cmd" and any(re.search(r"%[^%]+%|![^!]+!", word.value)
                                  for word in words if word.value.lower() != "%comspec%"):
            uncertain = True
        if any(not word.complete for word in words):
            continue
        payload, child, unknown = _payload(command, words, shell)
        uncertain |= unknown
        if payload is not None:
            if depth >= 3:
                uncertain = True
            else:
                nested, unknown = inspect_commands(payload, child, depth + 1)
                commands.extend(nested)
                uncertain |= unknown
    return commands, uncertain

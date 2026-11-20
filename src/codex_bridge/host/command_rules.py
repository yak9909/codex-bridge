"""Conservative tokenization for command rules. The allowlist uses its own matcher."""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from typing import Callable, Literal

Shell = Literal["cmd", "posix", "powershell"]
_WINDOWS_PROGRAM = re.compile(r"""(?i)((?:[a-z]:[\\/]|\\\\)[^:"'\r\n&|;,=<>]*?\.(?:exe|cmd|bat|com))(?=\s|$)""")

_REDIRECTION = re.compile(r"[<>]{1,2}(&[0-9-]+)?")
_CMD_EXPANSION = re.compile(r"%[^%]+%|![^!]+!|%[0-9*]")


@dataclass(frozen=True)
class Word:
    value: str
    start: int
    end: int
    complete: bool = True
    expands: bool = False
    has_parentheses: bool = False


def _expression_end(command: str, start: int, shell: Shell, closing: str) -> int | None:
    """Find the end of a substitution without treating quoted delimiters as syntax."""
    quote = ""
    level = 1
    escape = "\\" if shell == "posix" else chr(96)
    i = start
    while i < len(command):
        char = command[i]
        if char == escape and quote != "'":
            i += 2
            continue
        if closing != ")" and char == closing:
            return i
        if char == quote:
            if shell == "powershell" and command[i + 1:i + 2] == char:
                i += 2
                continue
            quote = ""
        elif not quote:
            if char in "'\"":
                quote = char
            elif char == ")" and closing == ")":
                level -= 1
                if level == 0:
                    return i
            elif char == "(" and closing == ")":
                level += 1
        i += 1
    return None


def lex(command: str, shell: Shell = "cmd", *, pattern: bool = False, cmd_semicolons: bool = False,
        expressions: list[tuple[str, Shell]] | None = None,
        inputs: list[str] | None = None) -> tuple[list[list[Word]], bool]:
    """Return recognizable commands and whether any part needs manual review."""
    commands: list[list[Word]] = []
    words: list[Word] = []
    parts: list[str] = []
    start = None
    quote = ""
    uncertain = False
    expands = has_parentheses = redirect_target = input_target = False
    groups = 0
    separators = "&|\r\n" + (";" if shell != "cmd" or cmd_semicolons else "")
    cmd_delimiters = ",;=" if shell == "cmd" and not pattern else ""
    quotes = '"' if shell == "cmd" else "'\""
    escape = {"cmd": "^", "posix": "\\", "powershell": chr(96)}[shell]

    def finish_word(end: int, *, complete: bool = True) -> None:
        nonlocal start, expands, has_parentheses, redirect_target, input_target
        if start is not None:
            value = "".join(parts)
            if not redirect_target:
                words.append(Word(value, start, end, complete, expands, has_parentheses))
            elif input_target and inputs is not None and complete:
                inputs.append(value)
            parts.clear()
            start = None
            expands = has_parentheses = redirect_target = input_target = False

    i = 0
    while i < len(command):
        char = command[i]
        if not quote and not pattern and char in "<>":
            if parts and all(part.isascii() and part.isdigit() for part in parts):
                parts.clear()
                start = None
            finish_word(i)
            redirection = _REDIRECTION.match(command, i)
            assert redirection is not None
            redirect_target = redirection[1] is None
            input_target = redirection[0] == "<"
            i = redirection.end()
            continue
        if expressions is not None and not pattern and quote != "'":
            opening = 0
            closing = ")"
            if command.startswith("$(", i) and (shell != "cmd" or not quote):
                opening = 2
            elif shell == "powershell" and char == "(" and not quote and (words or start is not None):
                opening = 1
            elif shell == "posix" and char == chr(96):
                opening = 1
                closing = chr(96)
            if opening:
                child: Shell = "posix" if shell == "cmd" else shell
                end = _expression_end(command, i + opening, child, closing)
                if start is None:
                    start = i
                if end is None:
                    uncertain = True
                    finish_word(len(command), complete=False)
                    break
                expressions.append((command[i + opening:end], child))
                expands |= opening == 2 or closing != ")"
                parts.append(command[i:end + 1])
                i = end + 1
                continue
        group_start = char == "(" and start is None and not words and not redirect_target
        group_end = char == ")" and groups > 0
        if not quote and not pattern and (char in separators or group_start or group_end):
            finish_word(i)
            if words:
                commands.append(words)
                words = []
            if redirect_target:
                uncertain = True
                redirect_target = input_target = False
            groups += int(group_start) - int(group_end)
            uncertain |= shell == "powershell" and char == "&" and not (
                command[i:i + 2] == "&&" or (i > 0 and command[i - 1] == "&"))
        elif not quote and (char.isspace() or char in cmd_delimiters):
            finish_word(i)
        elif shell == "cmd" and not pattern and char == "@" and start is None and not words and not redirect_target:
            pass
        elif not quote and start is None and not pattern and shell != "cmd" and char == "#":
            # A line comment ends at the next separator newline.
            end = command.find("\n", i)
            i = len(command) if end < 0 else end
            continue
        else:
            if start is None:
                start = i
                # Also recognize unquoted absolute Windows executable paths for restrictive rules.
                path = (_WINDOWS_PROGRAM.match(command, i)
                        if shell == "cmd" and not words and not pattern and not redirect_target else None)
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
                elif shell == "cmd" and following in "\r\n":
                    if following == "\r" and command[i + 2:i + 3] == "\n":
                        i += 1
                    if not parts:
                        start = None
                elif shell == "cmd" and following.isspace():
                    finish_word(i)
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
                if not pattern:
                    has_parentheses |= not quote and char in "()"
                    expands |= shell != "cmd" and quote != "'" and char == "$"
                    uncertain |= shell == "powershell" and not quote and char in "{}"
                parts.append(char)
        i += 1
    uncertain |= bool(quote or groups or (redirect_target and start is None))
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


def _start_payload(command: str, words: list[Word]) -> str | None:
    title_seen = False
    skip_value = False
    for word in words[1:]:
        if skip_value:
            skip_value = False
        elif word.value.startswith("/"):
            skip_value = word.value.lower() in ("/d", "/node", "/affinity", "/machine")
        elif not title_seen and command[word.start] == '"':
            title_seen = True
        else:
            return command[word.start:words[-1].end]
    return None


_POWERSHELL_VALUE_OPTIONS = (
    "-windowstyle", "-executionpolicy", "-configurationname", "-configurationfile",
    "-outputformat", "-inputformat", "-psconsolefile", "-version", "-workingdirectory",
    "-settingsfile", "-custompipename",
)
_POWERSHELL_SWITCHES = (
    "-noprofile", "-noprofileloadtime", "-nologo", "-noexit", "-noninteractive",
    "-interactive", "-sta", "-mta", "-login", "-help", "-?",
)


def _powershell_argument(words: list[Word]) -> tuple[int | None, bool]:
    implicit = None
    skip_value = False
    uncertain = False
    for i, word in enumerate(words[1:], start=1):
        if skip_value:
            skip_value = False
            continue
        flag = word.value.lower()
        if not flag.startswith(("-", "/")):
            if implicit is None:
                implicit = i
            continue
        flag, separator, _ = ("-" + flag[1:]).partition(":")
        if flag == "-ec" or (len(flag) > 1 and "-encodedcommand".startswith(flag)):
            return None, True
        if len(flag) > 1 and "-command".startswith(flag):
            if separator or i + 1 == len(words):
                return None, True
            return i + 1, uncertain
        if len(flag) > 1 and "-file".startswith(flag):
            # Everything after the script name belongs to the script.
            return implicit, uncertain or (not separator and i + 1 == len(words))
        if len(flag) > 1 and (flag == "-ep" or any(option.startswith(flag) for option in _POWERSHELL_VALUE_OPTIONS)):
            skip_value = not separator
        elif len(flag) < 2 or not any(option.startswith(flag) for option in _POWERSHELL_SWITCHES):
            uncertain = True
    return implicit, uncertain or skip_value


def _payload(command: str, words: list[Word], shell: Shell) -> tuple[str | None, Shell, bool]:
    program = program_name(words[0].value)
    if shell == "cmd" and program == "call":
        return (command[words[1].start:words[-1].end] if len(words) > 1 else None), "cmd", len(words) == 1
    if shell == "cmd" and program == "start":
        payload = _start_payload(command, words)
        return payload, "cmd", payload is None
    if program in ("powershell", "pwsh"):
        index, unknown = _powershell_argument(words)
        if index is None:
            return None, "powershell", unknown
        first = words[index]
        tail = command[first.start:words[-1].end]
        if tail.startswith("'") and tail.endswith("'"):
            tail = tail[1:-1]
        elif command[first.start] == '"':
            tail = first.value + command[first.end:words[-1].end]
        return tail, "powershell", unknown or not tail.strip()
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
        elif program in ("bash", "sh") and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word.value):
            child = "posix"
        else:
            continue
        index = i + 1
        if child == "posix" and index < len(words) and words[index].value == "--":
            index += 1
        if index == len(words):
            return None, child, True
        first = words[index]
        tail = command[first.start:words[-1].end]
        if child == "posix":
            if shell == "cmd" and tail.startswith("'"):
                return (tail[1:tail.index("'", 1)], child, False) if "'" in tail[1:] else (None, child, True)
            return first.value, child, False
        if tail.startswith('""') and tail.endswith('"'):
            tail = tail[1:-1]
        elif index + 1 == len(words):
            tail = first.value
        return tail, child, not tail.strip()
    return None, shell, False


def inspect_commands(command: str, shell: Shell = "cmd", depth: int = 0, *,
                     related_program: Callable[[str], bool] = lambda _: True,
                     inputs: list[str] | None = None) -> tuple[list[list[str | None]], bool]:
    expressions: list[tuple[str, Shell]] = []
    segments, uncertain = lex(command, shell, expressions=expressions, inputs=inputs)
    if shell == "cmd" and ";" in command:
        extra, unknown = lex(command, shell, cmd_semicolons=True, expressions=[], inputs=inputs)
        if extra != segments:
            segments.extend(extra)
            uncertain |= unknown
    commands = []
    for words in segments:
        commands.append([word.value if word.complete else None for word in words])
        first = words[0]
        program = program_name(first.value)
        related = related_program(first.value)
        if shell == "cmd":
            uncertain |= program in ("if", "for")
            uncertain |= program != "%comspec%" and bool(_CMD_EXPANSION.search(first.value))
            if related:
                uncertain |= any(_CMD_EXPANSION.search(word.value) for word in words[1:])
        else:
            uncertain |= first.expands or (related and any(word.expands for word in words[1:]))
            uncertain |= shell == "powershell" and program in ("iex", "invoke-expression")
        uncertain |= related and any(word.has_parentheses for word in words)
        if any(not word.complete for word in words):
            continue
        payload, child, unknown = _payload(command, words, shell)
        uncertain |= unknown
        if payload is not None:
            expressions.append((payload, child))
    if depth >= 3:
        return commands, uncertain or bool(expressions)
    for payload, child in expressions:
        nested, unknown = inspect_commands(payload, child, depth + 1, related_program=related_program, inputs=inputs)
        commands.extend(nested)
        uncertain |= unknown
    return commands, uncertain

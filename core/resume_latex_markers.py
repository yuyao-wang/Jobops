"""Controlled LaTeX content markers shared by construction and validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


RESUME_LATEX_MARKER_CONTRACT_VERSION = "jobops-latex-markers-v1"
JOBOPS_CONTENT_BEGIN = "%% JOBOPS-CONTENT-BEGIN"
JOBOPS_CONTENT_END = "%% JOBOPS-CONTENT-END"
SECTION_MACRO = "JobopsSection"
BULLET_MACRO = "JobopsBullet"

MARKER_MACRO_DEFINITIONS = (
    "\\providecommand{\\JobopsSection}[2]{\\section*{#2}}\n"
    "\\providecommand{\\JobopsBullet}[2]{\\item #2}\n"
)

#: Visible runs at least this long are treated as historical resume content.
#: Shorter runs (a name, a contact line, a section label) may legitimately
#: carry over from the base layout, so they are not stale-content evidence.
STALE_CONTENT_MIN_CHARS = 40

# Single-pass map: sequential str.replace would re-escape the braces that
# \textbackslash{} and friends introduce.
_LATEX_ESCAPES = {
    "\\": "\\textbackslash{}",
    "{": "\\{",
    "}": "\\}",
    "&": "\\&",
    "%": "\\%",
    "$": "\\$",
    "#": "\\#",
    "_": "\\_",
    "~": "\\textasciitilde{}",
    "^": "\\textasciicircum{}",
}
_COMMAND_PATTERN = re.compile(r"\\[A-Za-z@]+\*?")
_NON_TEXT_PATTERN = re.compile(r"[^0-9A-Za-z]+")


class ResumeLatexMarkerError(ValueError):
    """The LaTeX text does not satisfy the controlled marker contract."""


def escape_latex(value: str) -> str:
    """Escape one Draft string for LaTeX without altering its wording."""

    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return "".join(
        _LATEX_ESCAPES.get(character, character) for character in value
    )


def uses_controlled_markers(source: str) -> bool:
    """Report whether a LaTeX source already carries the controlled region."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    return (
        source.count(JOBOPS_CONTENT_BEGIN) == 1
        and source.count(JOBOPS_CONTENT_END) == 1
        and source.index(JOBOPS_CONTENT_BEGIN)
        < source.index(JOBOPS_CONTENT_END)
    )


def split_controlled_region(source: str) -> tuple[str, str, str]:
    """Return the text before, inside and after the controlled region."""

    if not uses_controlled_markers(source):
        raise ResumeLatexMarkerError(
            "source does not contain exactly one controlled region"
        )
    begin = source.index(JOBOPS_CONTENT_BEGIN)
    end = source.index(JOBOPS_CONTENT_END)
    return (
        source[:begin],
        source[begin + len(JOBOPS_CONTENT_BEGIN) : end],
        source[end + len(JOBOPS_CONTENT_END) :],
    )


def _read_braced(text: str, start: int) -> tuple[str, int]:
    """Read one brace group, honouring backslash-escaped braces."""

    if start >= len(text) or text[start] != "{":
        raise ResumeLatexMarkerError("expected a brace group")
    depth = 0
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text):
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
        index += 1
    raise ResumeLatexMarkerError("unbalanced brace group")


@dataclass(frozen=True, slots=True)
class ParsedMarker:
    macro: str
    marker_id: str
    text: str
    order: int


def parse_markers(region: str) -> tuple[ParsedMarker, ...]:
    """Parse every controlled marker in document order."""

    if not isinstance(region, str):
        raise TypeError("region must be a string")
    markers: list[ParsedMarker] = []
    pattern = re.compile(rf"\\({SECTION_MACRO}|{BULLET_MACRO})\s*(?=\{{)")
    index = 0
    order = 0
    while True:
        match = pattern.search(region, index)
        if match is None:
            break
        marker_id, after_id = _read_braced(region, match.end())
        text, after_text = _read_braced(region, after_id)
        markers.append(
            ParsedMarker(
                macro=match.group(1),
                marker_id=marker_id,
                text=text,
                order=order,
            )
        )
        order += 1
        index = after_text
    return tuple(markers)


def render_marker(macro: str, marker_id: str, text: str) -> str:
    if macro not in {SECTION_MACRO, BULLET_MACRO}:
        raise ResumeLatexMarkerError("unknown controlled macro")
    return f"\\{macro}{{{marker_id}}}{{{escape_latex(text)}}}"


def visible_text_runs(source: str) -> frozenset[str]:
    """Normalized visible-text runs, used to detect stale resume content."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    runs: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("%"):
            continue
        without_commands = _COMMAND_PATTERN.sub(" ", stripped)
        for part in re.split(r"[{}]", without_commands):
            normalized = " ".join(
                token
                for token in _NON_TEXT_PATTERN.split(part)
                if token
            ).casefold()
            if len(normalized) >= STALE_CONTENT_MIN_CHARS:
                runs.add(normalized)
    return frozenset(runs)


def normalized_run(value: str) -> str:
    return " ".join(
        token for token in _NON_TEXT_PATTERN.split(value) if token
    ).casefold()


def source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def marker_contract_dict() -> dict[str, Any]:
    """The exact contract handed to a bounded construction Agent."""

    return {
        "begin_sentinel": JOBOPS_CONTENT_BEGIN,
        "bullet_macro": f"\\{BULLET_MACRO}{{bullet_id}}{{text}}",
        "contract_version": RESUME_LATEX_MARKER_CONTRACT_VERSION,
        "end_sentinel": JOBOPS_CONTENT_END,
        "macro_definitions": MARKER_MACRO_DEFINITIONS,
        "section_macro": f"\\{SECTION_MACRO}{{section_id}}{{title}}",
    }


__all__ = [
    "BULLET_MACRO",
    "JOBOPS_CONTENT_BEGIN",
    "JOBOPS_CONTENT_END",
    "MARKER_MACRO_DEFINITIONS",
    "RESUME_LATEX_MARKER_CONTRACT_VERSION",
    "SECTION_MACRO",
    "STALE_CONTENT_MIN_CHARS",
    "ParsedMarker",
    "ResumeLatexMarkerError",
    "escape_latex",
    "marker_contract_dict",
    "normalized_run",
    "parse_markers",
    "render_marker",
    "source_sha256",
    "split_controlled_region",
    "uses_controlled_markers",
    "visible_text_runs",
]

"""Shared deterministic dependency policy for single-source Resume LaTeX."""

from __future__ import annotations

import re

from .managed_resume_template import MANAGED_RESUME_LATEX_PACKAGES


RESUME_LATEX_DEPENDENCY_POLICY_VERSION = (
    "resume-latex-single-file-dependencies-v1"
)

_FILE_DEPENDENCY_PATTERN = re.compile(
    r"\\(?:input|include|includegraphics|lstinputlisting|subfile"
    r"|includepdf|bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{"
)
_SINGLE_FILE_EXTERNAL_TOKEN_PATTERN = re.compile(
    r"\\(?:input|include|includegraphics|lstinputlisting|subfile"
    r"|includepdf|bibliography|addbibresource|graphicspath"
    r"|InputIfFileExists|IfFileExists|includeonly|setmainfont"
    r"|setsansfont|setmonofont|fontspec)\b"
)
_USEPACKAGE_PATTERN = re.compile(
    r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}"
)
_USEPACKAGE_TOKEN = re.compile(r"\\usepackage\b")


def unmanaged_file_dependencies(source: str) -> tuple[str, ...]:
    """Return external-file macros forbidden by a single-source contract."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    found: list[str] = []
    for match in _FILE_DEPENDENCY_PATTERN.finditer(source):
        macro = match.group(0)
        name = macro.strip()[1:].split("[")[0].split("{")[0].strip()
        if name and name not in found:
            found.append(name)
    return tuple(found)


def unmanaged_latex_packages(source: str) -> tuple[str, ...]:
    """Return packages outside the closed managed-template allowlist."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    matches = tuple(_USEPACKAGE_PATTERN.finditer(source))
    if len(matches) != len(tuple(_USEPACKAGE_TOKEN.finditer(source))):
        return ("<dynamic-package-expression>",)
    packages: list[str] = []
    allowed = set(MANAGED_RESUME_LATEX_PACKAGES)
    for match in matches:
        for raw in match.group(1).split(","):
            package = raw.strip()
            if (
                not package
                or re.fullmatch(r"[A-Za-z0-9._-]+", package) is None
                or package not in allowed
            ) and package not in packages:
                packages.append(package or "<empty-package>")
    return tuple(packages)


def single_file_external_dependencies(source: str) -> tuple[str, ...]:
    """Return every external-file capability forbidden by the strict profile."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    found = list(unmanaged_file_dependencies(source))
    for match in _SINGLE_FILE_EXTERNAL_TOKEN_PATTERN.finditer(source):
        name = match.group(0)[1:]
        if name not in found:
            found.append(name)
    return tuple(found)


__all__ = [
    "MANAGED_RESUME_LATEX_PACKAGES",
    "RESUME_LATEX_DEPENDENCY_POLICY_VERSION",
    "unmanaged_file_dependencies",
    "unmanaged_latex_packages",
    "single_file_external_dependencies",
]

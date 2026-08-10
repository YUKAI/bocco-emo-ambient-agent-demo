#!/usr/bin/env python3
"""Fail if anything under hermes/ imports a module outside the standard library.

The bridge runs the fast-route scripts as a subprocess under an interpreter this
repository does not own (`fast_route_python`, `/opt/hermes-agent/.venv/bin/python`
on the Pi), and the Hermes skills are executed by the agent under that same
virtualenv. No install step in this repository puts anything there on their
behalf.

So a single `import requests` in a fast route does not fail here. It fails on the
robot, at the moment somebody asks about the weather, as a route that quietly
stops answering and falls back to a slow model call — or, for a skill, as an
agent that reports it cannot look something up.

The check is import-only and never executes the scripts, because executing them
means network calls to Open-Meteo, NHK and Gmail.

Run it against the interpreter the robot has (3.11): the standard library is not
the same set in every version, and the Pi's version is the one that decides.

    python3 scripts/check-hermes-stdlib.py
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys


def imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Every distribution the module imports, with line numbers.

    "Distribution" meaning the first dotted component — ``xml`` for
    ``xml.etree.ElementTree`` — since that is what has to be installed.

    ``ast.walk`` rather than ``tree.body``, so an import nested inside a
    function or guarded by a ``try`` is reported too. A deferred import is
    still an import: it fails when that branch runs, on the robot, which is
    later and quieter than failing at module load.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            # A relative import resolves inside the skill's own directory and
            # cannot reach a package that is not installed.
            if node.level == 0 and node.module:
                found.append((node.lineno, node.module.split(".")[0]))
    return found


def is_local_sibling(script: Path, name: str) -> bool:
    """True when the import resolves to a file shipped beside the script."""
    directory = script.parent
    return (directory / f"{name}.py").is_file() or (
        directory / name / "__init__.py"
    ).is_file()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    hermes = root / "hermes"
    scripts = sorted(hermes.rglob("*.py"))

    if not scripts:
        print(f"error: no Python found under {hermes}", file=sys.stderr)
        print("  this check is looking in the wrong place", file=sys.stderr)
        return 1

    violations: list[str] = []
    for script in scripts:
        relative = script.relative_to(root)
        try:
            tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        except SyntaxError as error:
            violations.append(f"{relative}:{error.lineno}: cannot parse: {error.msg}")
            continue

        for lineno, name in imported_names(tree):
            if name in sys.stdlib_module_names:
                continue
            if is_local_sibling(script, name):
                continue
            violations.append(
                f"{relative}:{lineno}: imports {name!r}, which is not in the "
                f"Python {sys.version_info.major}.{sys.version_info.minor} "
                "standard library"
            )

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if violations:
        print(
            f"error: hermes scripts must run on a bare Python {version}:",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "\n  Nothing installs dependencies for these scripts. Use the "
            "standard library,\n  or move the work into the bridge, which has "
            "a dependency list.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(scripts)} script(s) under hermes/ import only the Python {version} standard library")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

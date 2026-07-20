"""The review gate: no migration reaches the database until a human deletes a line.

Why this exists
---------------
`alembic revision --autogenerate` writes a *draft*, not a correct migration. It
diffs models against the live schema and has three blind spots that cost data:

1. **Renames are invisible.** Renaming `posts.foo` to `posts.bar` looks exactly
   like "drop foo, add bar" -- autogenerate emits `drop_column` + `add_column`
   and every value in the column is gone. The correct migration is a one-line
   `op.alter_column(..., new_column_name=...)` that a human has to write.
2. **Type changes are guesses.** A narrowing type change generates a cast that
   may truncate or fail on real rows (cf. the 768 -> 1024 vector bump, which
   had to null the column deliberately).
3. **Data never migrates itself.** Backfills, defaults for existing rows, and
   splitting/merging columns are invisible to a schema diff.

The mechanism
-------------
Every generated revision carries a module-level `UNREVIEWED = True` line, and
`upgrade` refuses to run while any revision still has one. Approving a migration
means opening the file and deleting that line by hand -- a deliberate edit, never
something the tooling does for you.

To keep that deletion from becoming a reflex, `new` writes the *specific*
hazards it found into comments sitting directly above the marker, so the
sentence "this drops a column and all of its data" is physically between you and
the line you came to delete.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

MARKER = "UNREVIEWED"
MARKER_LINE = f"{MARKER} = True"
FINDINGS_SENTINEL = "# --- automated analysis of this draft"

# Operations that can destroy rows. Messages stay ASCII: they are printed to a
# Windows console, which mangles anything else.
DESTRUCTIVE_OPS = {
    "drop_table": "drops a table and every row in it",
    "drop_column": "drops a column and all of its data",
    "drop_schema": "drops a schema and everything in it",
    "rename_table": "renames a table -- safe in itself, but verify nothing else "
    "still references the old name",
}

# Worth seeing, not alarming: no rows are lost.
BENIGN_OPS = {
    "drop_index": "drops an index (rebuildable -- no data loss)",
    "drop_constraint": "drops a constraint (no data loss, but weakens integrity)",
}

# Raw SQL inside op.execute() that changes or removes existing data.
SQL_DESTRUCTIVE = re.compile(
    r"\b(?:DROP\s+(?:TABLE|COLUMN|SCHEMA|DATABASE|TYPE)|TRUNCATE|DELETE\s+FROM"
    r"|UPDATE\s+\w+\s+SET|ALTER\s+COLUMN\s+\w+\s+TYPE)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    lineno: int
    severe: bool

    def render(self) -> str:
        return f"  [{'DANGER' if self.severe else 'note  '}] line {self.lineno}: {self.message}"


def normalized_source(path: Path) -> str:
    """File text with line endings normalized -- the repo is edited on Windows and
    parsed inside a Linux container."""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def is_unreviewed(path: Path) -> bool:
    """True while the script still carries its `UNREVIEWED = True` marker.

    Parsed rather than grepped, so the marker mentioned in a docstring or
    comment (as in this module) does not count -- only a real assignment does.
    """
    for node in ast.parse(normalized_source(path)).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == MARKER for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            return True
    return False


def _string_arg(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _upgrade_body(tree: ast.Module) -> ast.AST | None:
    """The `upgrade()` function, which is the only part that runs on a normal
    apply. `downgrade()` is deliberately not analyzed: rolling back is inherently
    destructive, so flagging it would fire on every migration ever written and
    train the reader to ignore the warnings that matter."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    return None


def _op_calls(tree: ast.AST):
    """Yield (operation_name, call_node) for every `op.<something>(...)` call."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        ):
            yield node.func.attr, node


def analyze(path: Path) -> list[Finding]:
    """Static read of a migration, surfacing everything that can cost data.

    Deliberately syntactic: it reports what the script *says*, so it cannot be
    fooled by anything short of dynamically constructed SQL -- and a migration
    that builds its DDL dynamically deserves a hard look anyway.
    """
    tree = ast.parse(normalized_source(path), filename=str(path))
    findings: list[Finding] = []
    dropped_columns: dict[str, list[str]] = {}
    added_columns: dict[str, int] = {}

    upgrade = _upgrade_body(tree)
    if upgrade is None:
        return [Finding("no_upgrade", "no upgrade() function found", 1, True)]

    for name, call in _op_calls(upgrade):
        if name in DESTRUCTIVE_OPS:
            target = ", ".join(a for a in (_string_arg(x) for x in call.args[:2]) if a)
            findings.append(
                Finding(name, f"op.{name}({target}) -- {DESTRUCTIVE_OPS[name]}", call.lineno, True)
            )
        elif name in BENIGN_OPS:
            findings.append(
                Finding(name, f"op.{name}(...) -- {BENIGN_OPS[name]}", call.lineno, False)
            )

        if name == "drop_column" and len(call.args) >= 2:
            table, column = _string_arg(call.args[0]), _string_arg(call.args[1])
            if table and column:
                dropped_columns.setdefault(table, []).append(column)
        elif name == "add_column" and call.args:
            if table := _string_arg(call.args[0]):
                added_columns[table] = call.lineno
        elif name == "alter_column":
            if any(kw.arg == "type_" for kw in call.keywords):
                findings.append(
                    Finding(
                        "type_change",
                        "op.alter_column(type_=...) -- the cast runs against existing rows "
                        "and can truncate or fail; confirm it is lossless",
                        call.lineno,
                        True,
                    )
                )
            if any(kw.arg == "new_column_name" for kw in call.keywords):
                findings.append(
                    Finding(
                        "rename",
                        "op.alter_column(new_column_name=...) -- a real rename, preserves "
                        "data (this is the correct form)",
                        call.lineno,
                        False,
                    )
                )
        elif name == "execute" and call.args:
            if match := SQL_DESTRUCTIVE.search(_string_arg(call.args[0]) or ""):
                findings.append(
                    Finding(
                        "raw_sql",
                        f"op.execute(...) contains {match.group(0).upper()} -- raw SQL that "
                        "changes or removes existing data",
                        call.lineno,
                        True,
                    )
                )

    # The failure this gate exists for: autogenerate cannot see a rename, so it
    # emits drop+add on one table and the data is silently lost.
    for table, columns in dropped_columns.items():
        if table in added_columns:
            findings.append(
                Finding(
                    "possible_rename",
                    f"table {table!r}: drops {', '.join(columns)} AND adds a column. If this "
                    "is a RENAME, autogenerate got it wrong and the data will be lost -- "
                    "replace both with op.alter_column(new_column_name=...)",
                    added_columns[table],
                    True,
                )
            )
    return sorted(findings, key=lambda f: (f.lineno, f.code))


def annotate(path: Path) -> list[Finding]:
    """Write this draft's hazards into comments directly above its `UNREVIEWED`
    marker, so approving it means reading past them. Returns the findings."""
    findings = analyze(path)
    source = normalized_source(path)
    if not findings or FINDINGS_SENTINEL in source:
        return findings

    block = [
        FINDINGS_SENTINEL + " (regenerate with `new`, never hand-maintained) ---",
        "#",
        *(f"#   {f.render().strip()}" for f in findings),
        "#",
    ]
    if any(f.severe for f in findings):
        block += [
            "# Read every DANGER line above against the real schema before deleting",
            "# the marker below. Autogenerate cannot see renames: if a drop+add pair",
            "# is really one column being renamed, fix it here or lose the data.",
            "#",
        ]
    lines = source.splitlines()
    index = next(i for i, line in enumerate(lines) if line.strip() == MARKER_LINE)
    lines[index:index] = block
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return findings


def version_scripts(migrations_dir: Path) -> list[Path]:
    return sorted(p for p in (migrations_dir / "versions").glob("*.py") if p.stem != "__init__")


def verify(migrations_dir: Path) -> list[str]:
    """Problems that must block an upgrade. Empty list means safe to apply.

    Every script is checked rather than only the pending ones: "which revisions
    are pending" is not knowable without a database connection, and an
    already-applied migration that still claims to be unreviewed is worth
    stopping for.
    """
    return [
        f"{script.name}: still marked UNREVIEWED -- read it, correct it, then delete "
        f"the `{MARKER_LINE}` line"
        for script in version_scripts(migrations_dir)
        if is_unreviewed(script)
    ]

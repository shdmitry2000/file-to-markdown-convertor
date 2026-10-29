"""Fail the image build when a platform module this service imports was not vendored.

    python -m app.shared_check

The shared/ libs are copied in by a hand-kept list (enterprise_sync.yaml in the main
repo). Most of their imports are lazy — inside the function that needs them — so an
image missing one builds, passes /health, and only fails when that function runs:
1.4.8.2 shipped to enterprise without shared/space_llm.py and every VLM call made with
the space's connector answered 503 ("No module named 'shared.space_llm'").

This reads every `shared.*` import under app/ (lazy ones included) and imports it now.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

# Reached only through a guarded import that falls back when it is absent (the
# connector store: standalone, this image uses the global connector credentials).
OPTIONAL = ("shared.db",)


def wanted(app_dir: Path) -> dict[str, set[str]]:
    """{module: names imported from it} for every `shared.*` import under `app_dir`."""
    out: dict[str, set[str]] = {}
    for path in app_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "shared" or node.module.startswith("shared."):
                    out.setdefault(node.module, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("shared."):
                        out.setdefault(a.name, set())
    return out


def missing(app_dir: Path) -> list[str]:
    errors = []
    for module, names in sorted(wanted(app_dir).items()):
        if module.startswith(OPTIONAL):
            continue
        try:
            mod = importlib.import_module(module)
            for name in sorted(names):
                # `from shared import secret_box` names a submodule, not an attribute.
                if not hasattr(mod, name):
                    importlib.import_module(f"{module}.{name}")
        except ImportError as exc:
            errors.append(f"{module}: {exc}")
    return errors


if __name__ == "__main__":
    errors = missing(Path(__file__).resolve().parent)
    for e in errors:
        print(f"ERROR: shared lib not importable — {e}", file=sys.stderr)
    if errors:
        print("Vendor it: add it to the markdown module's shared_include in "
              "enterprise_git_scripts/enterprise_sync.yaml (make vendor-markdown-shared "
              "reads the same list).", file=sys.stderr)
        sys.exit(1)
    print("shared libs: every import resolves")

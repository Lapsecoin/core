#!/usr/bin/env python3
"""Pick the test files a change can affect, from the import graph.

    python scripts/select_tests.py --base origin/main      # files changed since
    python scripts/select_tests.py src/discovery.py        # explicit files

Prints one test file per line. Prints "ALL" when it cannot prove a smaller
set is safe, and nothing when no test can be affected (docs only).

It errs toward running too much, never too little:

  * A test depends on every module it imports, transitively, including
    imports inside functions (the whole AST is walked, not just the top).
  * A test also depends on any repo path or root script it names in a
    string, which covers subprocess runs, spec_from_file_location and
    tests that read templates or docs.
  * Changes to test infrastructure, dependencies, packaging or the CI
    config, and any file the graph does not know about, select ALL.

The release workflow still runs the whole suite. This is for the faster
check on every push, not a replacement for the release gate.
"""

import argparse
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Anything here can change how every test behaves.
RUN_ALL = {
    "conftest.py", "tests/conftest.py", "pytest.ini", "pyproject.toml",
    "requirements.txt", "VERSION", "hook_oqs.py", "Makefile",
    "scripts/select_tests.py",
}
RUN_ALL_PREFIXES = (".github/", "tests/__init__", "vendor/")

# Changes under these never reach a test unless a test names the path.
INERT_SUFFIXES = (".md", ".svg", ".png", ".ico")
INERT_PREFIXES = ("docs/", "scripts/")

# Tests that boot the whole app or run full flows. A change to any code
# also runs these, since an import graph cannot see every runtime coupling.
ALWAYS = ("tests/test_main_smoke.py", "tests/test_e2e_flows.py",
          "tests/test_integration_block_tx_state.py")

# Directories a test can read from without importing anything.
DATA_ROOTS = ("templates_html", "docs", "vendor", "scripts", "src")


def py_files():
    out = {}
    for base in ("src", "tests", "."):
        d = os.path.join(ROOT, base)
        for name in os.listdir(d):
            if name.endswith(".py"):
                rel = os.path.normpath(os.path.join(base, name)).replace(os.sep, "/")
                out[os.path.splitext(name)[0]] = out.get(os.path.splitext(name)[0], rel)
    return out


def local_imports(path, modules):
    """Names of repo modules imported anywhere in this file."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        source = f.read()
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [
                (node.module + "." if node.module else "") + a.name for a in node.names]
        else:
            continue
        for n in names:
            for part in (n, n.split(".")[0], n.split(".")[-1]):
                if part in modules:
                    found.add(part)
    return found, source


def build(modules):
    deps, sources = {}, {}
    for name, path in modules.items():
        deps[name], sources[name] = local_imports(path, modules)
    return deps, sources


def closure(name, deps):
    seen, stack = set(), [name]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(deps.get(cur, ()))
    return seen


def select(changed):
    modules = py_files()
    deps, sources = build(modules)
    path_to_mod = {p: m for m, p in modules.items()}
    tests = {m: p for m, p in modules.items() if p.startswith("tests/test_")}

    hit_modules, hit_paths = set(), []
    for rel in changed:
        rel = rel.strip().replace(os.sep, "/")
        if not rel:
            continue
        if rel in RUN_ALL or rel.startswith(RUN_ALL_PREFIXES):
            return None
        if rel in path_to_mod:
            hit_modules.add(path_to_mod[rel])
        elif rel.endswith(INERT_SUFFIXES) or rel.startswith(INERT_PREFIXES):
            hit_paths.append(rel)
        elif rel.startswith(DATA_ROOTS) or "/" not in rel:
            hit_paths.append(rel)
        else:
            return None  # a file the graph knows nothing about

    chosen = set()
    for tname, tpath in tests.items():
        reach = closure(tname, deps)
        if tpath in changed or reach & hit_modules:
            chosen.add(tpath)
            continue
        # Paths named in strings, anywhere in the test or what it imports.
        text = "\n".join(sources[m] for m in reach)
        for rel in hit_paths:
            if rel in text or os.path.basename(rel) in text:
                chosen.add(tpath)
                break
        else:
            # A root script or module run by path or in a subprocess is
            # named in the test's own source, not in what it imports.
            own = sources[tname]
            if any(modules[m] in own or (m + ".py") in own for m in hit_modules):
                chosen.add(tpath)
    if chosen or hit_modules:
        chosen.update(t for t in ALWAYS if t in tests.values())
    return sorted(chosen)


def changed_files(base):
    for args in (["git", "diff", "--name-only", f"{base}...HEAD"],
                 ["git", "diff", "--name-only", base]):
        r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            files = r.stdout.split()
            r2 = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                cwd=ROOT, capture_output=True, text=True)
            return files + r2.stdout.split()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--base", help="git ref to diff against")
    args = ap.parse_args()
    files = args.files
    if args.base:
        files = changed_files(args.base)
        if files is None:
            print("ALL")
            return 0
    result = select(files)
    if result is None:
        print("ALL")
    else:
        print("\n".join(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

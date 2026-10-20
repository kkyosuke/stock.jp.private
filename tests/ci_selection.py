"""Conservatively select regressions from both sides of a complete Git diff.

Imports, module-name strings (including CLI tables) and source-file strings
form the dependency graph. Non-Python inputs and shared entry points require
all ordinary regressions. Weekly/manual CI remains the exhaustive backstop.
"""
from __future__ import annotations

import ast
from collections import defaultdict
import io
import re
import subprocess
import tarfile

from stock_jp.automation.git import git

BASELINE = frozenset({
    "tests.test_ci", "tests.test_ci_selection", "tests.test_ci_suite",
    "tests.test_daily_stock_price_workflow", "tests.test_guardrail_ownership",
    "tests.test_operation_policy", "tests.test_public_price_guardrail",
    "tests.test_single_repository", "tests.test_validate_checkout",
})
SHARED = frozenset({
    "src/stock_jp/cli.py", "src/stock_jp/__main__.py", "src/stock_jp/workspace.py",
    "src/stock_jp/automation/ci.py", "src/stock_jp/automation/git.py",
    "tests/ci_suite.py", "tests/ci_selection.py",
})


def module_name(path):
    if not path.endswith(".py") or not path.startswith(("src/stock_jp/", "tests/")):
        return None
    name = path.removeprefix("src/")[:-3].replace("/", ".")
    return name.removesuffix(".__init__")


def is_document(path):
    return path == "README.md" or (path.startswith("docs/") and path.endswith(".md")
                                   and not path.startswith("docs/rules/"))


def broad_input(paths):
    return next((path for path in paths if not is_document(path)
                 and (module_name(path) is None or path in SHARED or path.endswith("/__init__.py"))), None)


def snapshot(root, commit):
    """Read committed Python blobs only, without extracting or following links."""
    names = git(root, "ls-tree", "-r", "--name-only", "-z", commit, "--", "src/stock_jp", "tests")
    expected = {p.decode() for p in names.split(b"\0") if p.endswith(b".py")}
    data = git(root, "archive", "--format=tar", commit, "src/stock_jp", "tests")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            if member.name.endswith(".py"):
                if not member.isfile() or member.name not in expected:
                    raise ValueError("unsafe Python snapshot member")
                files[member.name] = archive.extractfile(member).read()
    if files.keys() != expected:
        raise ValueError("incomplete Python snapshot")
    return files


def dependencies(files, known, dynamic):
    graph = defaultdict(set)
    for path, source in files.items():
        name = module_name(path)
        if name is None:
            raise ValueError("unmapped Python path")
        package = name if path.endswith("/__init__.py") else name.rpartition(".")[0]

        def add(reference):
            # Attribute references also depend on the module owning them.
            while reference:
                if reference in known:
                    graph[name].add(reference)
                reference = reference.rpartition(".")[0]

        add(package)
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) \
                    in {"import_module", "spec_from_file_location", "__import__", "exec", "eval"}:
                # Runtime loaders may hide dependencies from AST analysis.
                # Always include their consumers for any source change.
                dynamic.add(name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parts = package.split(".")
                    if node.level > len(parts):
                        raise ValueError("relative import escapes package")
                    prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
                add(prefix)
                for alias in node.names:
                    if alias.name == "*":
                        graph[name].update(m for m in known if m.startswith(prefix + "."))
                    else:
                        add(prefix + "." + alias.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Covers mock targets, importlib, subprocess -m, CLI tables,
                # and spec_from_file_location paths used by health/recovery.
                value = node.value.replace("src/stock_jp/", "stock_jp/").replace("/", ".")
                for reference in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value):
                    add(reference)
                    add("stock_jp." + reference)
                if node.value == "stock_jp":
                    add("stock_jp.__main__")
        graph[name].discard(name)
    return graph


def affected_modules(before, after, paths, test_modules):
    """Return None whenever the diff cannot safely narrow the ordinary suite."""
    modules = {module_name(path) for path in before.keys() | after.keys()}
    if None in modules:
        return None, "unmapped Python snapshot"
    broad = broad_input(paths)
    if broad:
        return None, "shared or non-Python input: " + broad
    changed = set()
    for path in paths:
        if is_document(path):
            continue
        name = module_name(path)
        if name not in modules:
            return None, "unmapped Python input: " + path
        changed.add(name)
    if not changed:
        return None, "no mapped Python changes"
    dynamic = set()
    graph = dependencies(before, modules, dynamic)
    for module, refs in dependencies(after, modules, dynamic).items():
        graph[module].update(refs)
    reverse = defaultdict(set)
    for module, refs in graph.items():
        for ref in refs:
            reverse[ref].add(module)
    def consumers(module):
        affected, pending = set(), [module]
        while pending:
            current = pending.pop()
            if current not in affected:
                affected.add(current)
                pending.extend(reverse[current] - affected)
        return affected

    selected, source_affected = set(), False
    for module in changed:
        affected = consumers(module)
        covered = affected & test_modules
        if not covered:
            return None, "no dependent test for: " + module
        selected.update(covered)
        source_affected |= any(name.startswith("stock_jp.") for name in affected)
    # Dynamic consumers widen the suite but cannot establish that an otherwise
    # untested source file has coverage. That case already fell back above.
    if source_affected:
        for module in dynamic:
            selected.update(consumers(module) & test_modules)
    return selected | BASELINE, "dependencies in base and head"


def plan(root, base, head, test_modules):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") and sha != "0" * 40 for sha in (base, head)):
        return None, "no comparison base/head"
    try:
        if git(root, "merge-base", base, head).decode().strip() != base:
            return None, "base is not an ancestor"
        # Never narrow a local checkout that differs from the analysed commit.
        dirty = git(root, "diff", "--name-only", head)
        untracked = git(root, "ls-files", "--others", "--exclude-standard")
        if dirty or untracked:
            return None, "uncommitted checkout"
        paths = [p.decode() for p in git(root, "diff", "--no-renames", "--name-only", "-z", base, head).split(b"\0") if p]
        if not paths:
            return None, "empty diff"
        broad = broad_input(paths)
        if broad:
            return None, "shared or non-Python input: " + broad
        return affected_modules(snapshot(root, base), snapshot(root, head), paths, test_modules)
    except (OSError, subprocess.CalledProcessError, UnicodeError, SyntaxError, ValueError, tarfile.TarError):
        return None, "dependency comparison unavailable"

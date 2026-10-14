"""The test selector decides which tests a release runs, so it is held to the
rule it states: it may run too much, never too little. Each case builds a tiny
repository and asks what a change to one file selects."""
import importlib.util
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "select_tests.py")


def load():
    spec = importlib.util.spec_from_file_location("select_tests_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FILES = {
    "src/a.py": "import b\n",
    "src/b.py": "x = 1\n",
    "src/c.py": "y = 2\n",
    "src/lazy.py": "def f():\n    import b\n",
    "src/web.py": "def page(render_template):\n    return render_template('page.html')\n",
    "src/data.txt": "words\n",
    "src/reads.py": "WORDS = open('data.txt').read()\n",
    "src/patched.py": "z = 3\n",
    "src/dyn.py": "w = 4\n",
    "main.py": "print('x')\n",
    "templates_html/base.html": "<html>{% block c %}{% endblock %}</html>\n",
    "templates_html/page.html": '{% extends "base.html" %}{% include "bit.html" %}\n',
    "templates_html/bit.html": "<b>bit</b>\n",
    "templates_html/solo.html": "<i>solo</i>\n",
    "templates_html/css/pages/page.css": "a{}\n",
    "docs/guide.md": "words\n",
    "scripts/tool.sh": "echo\n",
    "scripts/other.py": "print()\n",
    "VERSION": "1.0.0\n",
    "tests/conftest.py": "",
    "tests/__init__.py": "",
    "tests/helpers.py": "import c\n",
    "tests/test_a.py": "import a\n",
    "tests/test_b.py": "import b\n",
    "tests/test_c.py": "import helpers\n",
    "tests/test_lazy.py": "import lazy\n",
    "tests/test_web.py": "import web\n",
    "tests/test_reads.py": "import reads\n",
    "tests/test_patch.py": (
        "from unittest import mock\n"
        "def test_x():\n    mock.patch('patched.thing')\n"),
    "tests/test_dynamic.py": (
        "import importlib\n"
        "def test_x():\n    importlib.import_module('dyn')\n"),
    "tests/test_scan.py": (
        "import glob\n"
        "def test_x():\n    glob.glob('templates_html/*.html')\n"),
    "tests/test_subproc.py": (
        "import subprocess\n"
        "def test_x():\n    subprocess.run(['python', 'main.py'])\n"),
    "tests/test_script.py": (
        "def test_x():\n    open('scripts/tool.sh').read()\n"),
    "tests/test_smoke_flow.py": "import a\n",
}


@pytest.fixture
def sel(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    mod = load()
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "ALWAYS", ("tests/test_smoke_flow.py",))

    def run(*changed):
        out = mod.select(list(changed))
        return None if out is None else {p[len("tests/test_"):-3] for p in out}
    run.mod, run.root = mod, tmp_path
    return run


class TestImportGraph:
    def test_a_change_selects_the_tests_that_import_it(self, sel):
        assert {"b", "a"} <= sel("src/b.py")

    def test_it_follows_imports_through_other_modules(self, sel):
        # test_a imports a, which imports b
        assert "a" in sel("src/b.py")

    def test_it_follows_imports_made_inside_functions(self, sel):
        assert "lazy" in sel("src/b.py")

    def test_it_follows_imports_through_test_helpers(self, sel):
        assert "c" in sel("src/c.py")

    def test_unrelated_tests_are_left_out(self, sel):
        out = sel("src/c.py")
        assert "b" not in out and "web" not in out and "patch" not in out

    def test_the_always_run_tests_ride_along_with_any_code_change(self, sel):
        assert "smoke_flow" in sel("src/c.py")


class TestNamesInStrings:
    def test_a_dotted_patch_target_is_a_dependency(self, sel):
        assert "patch" in sel("src/patched.py")

    def test_a_dynamic_import_is_a_dependency(self, sel):
        assert "dynamic" in sel("src/dyn.py")

    def test_a_root_script_run_by_path_selects_the_test_that_runs_it(self, sel):
        assert "subproc" in sel("main.py")

    def test_a_script_a_test_names_selects_that_test(self, sel):
        assert "script" in sel("scripts/tool.sh")

    def test_a_data_file_a_module_reads_selects_what_imports_that_module(self, sel):
        assert "reads" in sel("src/data.txt")


class TestTemplates:
    def test_a_page_selects_what_renders_it(self, sel):
        assert "web" in sel("templates_html/page.html")

    def test_a_base_template_reaches_the_pages_that_extend_it(self, sel):
        assert "web" in sel("templates_html/base.html")

    def test_an_included_partial_reaches_the_pages_that_include_it(self, sel):
        assert "web" in sel("templates_html/bit.html")

    def test_a_template_nothing_names_selects_no_renderer(self, sel):
        assert "web" not in (sel("templates_html/solo.html") or set())

    def test_a_test_that_scans_the_folder_depends_on_every_file_in_it(self, sel):
        assert "scan" in sel("templates_html/solo.html")

    def test_css_nobody_names_selects_only_the_folder_scanners(self, sel):
        # test_scan globs the folder; test_smoke_flow always rides along
        assert sel("templates_html/css/pages/page.css") == {"scan", "smoke_flow"}


class TestRunEverything:
    @pytest.mark.parametrize("path", [
        "tests/conftest.py", "pyproject.toml", "requirements.txt",
        "scripts/select_tests.py", "tests/__init__.py", "vendor/lib.js"])
    def test_files_that_change_every_test(self, sel, path):
        assert sel(path) is None

    def test_a_file_the_graph_knows_nothing_about(self, sel):
        assert sel("tests/data/fixture.json") is None

    def test_a_deleted_module_runs_everything(self, sel):
        (sel.root / "src" / "b.py").unlink()
        assert sel("src/b.py") is None

    def test_a_moved_module_runs_everything(self, sel):
        # git lists the old path of a move (the selector asks for no renames)
        (sel.root / "src" / "b.py").rename(sel.root / "src" / "b2.py")
        assert sel("src/b.py", "src/b2.py") is None

    def test_a_deleted_test_selects_nothing_by_itself(self, sel):
        (sel.root / "tests" / "test_b.py").unlink()
        assert sel("tests/test_b.py") == set()


class TestQuietChanges:
    def test_docs_select_only_what_names_them(self, sel):
        assert sel("docs/guide.md") == set()

    def test_no_change_selects_nothing(self, sel):
        assert sel() == set()

    def test_the_version_file_selects_only_what_names_it(self, sel):
        assert sel("VERSION") == set()

    def test_a_new_test_runs_itself(self, sel):
        path = sel.root / "tests" / "test_new.py"
        path.write_text("import c\n")
        assert "new" in sel("tests/test_new.py")


class TestTheRealRepository:
    """The selector against this repository, so a rename of a file it relies
    on is caught here and not on release day."""

    def test_the_always_run_tests_exist(self):
        mod = load()
        for rel in mod.ALWAYS:
            assert os.path.exists(os.path.join(mod.ROOT, rel)), rel

    def test_a_change_to_a_module_selects_its_own_test(self):
        mod = load()
        assert "tests/test_node.py" in mod.select(["src/node.py"])
        assert "tests/test_tx.py" in mod.select(["src/tx.py"])

    def test_base_template_selects_the_page_tests(self):
        mod = load()
        assert "tests/test_api.py" in mod.select(["templates_html/base.html"])

    def test_every_test_file_is_known_to_the_graph(self):
        mod = load()
        known = set(mod.py_files().values())
        on_disk = {f"tests/{n}" for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py")}
        assert on_disk <= known

    def test_the_command_line_prints_ALL_for_a_file_it_cannot_place(self):
        out = subprocess.run([sys.executable, SCRIPT, "tests/data/unknown.json"],
                             capture_output=True, text=True, check=True).stdout
        assert out.strip() == "ALL"

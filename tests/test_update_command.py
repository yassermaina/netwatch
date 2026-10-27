"""The netwatch-update script: argument handling and version detection (installs nothing)."""

import shutil
import subprocess
from pathlib import Path

from netwatch import __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "netwatch-update"


def run(*args, cwd=None):
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd, timeout=60)


def test_check_reads_version_from_folder():
    res = run("--check", str(ROOT))
    assert res.returncode == 0, res.stderr
    assert f"New version:       {__version__}" in res.stdout


def test_check_reads_version_from_release_zip(tmp_path):
    zip_path = tmp_path / "netwatch.zip"
    tree = tmp_path / "tree"
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns("build", ".git", "__pycache__", "*.zip"))
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", tmp_path, "tree")
    res = run("--check", str(zip_path))
    assert res.returncode == 0, res.stderr
    assert __version__ in res.stdout


def test_rejects_bad_input(tmp_path):
    assert run("--method", "snap").returncode != 0
    res = run("--check", str(tmp_path))
    assert res.returncode != 0 and "does not look like a NetWatch source folder" in res.stderr
    assert run("--help").returncode == 0

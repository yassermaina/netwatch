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


def test_user_prefix_update_needs_no_sudo_and_installs_under_the_prefix(tmp_path):
    """`--method make --prefix DIR` installs into DIR as a plain user; sudo and a running NetWatch are never touched."""
    import os
    import sys

    import pytest

    if subprocess.run([sys.executable, "-c", "import pip, setuptools"], capture_output=True).returncode != 0:
        pytest.skip("needs pip and setuptools for the project's own make install")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "sudo").write_text("#!/bin/sh\necho 'sudo must not be used for a user prefix' >&2\nexit 99\n")
    (stubs / "pgrep").write_text("#!/bin/sh\nexit 1\n")  # pretend no NetWatch is running, so none is stopped
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    prefix = tmp_path / "prefix"
    # PYTHONNOUSERSITE: pip must not see (and uninstall) a NetWatch the developer installed in ~/.local.
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", PYTHON=sys.executable, PYTHONNOUSERSITE="1")
    res = subprocess.run(
        ["sh", str(SCRIPT), "--yes", "--force", "--no-restart", "--method", "make", "--prefix", str(prefix), str(ROOT)],
        capture_output=True, text=True, timeout=180, env=env, cwd=tmp_path,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (prefix / "bin" / "netwatch-update").is_file()
    assert (prefix / "share" / "applications" / "io.github.netwatch.NetWatch.desktop").is_file()
    assert list(prefix.glob("lib/python*/site-packages/netwatch/__init__.py"))
    assert f"NetWatch {__version__} is installed" in res.stdout
    assert "sudo must not be used" not in res.stderr

#!/usr/bin/env python3
"""Exercise deployment Git updates using temporary local repositories only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "git-update.sh"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "CTS-G test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
       "GIT_COMMITTER_NAME": "CTS-G test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def run(args, *, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, env=ENV, check=check, capture_output=True, text=True, timeout=30)


def git(repo, *args):
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def commit(repo, value):
    (repo / "tracked.txt").write_text(value, encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-q", "-m", value.strip())


def update(repo, branch="main", force=False):
    return run(["bash", "-c", 'source "$1"; cts_git_update "$2" origin "$3" "$4"',
                "--", str(HELPER), str(repo), branch, "1" if force else "0"], check=False)


def main():
    with tempfile.TemporaryDirectory(prefix="ctsg-safe-update-") as tmp:
        base = Path(tmp)
        source, work, dirty, switch = (base / name for name in ("source", "work", "dirty", "switch"))
        run(["git", "init", "-q", "-b", "main", str(source)])
        commit(source, "base\n")
        run(["git", "clone", "-q", str(source), str(work)])
        commit(source, "remote-one\n")
        git(work, "fetch", "-q", "origin")
        assert update(work).returncode == 0
        assert git(work, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
        assert (work / "tracked.txt").read_text(encoding="utf-8") == "remote-one\n"

        commit(work, "local-commit\n")
        local_head = git(work, "rev-parse", "HEAD")
        commit(source, "remote-two\n")
        git(work, "fetch", "-q", "origin")
        assert update(work).returncode != 0
        assert git(work, "rev-parse", "HEAD") == local_head
        assert (work / "tracked.txt").read_text(encoding="utf-8") == "local-commit\n"

        run(["git", "clone", "-q", str(source), str(dirty)])
        dirty_head = git(dirty, "rev-parse", "HEAD")
        (dirty / "tracked.txt").write_text("local-dirty\n", encoding="utf-8")
        commit(source, "remote-three\n")
        git(dirty, "fetch", "-q", "origin")
        assert update(dirty).returncode != 0
        assert git(dirty, "rev-parse", "HEAD") == dirty_head
        assert (dirty / "tracked.txt").read_text(encoding="utf-8") == "local-dirty\n"

        run(["git", "clone", "-q", str(source), str(switch)])
        git(switch, "checkout", "-q", "-b", "other")
        commit(switch, "other-branch\n")
        (switch / "tracked.txt").write_text("unsaved-other\n", encoding="utf-8")
        assert update(switch, branch="main").returncode != 0
        assert git(switch, "branch", "--show-current") == "other"
        assert (switch / "tracked.txt").read_text(encoding="utf-8") == "unsaved-other\n"

        # Explicit --force is tested only inside this disposable repository.
        assert update(dirty, force=True).returncode == 0
        assert git(dirty, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
        assert (dirty / "tracked.txt").read_text(encoding="utf-8") == "remote-three\n"
    print("safe Git update: fast-forward, divergence, dirty checkout and explicit force passed")


if __name__ == "__main__":
    main()

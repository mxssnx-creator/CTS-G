#!/usr/bin/env python3
"""Exercise the remote installer through a local fake SSH, never against a real host."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "remote-install.sh"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "CTS-G test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
       "GIT_COMMITTER_NAME": "CTS-G test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def run(args, *, env=ENV, check=True):
    return subprocess.run(args, env=env, check=check, capture_output=True, text=True, timeout=30)


def git(repo, *args):
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def commit(repo, value):
    (repo / "tracked.txt").write_text(value, encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-q", "-m", value.strip())


def main():
    with tempfile.TemporaryDirectory(prefix="ctsg-remote-safe-") as tmp:
        base = Path(tmp)
        source, work, diverged, fakebin = (base / name for name in ("source repo", "work repo", "diverged repo", "bin"))
        fakebin.mkdir()
        ssh = fakebin / "ssh"
        ssh.write_text('#!/usr/bin/env bash\nfor last; do :; done\nexec /bin/bash -c "$last"\n', encoding="utf-8")
        ssh.chmod(0o755)
        run(["git", "init", "-q", "-b", "main", str(source)])
        (source / "deploy").mkdir()
        stub = source / "deploy" / "install-linux.sh"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CTSG_INSTALL_LOG"\n', encoding="utf-8")
        stub.chmod(0o755)
        (source / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(source, "add", "tracked.txt", "deploy/install-linux.sh")
        git(source, "commit", "-q", "-m", "base")
        run(["git", "clone", "-q", str(source), str(work)])
        commit(source, "remote-one\n")

        def install(repo, log):
            env = dict(ENV, PATH=f"{fakebin}{os.pathsep}{ENV['PATH']}", CTS_G_ROOT=str(repo), REPO_URL=str(source),
                       CTSG_INSTALL_LOG=str(log))
            return run(["bash", str(INSTALLER), "--branch", "main", "--start-live", "--no-vst"], env=env, check=False)

        success_log = base / "success.log"
        first = install(work, success_log)
        assert first.returncode == 0, first.stderr
        assert git(work, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
        flags = success_log.read_text(encoding="utf-8").splitlines()
        assert "--start-live" in flags and "--no-vst" in flags

        dirty_head = git(work, "rev-parse", "HEAD")
        (work / "tracked.txt").write_text("local-dirty\n", encoding="utf-8")
        commit(source, "remote-two\n")
        dirty_log = base / "dirty.log"
        dirty = install(work, dirty_log)
        assert dirty.returncode != 0 and not dirty_log.exists()
        assert git(work, "rev-parse", "HEAD") == dirty_head
        assert (work / "tracked.txt").read_text(encoding="utf-8") == "local-dirty\n"

        run(["git", "clone", "-q", str(source), str(diverged)])
        commit(diverged, "local-commit\n")
        diverged_head = git(diverged, "rev-parse", "HEAD")
        commit(source, "remote-three\n")
        diverged_log = base / "diverged.log"
        result = install(diverged, diverged_log)
        assert result.returncode != 0 and not diverged_log.exists()
        assert git(diverged, "rev-parse", "HEAD") == diverged_head
        assert (diverged / "tracked.txt").read_text(encoding="utf-8") == "local-commit\n"
    print("remote installer: fake SSH, spaced paths, fast-forward, flag forwarding and dirty/diverged preservation passed")


if __name__ == "__main__":
    main()

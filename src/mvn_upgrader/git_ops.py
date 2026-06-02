"""Git operations: branch / checkpoint / commit / restore / push.

All commands go through the safe subprocess wrapper. ``git`` is our backup, so
applies run with ``-DgenerateBackupPoms=false`` and a failed item is reset hard
to its checkpoint to wipe both POM edits and any Codex code changes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import proc

log = logging.getLogger("mvn_upgrader.git_ops")


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path | str, runner=proc.run) -> None:
        self.repo = Path(repo)
        self._run = runner

    def _git(self, *args: str, check: bool = False) -> proc.ProcResult:
        return self._run(["git", *args], cwd=self.repo, check=check)

    # ---- queries ------------------------------------------------------------
    def is_repo(self) -> bool:
        return self._git("rev-parse", "--is-inside-work-tree").ok

    def is_clean(self) -> bool:
        res = self._git("status", "--porcelain")
        return res.ok and not res.stdout.strip()

    def current_branch(self) -> Optional[str]:
        res = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return res.stdout.strip() if res.ok else None

    def head_sha(self) -> Optional[str]:
        res = self._git("rev-parse", "HEAD")
        return res.stdout.strip() if res.ok else None

    def branch_exists(self, name: str) -> bool:
        return self._git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"
        ).ok

    def checkout(self, name: str) -> None:
        res = self._git("checkout", name)
        if not res.ok:
            raise GitError(f"could not checkout {name}: {res.combined.strip()}")

    def changed_files(self) -> list[str]:
        res = self._git("status", "--porcelain")
        files = []
        for line in res.stdout.splitlines():
            if len(line) > 3:
                files.append(line[3:].strip())
        return files

    def has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    # ---- mutations ----------------------------------------------------------
    def create_branch(self, name: str, base: Optional[str] = None) -> None:
        if base:
            res = self._git("checkout", "-B", name, base)
        else:
            res = self._git("checkout", "-B", name)
        if not res.ok:
            raise GitError(f"could not create branch {name}: {res.combined.strip()}")

    def checkpoint(self) -> str:
        """Return the current HEAD sha to use as a revert point for one item."""
        sha = self.head_sha()
        if not sha:
            raise GitError("could not determine HEAD for checkpoint")
        return sha

    def commit_all(self, message: str) -> Optional[str]:
        add = self._git("add", "-A")
        if not add.ok:
            raise GitError(f"git add failed: {add.combined.strip()}")
        commit = self._run(
            ["git", "commit", "-m", message], cwd=self.repo
        )
        if not commit.ok:
            # Nothing to commit is not fatal; surface as None.
            log.warning("git commit produced rc=%s: %s",
                        commit.returncode, commit.combined.strip())
            return None
        return self.head_sha()

    def commit_files(
        self, message: str, paths: list[str], force: bool = False
    ) -> Optional[str]:
        add_args = ["add"] + (["-f"] if force else []) + list(paths)
        add = self._git(*add_args)
        if not add.ok:
            raise GitError(f"git add failed: {add.combined.strip()}")
        commit = self._run(["git", "commit", "-m", message], cwd=self.repo)
        if not commit.ok:
            log.warning("git commit produced rc=%s: %s",
                        commit.returncode, commit.combined.strip())
            return None
        return self.head_sha()

    def restore_to(self, checkpoint_sha: str) -> None:
        """Hard-reset to a checkpoint and remove any untracked files/dirs."""
        reset = self._git("reset", "--hard", checkpoint_sha)
        if not reset.ok:
            raise GitError(f"git reset failed: {reset.combined.strip()}")
        self._git("clean", "-fd")

    def discard_worktree(self) -> None:
        """Discard uncommitted changes without moving HEAD."""
        self._git("checkout", "--", ".")
        self._git("clean", "-fd")

    def push(self, remote: str, branch: str, set_upstream: bool = True) -> proc.ProcResult:
        args = ["push"]
        if set_upstream:
            args += ["-u"]
        args += [remote, branch]
        res = self._git(*args)
        if not res.ok:
            raise GitError(f"git push failed: {res.combined.strip()}")
        return res

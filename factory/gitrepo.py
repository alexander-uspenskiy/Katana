"""Git for a run's workspace: work on a clone of the project's own repository, on a branch of its own.

With `git.mode` = auto (the default) and a project folder (or `git.repo`) that is already a git repository with
at least one commit, a run starts from a local clone of it on the branch katana/<run-id>, and when the run ends
Katana fetches that branch into the repository as a ref. Nothing in the repository changes except that new
branch: its working tree, HEAD and other branches are left alone. Otherwise (mode off, no repository, or no
commits yet) the run starts from a fresh `git init`, as before.

The clone has no `origin`, so nothing an agent runs in the workspace can reach the real repository. Katana does
no pushing; handing the branch back is a local fetch.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

MODES = ["auto", "off"]
EXCLUDE = "/.katana/"  # Katana's own folder, kept out of the project repository's `git status`
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def detect(project: Path, conf: dict) -> tuple:
    """(repository folder or None, why). `git.repo` names a repository (or a folder inside one); without it the
    project folder itself must be the repository's top level, so a project that happens to sit inside some
    larger repository is not picked up by accident."""
    g = conf.get("git") or {}
    if g.get("mode", "auto") == "off":
        return None, "git is off in Settings"
    explicit = str(g.get("repo") or "").strip()
    folder = Path(explicit).expanduser() if explicit else Path(project)
    if not folder.is_dir():
        return None, f"{folder} is not a folder"
    try:
        top = Path(git(folder, "rev-parse", "--show-toplevel")).resolve()
    except (RuntimeError, OSError):
        return None, f"{folder} is not a git repository"
    if not explicit and top != Path(project).resolve():
        return None, f"{folder} is inside the repository {top}, not its top level"
    if subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=top, capture_output=True, env=ENV).returncode:
        return None, f"{top} has no commits yet"
    return top, "ok"


def exclude_katana(repo: Path, project: Path) -> None:
    """When the project folder is inside the repository, keep .katana/ out of its status (a local, untracked rule)."""
    try:
        rel = Path(project).resolve().relative_to(repo)
    except ValueError:
        return
    rule = "/" + (str(rel) + "/" if str(rel) != "." else "") + ".katana/"
    path = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    try:
        text = path.read_text() if path.exists() else ""
        if rule not in text.splitlines():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + rule + "\n")
    except OSError:
        pass


def clone(repo: Path, project: Path, workspace: Path, run_id: str) -> dict:
    """Clones `repo` into `workspace` (which must not exist) and starts the branch katana/<run-id> from its HEAD."""
    exclude_katana(repo, project)
    dirty = bool(git(repo, "status", "--porcelain", "--untracked-files=no"))
    git(workspace.parent, "clone", "--local", "-q", str(repo), str(workspace))
    git(workspace, "remote", "remove", "origin")
    git(workspace, "config", "user.name", "Katana")
    git(workspace, "config", "user.email", "katana@localhost")
    branch = f"katana/{run_id}"
    git(workspace, "checkout", "-q", "-b", branch)
    return {"repo": str(repo), "branch": branch, "base": git(workspace, "rev-parse", "HEAD"), "dirty": dirty}


def hand_back(info: dict, workspace: Path) -> None:
    """Commits what the agents left uncommitted, then fetches the branch into the repository. Records the result
    in info: `commits` (new commits on the branch) and `handed_back`, or `error`."""
    try:
        if git(workspace, "status", "--porcelain"):
            git(workspace, "add", "-A")
            git(workspace, "commit", "-q", "-m", f"Katana: remaining changes of {info['branch']}")
        info["commits"] = int(git(workspace, "rev-list", "--count", f"{info['base']}..HEAD"))
        if info["commits"]:
            ref = info["branch"]
            git(Path(info["repo"]), "fetch", "-q", "--no-tags", str(workspace), f"+HEAD:refs/heads/{ref}")
            info["handed_back"] = True
    except (RuntimeError, OSError, ValueError) as e:
        info["error"] = str(e)[:300]

"""Git for a run's workspace: work on a clone of the project's own repository, on a branch of its own.

With `git.mode` = auto (the default) and a project folder (or `git.repo`) that is already a git repository with
at least one commit, a run starts from a local clone of it on the branch katana/<run-id>, and when the run ends
Katana fetches that branch into the repository as a ref. Nothing in the repository changes except that new
branch: its working tree, HEAD and other branches are left alone. Otherwise (mode off, no repository, or no
commits yet) the run starts from a fresh `git init`, as before.

The clone has no `origin`, so nothing an agent runs in the workspace can reach the real repository. Handing the
branch back is a local fetch. Publishing it is a separate step that Katana itself runs, in the real repository,
after the run ends (see `publish` and `git.publish`): push the branch and open a pull request (the default),
only push it, merge it into the base branch, or leave it local.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

MODES = ["auto", "off"]
# What happens to the branch once it is handed back. A run that did not finish "done" is never merged:
# merge falls back to a draft pull request.
PUBLISH = ["pr", "push", "merge", "local"]
DEFAULT_PUBLISH = "pr"
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


def remote(repo: Path) -> str | None:
    """The URL of the repository's `origin`, or None when it has none (nothing can be published then)."""
    return git(Path(repo), "remote", "get-url", "origin", check=False) or None


def base_branch(repo: Path) -> str:
    """The branch a pull request or merge targets: the repository's current branch when origin has it, else
    origin's default branch."""
    repo = Path(repo)
    current = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if current and git(repo, "ls-remote", "--heads", "origin", current, check=False):
        return current
    default = git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    if default.startswith("origin/"):
        return default[len("origin/"):]
    raise RuntimeError(f"cannot tell which branch of origin to target (the current branch {current or 'is detached'} is not on origin)")


def _merge(repo: Path, branch: str, base: str) -> None:
    """Merges `branch` into origin's `base` and pushes it, without touching the checkout: a fast-forward is a plain
    push, anything else is merged in a throwaway worktree."""
    git(repo, "fetch", "-q", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    tip = f"refs/remotes/origin/{base}"
    if subprocess.run(["git", "merge-base", "--is-ancestor", tip, f"refs/heads/{branch}"], cwd=repo, env=ENV).returncode == 0:
        git(repo, "push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{base}")
        return
    tmp = Path(tempfile.mkdtemp(prefix="katana-merge-"))
    tree = tmp / "tree"
    try:
        git(repo, "worktree", "add", "-q", "--detach", str(tree), tip)
        ident = ["-c", "user.name=Katana", "-c", "user.email=katana@localhost"]
        git(tree, *ident, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", f"refs/heads/{branch}")
        git(tree, "push", "-q", "origin", f"HEAD:refs/heads/{base}")
    finally:
        git(repo, "worktree", "remove", "--force", str(tree), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def publish(info: dict, status: str, title: str, body: str) -> None:
    """Publishes the branch that hand_back put in the repository, as `info['publish']` says, and records
    `info['published']`: {mode, result: local | pushed | pr | merged, url, base, note, error}. Runs in the real
    repository with the user's own git and gh credentials; never prompts. A run that is not "done" is
    published as a draft pull request at most."""
    mode = info.get("publish") if info.get("publish") in PUBLISH else "local"
    out = info["published"] = {"mode": mode, "result": "local"}
    if mode == "local":
        return
    repo, branch = Path(info["repo"]), info["branch"]
    try:
        if not remote(repo):
            out["note"] = "the repository has no origin remote, so the branch stays local"
            return
        base = out["base"] = base_branch(repo)
        if mode == "merge" and status != "done":
            mode, out["note"] = "pr", f"the run ended {status}, so it is not merged; a draft pull request instead"
        if mode == "merge":
            try:
                _merge(repo, branch, base)
                out["result"] = "merged"
                return
            except RuntimeError as e:
                mode, out["note"] = "pr", f"could not merge into {base} ({str(e)[:200]}); a pull request instead"
        git(repo, "push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
        out["result"] = "pushed"
        if mode != "pr":
            return
        if not shutil.which("gh"):
            out["note"] = (out.get("note", "") + "; " if out.get("note") else "") + "gh is not installed: open the pull request from the pushed branch"
            return
        cmd = ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body]
        if status != "done":
            cmd.append("--draft")
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=ENV)
        if p.returncode:
            raise RuntimeError(f"gh pr create: {(p.stderr or p.stdout).strip()}")
        out["url"] = (p.stdout.strip().splitlines() or [""])[-1]
        out["result"] = "pr"
    except (RuntimeError, OSError) as e:
        out["error"] = str(e)[:300]

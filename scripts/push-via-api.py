#!/usr/bin/env python3
"""Push local HEAD to GitHub via the GitHub API.

This is qywx-cd2-bot's durable workaround for this workstation, where normal
`git push`/`git fetch` over HTTPS can time out even though `gh api` works.

Default behavior:
- Pushes local commits to Vivitoto/qywx-cd2-bot main via GitHub's Git Data API.
- Requires a clean working tree, because only committed HEAD content is pushed.
- Replays commits whose ancestor tree matches the current remote main tree.
  This also handles the common case where a previous API push created a remote
  commit SHA that differs from the local commit SHA, while both trees match.

Usage:
  scripts/push-via-api.py
  scripts/push-via-api.py --dry-run

Environment overrides:
  QYWX_CD2_BOT_REPO=owner/repo
  QYWX_CD2_BOT_BRANCH=main
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REPO = os.environ.get("QYWX_CD2_BOT_REPO", "Vivitoto/qywx-cd2-bot")
DEFAULT_BRANCH = os.environ.get("QYWX_CD2_BOT_BRANCH", "main")


@dataclass(frozen=True)
class CommitMeta:
    message: str
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str


def run(args: list[str], *, input_bytes: bytes | None = None) -> str:
    res = subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        if res.stdout:
            sys.stdout.write(res.stdout.decode(errors="replace"))
        if res.stderr:
            sys.stderr.write(res.stderr.decode(errors="replace"))
        raise SystemExit(res.returncode)
    return res.stdout.decode()


def run_bytes(args: list[str]) -> bytes:
    res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        if res.stderr:
            sys.stderr.write(res.stderr.decode(errors="replace"))
        raise SystemExit(res.returncode)
    return res.stdout


def git(args: list[str]) -> str:
    return run(["git", *args])


def gh_api(args: list[str], payload: dict[str, Any] | None = None) -> str:
    input_bytes = json.dumps(payload).encode() if payload is not None else None
    command = ["gh", "api", *args]
    if payload is not None:
        command.extend(["--input", "-"])
    return run(command, input_bytes=input_bytes)


def require_repo_root() -> None:
    root = git(["rev-parse", "--show-toplevel"]).strip()
    cwd = str(Path.cwd().resolve())
    if Path(root).resolve() != Path(cwd):
        print(f"error: run from repo root: {root}", file=sys.stderr)
        raise SystemExit(2)


def require_clean_worktree() -> None:
    status = git(["status", "--porcelain"])
    if status.strip():
        print("error: working tree is not clean; commit or stash changes first", file=sys.stderr)
        print(status, file=sys.stderr)
        raise SystemExit(2)


def tree_of(ref: str) -> str:
    return git(["rev-parse", f"{ref}^{{tree}}"]).strip()


def commit_exists_locally(ref: str) -> bool:
    res = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


def remote_branch(repo: str, branch: str) -> tuple[str, str]:
    branch_json = json.loads(gh_api([f"repos/{repo}/branches/{branch}"]))
    sha = branch_json["commit"]["sha"]
    commit_json = json.loads(gh_api([f"repos/{repo}/git/commits/{sha}"]))
    return sha, commit_json["tree"]["sha"]


def find_base_with_tree(remote_tree: str, head: str) -> str | None:
    # Prefer exact remote SHA when it exists locally; otherwise fall back to a
    # first-parent ancestor with the same tree. The tree fallback is what keeps
    # API pushes usable after earlier API-created commits have different SHAs.
    for commit in git(["rev-list", "--first-parent", head]).splitlines():
        if tree_of(commit) == remote_tree:
            return commit
    return None


def commits_to_replay(base: str, head: str) -> list[str]:
    commits = git(["rev-list", "--reverse", f"{base}..{head}"]).splitlines()
    return [c for c in commits if c.strip()]


def commit_meta(commit: str) -> CommitMeta:
    raw = git([
        "log",
        "-1",
        "--format=%B%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
        commit,
    ])
    parts = raw.split("\x00")
    if len(parts) != 7:
        raise RuntimeError(f"unexpected git log metadata for {commit[:7]}")
    return CommitMeta(
        message=parts[0].rstrip("\n"),
        author_name=parts[1],
        author_email=parts[2],
        author_date=parts[3],
        committer_name=parts[4],
        committer_email=parts[5],
        committer_date=parts[6].strip(),
    )


def ls_tree_entry(commit: str, path: str) -> tuple[str, str]:
    out = run_bytes(["git", "ls-tree", "-z", commit, "--", path])
    record = out.rstrip(b"\0")
    if not record:
        raise RuntimeError(f"path not found in {commit[:7]}: {path}")
    meta, _path = record.split(b"\t", 1)
    mode, obj_type, _sha = meta.decode().split()
    if obj_type != "blob":
        raise RuntimeError(f"unsupported git object type for {path}: {obj_type}")
    return mode, obj_type


def blob_bytes(commit: str, path: str) -> bytes:
    return run_bytes(["git", "show", f"{commit}:{path}"])


def changed_tree_entries(commit: str) -> list[dict[str, Any]]:
    lines = git(["diff-tree", "--name-status", "-r", "-M", "--no-commit-id", commit]).splitlines()
    entries: list[dict[str, Any]] = []

    def add_delete(path: str) -> None:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        print(f"delete  {path}")

    def add_blob(path: str) -> None:
        mode, obj_type = ls_tree_entry(commit, path)
        data = blob_bytes(commit, path)
        payload = {"content": base64.b64encode(data).decode(), "encoding": "base64"}
        out = gh_api([f"repos/{ARGS.repo}/git/blobs", "--method", "POST"], payload)
        sha = json.loads(out)["sha"]
        entries.append({"path": path, "mode": mode, "type": obj_type, "sha": sha})
        print(f"blob {sha[:7]} {mode} {path}")

    for line in lines:
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("D"):
            add_delete(parts[1])
        elif status.startswith("R"):
            old_path, new_path = parts[1], parts[2]
            add_delete(old_path)
            add_blob(new_path)
        elif status.startswith("C"):
            add_blob(parts[2])
        else:
            add_blob(parts[1])
    return entries



def create_initial_remote_commit(commit: str) -> tuple[str, str]:
    """Create the first commit on an empty remote: full tree, no parents."""
    entries: list[dict[str, Any]] = []
    lines = git(["ls-tree", "-r", "-z", commit]).split("\0")
    for line in lines:
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, obj_type, _sha = meta.split()
        if obj_type != "blob":
            continue
        data = run_bytes(["git", "show", f"{commit}:{path}"])
        payload = {"content": base64.b64encode(data).decode(), "encoding": "base64"}
        out = gh_api([f"repos/{ARGS.repo}/git/blobs", "--method", "POST"], payload)
        blob_sha = json.loads(out)["sha"]
        entries.append({"path": path, "mode": mode, "type": obj_type, "sha": blob_sha})
        print(f"blob {blob_sha[:7]} {mode} {path}")
    tree_out = gh_api([f"repos/{ARGS.repo}/git/trees", "--method", "POST"], {"tree": entries})
    tree_sha = json.loads(tree_out)["sha"]
    print("tree", tree_sha)

    meta = commit_meta(commit)
    commit_payload = {
        "message": meta.message,
        "tree": tree_sha,
        "parents": [],
        "author": {"name": meta.author_name, "email": meta.author_email, "date": meta.author_date},
        "committer": {"name": meta.committer_name, "email": meta.committer_email, "date": meta.committer_date},
    }
    commit_out = gh_api([f"repos/{ARGS.repo}/git/commits", "--method", "POST"], commit_payload)
    remote_commit = json.loads(commit_out)["sha"]
    print(f"commit {remote_commit}  # local {commit[:7]}")
    return remote_commit, tree_sha


def create_remote_commit(commit: str, parent_sha: str, base_tree: str) -> tuple[str, str]:
    entries = changed_tree_entries(commit)
    tree_payload = {"base_tree": base_tree, "tree": entries}
    tree_out = gh_api([f"repos/{ARGS.repo}/git/trees", "--method", "POST"], tree_payload)
    tree_sha = json.loads(tree_out)["sha"]
    print("tree", tree_sha)

    meta = commit_meta(commit)
    commit_payload = {
        "message": meta.message,
        "tree": tree_sha,
        "parents": [parent_sha] if parent_sha else [],
        "author": {
            "name": meta.author_name,
            "email": meta.author_email,
            "date": meta.author_date,
        },
        "committer": {
            "name": meta.committer_name,
            "email": meta.committer_email,
            "date": meta.committer_date,
        },
    }
    commit_out = gh_api([f"repos/{ARGS.repo}/git/commits", "--method", "POST"], commit_payload)
    remote_commit = json.loads(commit_out)["sha"]
    print(f"commit {remote_commit}  # local {commit[:7]}")
    return remote_commit, tree_sha


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo, default: {DEFAULT_REPO}")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help=f"Branch, default: {DEFAULT_BRANCH}")
    parser.add_argument("--dry-run", action="store_true", help="plan only; do not create blobs/commits or move refs")
    parser.add_argument("--allow-dirty", action="store_true", help="allow dirty working tree; still pushes committed HEAD only")
    return parser.parse_args()


def main() -> int:
    global ARGS
    ARGS = parse_args()
    require_repo_root()
    if not ARGS.allow_dirty:
        require_clean_worktree()

    head = git(["rev-parse", "HEAD"]).strip()
    head_tree = tree_of("HEAD")
    try:
        remote_sha, remote_tree = remote_branch(ARGS.repo, ARGS.branch)
    except SystemExit:
        # Empty repo: no remote branch yet. Create the initial commit from HEAD
        # tree with no parents (history is intentionally flattened here).
        remote_sha = None
        remote_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree

    print(f"repo          {ARGS.repo}")
    print(f"branch        {ARGS.branch}")
    print(f"local HEAD    {head}")
    print(f"remote HEAD   {remote_sha}")
    print(f"local tree    {head_tree}")
    print(f"remote tree   {remote_tree}")

    if remote_sha is not None and head_tree == remote_tree:
        print("local HEAD tree already matches remote branch tree; nothing to push")
        return 0

    if remote_sha is None:
        print("empty remote; creating initial commit from HEAD tree")
        if ARGS.dry_run:
            print("dry-run: not creating initial commit/ref")
            return 0
        current_parent, current_tree = create_initial_remote_commit(head)
        gh_api(
            [f"repos/{ARGS.repo}/git/refs", "--method", "POST"],
            {"ref": f"refs/heads/{ARGS.branch}", "sha": current_parent},
        )
        print(f"refs/heads/{ARGS.branch} => {current_parent}")
        print(f"final tree {current_tree}")
        return 0

    base = find_base_with_tree(remote_tree, head)
    if base is None:
        if commit_exists_locally(remote_sha):
            print("remote commit exists locally but no matching tree was found", file=sys.stderr)
        else:
            print(
                "error: cannot find a local first-parent ancestor whose tree matches remote branch.\n"
                "The remote branch likely changed in a way this API helper cannot safely replay.\n"
                "Inspect remote changes before pushing.",
                file=sys.stderr,
            )
        return 2

    replay = commits_to_replay(base, head)
    if not replay:
        print("no commits to replay")
        return 0

    print(f"base          {base}  # tree matches remote")
    print("replay commits")
    for commit in replay:
        subject = git(["log", "-1", "--format=%s", commit]).strip()
        print(f"  {commit[:7]} {subject}")

    if ARGS.dry_run:
        print("dry-run: not creating GitHub blobs/commits and not moving refs")
        return 0

    current_parent = remote_sha
    current_tree = remote_tree
    for commit in replay:
        current_parent, current_tree = create_remote_commit(commit, current_parent, current_tree)

    gh_api(
        [f"repos/{ARGS.repo}/git/refs/heads/{ARGS.branch}", "--method", "PATCH"],
        {"sha": current_parent, "force": False},
    )
    print(f"refs/heads/{ARGS.branch} => {current_parent}")
    print(f"final tree {current_tree}")
    return 0


if __name__ == "__main__":
    ARGS = argparse.Namespace(repo=DEFAULT_REPO, branch=DEFAULT_BRANCH)
    sys.exit(main())

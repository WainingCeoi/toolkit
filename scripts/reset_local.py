"""Plan and confirm deletion of the checkout's Git-ignored local state."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Entry:
    signature: tuple[int, ...]
    ignored: bool
    repository: bool = False
    children: dict[str, Entry] = field(default_factory=dict)


@dataclass
class Deletion:
    entry: Entry
    remove: bool
    children: dict[str, Deletion]


def _git_ignored(root: Path) -> tuple[dict, set[tuple[str, ...]]]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)

    def git(*args: str) -> bytes:
        result = subprocess.run(
            ["git", *args], cwd=root, env=env, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError(
                os.fsdecode(result.stderr).strip() or "Git inventory failed."
            )
        return result.stdout

    if git("rev-parse", "--is-inside-work-tree", "--show-prefix") != b"true\n\n":
        raise RuntimeError("Run the reset from the root of a Git checkout.")
    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")[:-1])).resolve() != root:
        raise RuntimeError("Git's worktree differs from the reset checkout.")

    metadata = set()

    def protect(path: Path) -> None:
        for location in (Path(os.path.abspath(path)), path.resolve()):
            if location == root:
                raise RuntimeError("Git metadata overlaps the reset checkout.")
            if location.is_relative_to(root):
                metadata.add(location.relative_to(root).parts)
        for parent in path.parents:
            if parent.is_relative_to(root) and parent.is_symlink():
                metadata.add(parent.relative_to(root).parts)

    for args in (
        ("--absolute-git-dir",),
        ("--git-common-dir",),
        ("--git-path", "objects"),
    ):
        protect(root / os.fsdecode(git("rev-parse", *args)[:-1]))
    # Git canonicalizes a linked admin directory, so retain its pointer path too.
    marker = root / ".git"
    if marker.is_file():
        link = marker.read_bytes().rstrip(b"\r\n")
        if link.startswith(b"gitdir: "):
            protect(root / os.fsdecode(link[len(b"gitdir: ") :]))

    ignored = git(
        "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
    )
    # Git lists an entire ignored directory only when it contains no tracked paths.
    # Otherwise the output contains the individual ignored siblings.
    tree: dict = {}
    for raw in ignored.split(b"\0"):
        if not raw:
            continue
        parts = os.fsdecode(raw).rstrip("/").split("/")
        if any(part.casefold() in {"", ".", "..", ".git"} for part in parts):
            raise RuntimeError(f"Unsafe path in Git inventory: {os.fsdecode(raw)!r}")
        branch = tree
        for part in parts[:-1]:
            branch = branch.setdefault(part, {})
            if branch is None:
                break
        else:
            branch[parts[-1]] = None
    return tree, metadata


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _directory(name, *, parent=None, expected=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if expected is not None and _signature(os.fstat(fd)) != expected:
            raise RuntimeError(f"Directory changed during reset: {name!r}")
        yield fd
    finally:
        os.close(fd)


def _inventory(
    fd: int,
    paths: dict | None,
    device: int,
    protected: set[tuple[str, ...]],
    names: list[str] | None = None,
    git_metadata: bool = False,
    prefix=(),
) -> dict[str, Entry]:
    entries = {}
    if names is None:
        names = os.listdir(fd)
    for name in sorted(names if paths is None else paths):
        subtree = None if paths is None else paths[name]
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_dev != device:
            raise RuntimeError(f"Reset would cross a filesystem boundary at {name!r}.")
        path = (*prefix, name)
        if path in protected:
            entries[name] = Entry(_signature(info), False)
            continue
        entry = Entry(_signature(info), subtree is None)
        if stat.S_ISDIR(info.st_mode):
            with _directory(name, parent=fd, expected=entry.signature) as child:
                child_names = os.listdir(child)
                child_metadata = git_metadata or name.casefold() == ".git"
                lowered = {item.casefold() for item in child_names}
                entry.repository = not child_metadata and (
                    ".git" in lowered
                    or {"head", "config", "objects", "refs"} <= lowered
                )
                entry.children = _inventory(
                    child, subtree, device, protected, child_names, child_metadata, path
                )
        elif subtree is not None:
            raise RuntimeError(f"Git path has an unsafe parent: {name!r}")
        entries[name] = entry
    return entries


def _nested_choices(entries: dict[str, Entry], skipped: set, prefix=()) -> bool:
    for name, entry in entries.items():
        path = (*prefix, name)
        if entry.repository:
            print(
                f"Ignored state includes Git repository or worktree {'/'.join(path)!r}."
            )
            answer = input("Type delete, skip, or cancel: ").strip()
            if answer == "skip":
                skipped.add(path)
                continue
            if answer != "delete":
                return False
        if not _nested_choices(entry.children, skipped, path):
            return False
    return True


def _select(entries: dict[str, Entry], skipped: set, prefix=()) -> dict[str, Deletion]:
    plan = {}
    for name, entry in entries.items():
        path = (*prefix, name)
        if path in skipped:
            continue
        children = _select(entry.children, skipped, path)
        remove = entry.ignored and all(
            child in children and children[child].remove for child in entry.children
        )
        if remove or children:
            plan[name] = Deletion(entry, remove, children)
    return plan


def _will_remove(plan: dict[str, Deletion], path: Path) -> bool:
    for name in path.parts:
        deletion = plan.get(name)
        if deletion is None:
            return False
        if deletion.remove:
            return True
        plan = deletion.children
    return False


def _verify_rebuild_paths(root: Path, plan: dict[str, Deletion]) -> None:
    for needed in (
        "backend/.env.example",
        "backend/.python-version",
        "backend/pyproject.toml",
        "backend/src",
        "backend/uv.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "scripts",
    ):
        if _will_remove(plan, Path(needed)):
            raise RuntimeError(f"Reset would remove a rebuild input: {needed}")
    for interpreter in {
        Path(sys.executable).absolute(),
        Path(sys.executable).resolve(),
    }:
        if interpreter.is_relative_to(root) and _will_remove(
            plan, interpreter.relative_to(root)
        ):
            raise RuntimeError(
                f"Reset would remove its Python interpreter: {interpreter}"
            )

    required_types = {
        "backend": stat.S_ISDIR,
        "frontend": stat.S_ISDIR,
        "backend/.env": stat.S_ISREG,
        "backend/data": stat.S_ISDIR,
        "backend/.venv": None,
        "frontend/node_modules": None,
        "backend/data/bitcomet-devices.json": None,
    }
    for db in ("sub.db", "torrents.db"):
        for suffix in ("", "-journal", "-shm", "-wal"):
            required_types[f"backend/data/{db}{suffix}"] = None
    device = root.stat().st_dev
    for name, accepts in required_types.items():
        if _will_remove(plan, Path(name)):
            continue
        try:
            info = (root / name).lstat()
        except FileNotFoundError:
            continue
        if (
            accepts is None
            or not accepts(info.st_mode)
            or info.st_dev != device
            or (name == "backend/.env" and info.st_nlink != 1)
        ):
            raise RuntimeError(f"Retained path prevents a safe rebuild: {name}")


def _category(path: str) -> str:
    location = Path(path)
    if location.is_relative_to("backend/.venv") or location.is_relative_to(
        "frontend/node_modules"
    ):
        return "dependencies"
    if location.is_relative_to("backend/data"):
        return "saved subscriptions and BitComet settings"
    if location.name.startswith(".env"):
        return "local configuration"
    if location.is_relative_to("frontend/dist"):
        return "build output"
    return "other ignored local state"


def _preview(plan: dict[str, Deletion], prefix=()) -> None:
    for name, deletion in plan.items():
        path = (*prefix, name)
        if deletion.remove:
            label = "/".join(path)
            suffix = "/" if stat.S_ISDIR(deletion.entry.signature[2]) else ""
            print(f"  {label + suffix!r}  ({_category(label)})")
        else:
            _preview(deletion.children, path)


def _delete(fd: int, plan: dict[str, Deletion], deleted_links: dict) -> None:
    for name, deletion in plan.items():
        expected = deletion.entry.signature
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        current = _signature(info)
        inode = current[:2]
        removed = deleted_links.get(inode, 0)
        if removed:
            # Our previous unlink changes the inode's ctime and link count.
            unchanged = (
                current[:3] == expected[:3]
                and current[3] == expected[3] - removed
                and current[4:6] == expected[4:6]
            )
        else:
            unchanged = current == expected
        if not unchanged:
            raise RuntimeError(f"Path changed during reset: {name!r}")
        if stat.S_ISDIR(info.st_mode):
            with _directory(name, parent=fd, expected=expected) as child:
                _delete(child, deletion.children, deleted_links)
                if deletion.remove:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if _signature(current)[:3] != expected[:3]:
                        raise RuntimeError(f"Directory changed during reset: {name!r}")
                    os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
            if expected[3] > 1:
                deleted_links[inode] = removed + 1


def reset_checkout(root: Path, check_idle: Callable[[], None]) -> bool:
    """Delete exactly the confirmed ignored paths. Return False on cancellation."""
    if not sys.stdin.isatty():
        print("Reset cancelled. Use an interactive terminal to confirm deletion.")
        return False
    root = root.resolve()
    with _directory(root) as fd:
        root_info = os.fstat(fd)
        paths, metadata = _git_ignored(root)
        inventory = _inventory(fd, paths, root_info.st_dev, metadata)
        skipped: set = set()
        try:
            if not _nested_choices(inventory, skipped):
                print("Reset cancelled.")
                return False
            plan = _select(inventory, skipped)
            _verify_rebuild_paths(root, plan)
            print(f"Ignored paths to delete in {str(root)!r}:")
            if plan:
                _preview(plan)
                print("Local configuration and data in these paths will be lost.")
            else:
                print("  No paths to delete. The local environment will be rebuilt.")
            approved = input("Type reset to continue: ").strip() == "reset"
        except EOFError:
            approved = False
        if not approved:
            print("Reset cancelled.")
            return False
        check_idle()
        current_root = root.lstat()
        if (current_root.st_dev, current_root.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            raise RuntimeError(
                "Checkout changed after the reset preview. Run make init again."
            )
        current_paths, current_metadata = _git_ignored(root)
        if metadata != current_metadata or inventory != _inventory(
            fd, current_paths, root_info.st_dev, current_metadata
        ):
            raise RuntimeError(
                "Ignored paths changed after the reset preview. Run make init again."
            )
        _verify_rebuild_paths(root, plan)
        _delete(fd, plan, {})
    return True

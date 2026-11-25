"""Bounded UTF-8 skill trees; shared revision framing, descriptor-relative node checks."""

from __future__ import annotations

import os
import stat

from ..playbooks.tree import Tree
from . import contract

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def tree(files: dict) -> Tree:
    if not isinstance(files, dict) or not 1 <= len(files) <= contract.MAX_FILES:
        raise contract.GalleryError("Expected between 1 and 64 skill files.")
    # Use the shared contract for every path, text, size and collision check.
    contract.content(
        dict(
            format=1,
            kind="skill",
            id="local-skill",
            version="1.0.0",
            title="Local skill",
            author="Local",
            publisher="Local",
            summary="Editable local skill",
            source="https://battlelab.superstatus.io",
            requirements=[],
            files=files,
        )
    )
    directories = set()
    for path in files:
        parts = path.split("/")
        directories.update("/".join(parts[:n]) for n in range(1, len(parts)))
    return Tree({name: value.encode("utf-8") for name, value in files.items()}, directories)


def read(fd: int) -> Tree:
    files, directories, count, total = {}, set(), 0, 0

    def walk(parent, prefix="", depth=0):
        nonlocal count, total
        if depth > 8:
            raise contract.GalleryError("Skill directories exceed the depth limit.")
        for name in os.listdir(parent):
            count += 1
            if count > 512:
                raise contract.GalleryError("Skill tree exceeds its entry limit.")
            path = prefix + name
            contract.path(path)
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                directories.add(path)
                child = os.open(name, DIR, dir_fd=parent)
                try:
                    walk(child, path + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                child = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent
                )
                try:
                    opened = os.fstat(child)
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        raise contract.GalleryError("Skill file changed its node type.")
                    with os.fdopen(child, "rb", closefd=False) as stream:
                        raw = stream.read(contract.MAX_FILE_BYTES + 1)
                finally:
                    os.close(child)
                total += len(raw)
                if len(raw) > contract.MAX_FILE_BYTES or total > contract.MAX_CONTENT_BYTES:
                    raise contract.GalleryError("Skill exceeds its content limit.")
                files[path] = raw.decode("utf-8")
            else:
                raise contract.GalleryError(
                    "Skill trees contain regular text files and directories only."
                )

    walk(fd)
    result = tree(files)
    if result.dirs != directories:
        raise contract.GalleryError(
            "Empty skill directories cannot be represented; remove them before saving."
        )
    return result

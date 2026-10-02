"""``renameat2(2)`` for the stores that publish a whole entry at once (#950, #1191).

``os.rename`` silently REPLACES an empty destination directory and cannot swap two names, so a
store that must never replace what another writer put there, or that must swap a staged tree into
place in one step, calls the kernel's ``renameat2`` directly. ``OSError`` (``ENOSYS`` /
``EINVAL``) where the kernel or the filesystem does not offer a flag: the caller refuses the write
rather than approximating it.
"""

from __future__ import annotations

import ctypes
import errno
import os

#: Fail with ``EEXIST`` if the destination exists.
RENAME_NOREPLACE = 1
#: Atomically swap the two names (both must exist).
RENAME_EXCHANGE = 2

_fn: object = None


def renameat2(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str, flags: int) -> None:
    """``renameat2(src_dir_fd, src, dst_dir_fd, dst, flags)``, raising ``OSError`` on failure
    (``FileExistsError`` for a claimed destination under ``RENAME_NOREPLACE``)."""
    global _fn
    if _fn is None:
        try:
            fn = ctypes.CDLL(None, use_errno=True).renameat2
            fn.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            fn.restype = ctypes.c_int
            _fn = fn
        except (OSError, AttributeError):
            _fn = False
    if _fn is False:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    call = _fn
    assert callable(call)
    if call(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

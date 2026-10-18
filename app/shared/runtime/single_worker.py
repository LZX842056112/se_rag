"""查询服务的主机级单 worker 锁。进程退出时操作系统释放锁。"""
from __future__ import annotations

from pathlib import Path
import tempfile

from app.shared.config import settings


class SingleWorkerLease:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(tempfile.gettempdir()) / f"se-rag-query-{settings.api.query_port}.lock"
        self.file = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("查询服务仅支持一个 worker；请使用 --workers 1") from exc
        self.file = handle

    def release(self):
        if self.file:
            handle, self.file = self.file, None
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

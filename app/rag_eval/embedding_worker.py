"""One cancellable local embedding process per evaluation batch."""
from __future__ import annotations

import asyncio
import multiprocessing


class EmbeddingWorkerUnavailableError(RuntimeError):
    """A failed or cancelled worker must not start further embedding jobs."""


def _serve_embeddings(connection, encode):
    try:
        while True:
            texts = connection.recv()
            try:
                connection.send((True, encode(texts)))
            except Exception as exc:
                connection.send((False, type(exc).__name__))
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


class DenseEmbeddingWorker:
    """Reuse one model; cancellation kills the process instead of joining threads."""

    def __init__(self, encode):
        self._encode = encode
        self._connection = None
        self._process = None
        self._unavailable = False
        self._busy = False

    async def encode(self, texts):
        if self._unavailable or self._busy:
            raise EmbeddingWorkerUnavailableError("向量工作进程不可用或正在计算")
        self._busy = True
        try:
            if self._process is None:
                context = multiprocessing.get_context("spawn")
                self._connection, child = context.Pipe()
                self._process = context.Process(target=_serve_embeddings,
                    args=(child, self._encode), name="ragas-bge", daemon=True)
                try:
                    self._process.start()
                finally:
                    child.close()
            self._connection.send(list(texts))
            while not self._connection.poll():
                if not self._process.is_alive():
                    raise EmbeddingWorkerUnavailableError("向量工作进程已退出")
                await asyncio.sleep(.01)
            success, result = self._connection.recv()
            if not success:
                raise EmbeddingWorkerUnavailableError(result)
            return result
        except BaseException:
            # In particular, wait_for cancellation must stop the local model.
            self.close()
            raise
        finally:
            self._busy = False

    def close(self):
        self._unavailable = True
        process, self._process = self._process, None
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=.2)
            if not process.is_alive():
                process.close()
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

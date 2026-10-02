"""One durable plugin job, with worker ownership BEFORE the planned checkpoint (#1259).

A lost HTTP response never resubmits work. The worker acknowledges the durable operation ID
while retaining the cross-process fence until the job finishes. Another app worker's recovery
therefore cannot mistake a queued live install/verification for an abandoned one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from . import manager, process, storage


class Service:
    def __init__(self):
        self._busy = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._verify_loop = None
        self._verify_task = None

    async def _start(self, plan, work) -> dict:
        if self._stopping.is_set() or not self._busy.acquire(blocking=False):
            raise manager.ManagerError("another plugin operation is busy")
        ready = concurrent.futures.Future()

        def worker():
            item = None
            try:
                with storage.locked("worker", wait=0):
                    manager.expire_ready_signins()
                    item = plan()
                    ready.set_result(item)
                    if item["state"] != "planned":
                        return
                    try:
                        work(item)
                    except process.CleanupError:
                        manager._set_operation(
                            item["id"],
                            "cleanup_pending",
                            error="temporary process cleanup is pending",
                        )
                    except Exception:
                        manager._set_operation(
                            item["id"],
                            "failed",
                            error="operation could not complete; retry a new attempt",
                            if_states=manager._BUSY,
                        )
            except Exception as exc:
                if not ready.done():
                    ready.set_exception(
                        exc
                        if isinstance(exc, manager.ManagerError | storage.StateError)
                        else manager.ManagerError("another plugin operation is busy or unavailable")
                    )
                elif item is not None:
                    # A persist failure preserves the last durable state; recovery resolves it.
                    pass
            finally:
                self._busy.release()

        try:
            self._thread = threading.Thread(target=worker, name="plugin-operation", daemon=True)
            self._thread.start()
        except BaseException:
            self._busy.release()
            raise
        wrapped = asyncio.wrap_future(ready)
        # A disconnected caller leaves the server job running. Consume a later refusal even
        # when nobody is left to await it; the durable operation remains the observable record.
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return await asyncio.shield(wrapped)

    async def install(self, request_id: str, review_id: str, digest: str, **confirmations) -> dict:
        def plan():
            return manager.begin_install(request_id, review_id, digest, **confirmations)

        if request_id in manager.snapshot()["operations"]:
            return await asyncio.to_thread(plan)
        return await self._start(plan, lambda item: manager._run_install(item["id"]))

    async def verify(
        self, request_id: str, plugin_id: str, generation_id: str, *, confirm_effects: bool = False
    ) -> dict:
        from . import probes

        def plan():
            return manager.begin_action(
                request_id, plugin_id, generation_id, "verify", confirm_effects=confirm_effects
            )

        def work(item):
            manager._set_operation(item["id"], "running")

            async def run():
                self._verify_loop = asyncio.get_running_loop()
                self._verify_task = asyncio.current_task()
                try:
                    if self._stopping.is_set():
                        raise asyncio.CancelledError
                    await probes.run(item)
                except asyncio.CancelledError:
                    manager._set_operation(
                        item["id"], "interrupted", error="verification interrupted"
                    )
                finally:
                    self._verify_task = None
                    self._verify_loop = None

            asyncio.run(run())

        if request_id in manager.snapshot()["operations"]:
            return await asyncio.to_thread(plan)
        return await self._start(plan, work)

    async def signin(self, request_id: str, plugin_id: str, generation_id: str) -> dict:
        def plan():
            with storage.locked("worker", wait=0):
                manager.expire_ready_signins()
                return manager.begin_action(request_id, plugin_id, generation_id, "signin")

        if request_id in manager.snapshot()["operations"]:
            return await asyncio.to_thread(
                manager.begin_action, request_id, plugin_id, generation_id, "signin"
            )
        return await asyncio.to_thread(plan)

    async def shutdown(self) -> None:
        self._stopping.set()
        loop, task = self._verify_loop, self._verify_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self._thread is not None:
            # Install workers own a finite download/extraction job and keep their fence until
            # settled. Graceful shutdown drains them; forced process exit uses startup recovery.
            await asyncio.to_thread(self._thread.join)

    def close(self) -> None:
        self._stopping.set()

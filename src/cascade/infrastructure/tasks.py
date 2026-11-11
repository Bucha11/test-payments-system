"""Taskiq broker and background jobs.

* reconcile_payment(payment_id): enqueued when a payment is parked in
  PENDING_RECONCILIATION (or a webhook resolves its unknown attempt).
* sweep: recovery of expired leases (crashed workers), due reconciliations
  (PENDING_RECONCILIATION whose next-check time passed) and stuck CREATED payments.
  Runs every `sweep_interval_s` inside each worker process; claims are atomic, so
  concurrent sweepers are harmless.
"""

import asyncio
import contextlib
import logging
from uuid import UUID

from taskiq import TaskiqEvents, TaskiqState
from taskiq_redis import ListQueueBroker

from cascade.infrastructure.container import Services, build_services
from cascade.infrastructure.logging import setup_logging
from cascade.infrastructure.settings import get_settings

log = logging.getLogger(__name__)

# The broker's consumer blocks on BRPOP with no server-side timeout, so the socket read
# timeout must be disabled for it (redis-py >= 8 defaults to 5 s, which kills the
# receiver every 5 s). Connect timeout stays explicit.
broker = ListQueueBroker(
    url=get_settings().redis_url,
    socket_timeout=None,
    socket_connect_timeout=5,
)

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = build_services(get_settings(), queue=TaskiqQueue())
    return _services


class TaskiqQueue:
    async def enqueue_reconcile(self, payment_id: UUID) -> None:
        await reconcile_payment.kiq(str(payment_id))


@broker.task(task_name="reconcile_payment")
async def reconcile_payment(payment_id: str) -> None:
    await services().engine.process(UUID(payment_id))


@broker.task(task_name="sweep")
async def sweep() -> int:
    return await services().engine.sweep()


async def _sweep_loop(interval_s: float) -> None:
    while True:
        try:
            n = await services().engine.sweep()
            if n:
                log.info("sweep processed payments", extra={"count": n})
        except Exception:
            log.exception("sweep failed")
        await asyncio.sleep(interval_s)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    setup_logging(get_settings().log_level)
    interval = services().config.cascade.sweep_interval_s
    state.sweeper = asyncio.create_task(_sweep_loop(interval))


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    task = getattr(state, "sweeper", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if _services is not None:
        await _services.aclose()

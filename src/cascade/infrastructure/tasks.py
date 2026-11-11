"""Taskiq broker. Reconciliation/sweep tasks arrive in M3."""

from taskiq_redis import ListQueueBroker

from cascade.infrastructure.settings import get_settings

# The broker's consumer blocks on BRPOP with no server-side timeout, so the socket read
# timeout must be disabled for it (redis-py >= 8 defaults to 5 s, which kills the
# receiver every 5 s). Connect timeout stays explicit.
broker = ListQueueBroker(
    url=get_settings().redis_url,
    socket_timeout=None,
    socket_connect_timeout=5,
)


@broker.task
async def ping() -> str:
    return "pong"

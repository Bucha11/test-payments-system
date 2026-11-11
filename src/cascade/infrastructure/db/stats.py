"""Read-only stats: SQL aggregates over payments and attempts, no extra storage."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PER_PROVIDER = text(
    """
    SELECT provider,
           count(*)                                                       AS attempts,
           count(*) FILTER (WHERE status = 'SUCCEEDED')                   AS approved,
           count(*) FILTER (WHERE status = 'DECLINED_SOFT')               AS soft_declines,
           count(*) FILTER (WHERE status = 'DECLINED_HARD')               AS hard_declines,
           count(*) FILTER (WHERE status IN
                 ('FAILED_NOT_CHARGED', 'UNKNOWN', 'VOIDED', 'STARTED'))  AS failed_unknown,
           count(*) FILTER (WHERE status = 'REFUNDED_DUPLICATE')          AS refunded_duplicates,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
               FILTER (WHERE finished_at IS NOT NULL)                     AS p50_ms,
           percentile_cont(0.95) WITHIN GROUP (
               ORDER BY extract(epoch FROM finished_at - started_at) * 1000)
               FILTER (WHERE finished_at IS NOT NULL)                     AS p95_ms
      FROM attempts
     WHERE started_at >= now() - make_interval(mins => :w)
     GROUP BY provider
     ORDER BY provider
    """
)

_OVERALL = text(
    """
    WITH p AS (SELECT id, status, needs_review FROM payments
                WHERE created_at >= now() - make_interval(mins => :w)),
         first AS (SELECT a.payment_id, a.status FROM attempts a JOIN p ON p.id = a.payment_id
                    WHERE a.seq = 1),
         win AS (SELECT a.payment_id, a.seq FROM attempts a JOIN p ON p.id = a.payment_id
                  WHERE a.status = 'SUCCEEDED')
    SELECT count(*)                                                           AS payments,
           count(*) FILTER (WHERE p.status = 'SUCCEEDED')                     AS succeeded,
           count(*) FILTER (WHERE p.status = 'FAILED')                        AS failed,
           count(*) FILTER (WHERE p.status = 'SUCCEEDED' AND win.seq = 1)
               AS succeeded_first_attempt,
           count(*) FILTER (WHERE p.status = 'SUCCEEDED' AND win.seq > 1)     AS cascade_saved,
           count(*) FILTER (WHERE first.status IS NOT NULL
                              AND first.status NOT IN ('SUCCEEDED', 'DECLINED_HARD'))
                                                                              AS cascade_eligible,
           count(*) FILTER (WHERE p.status = 'PENDING_RECONCILIATION')
               AS pending_reconciliation,
           count(*) FILTER (WHERE p.needs_review)                             AS needs_review
      FROM p
      LEFT JOIN first ON first.payment_id = p.id
      LEFT JOIN win ON win.payment_id = p.id
    """
)


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


async def compute_stats(sm: async_sessionmaker[AsyncSession], window_min: int) -> dict[str, Any]:
    async with sm() as s:
        prov_rows = (await s.execute(_PER_PROVIDER, {"w": window_min})).mappings().all()
        overall = dict((await s.execute(_OVERALL, {"w": window_min})).mappings().one())
    providers = {}
    for r in prov_rows:
        d = dict(r)
        name = d.pop("provider")
        d["approval_rate"] = _ratio(d["approved"], d["attempts"])
        for k in ("p50_ms", "p95_ms"):
            d[k] = round(d[k], 1) if d[k] is not None else None
        providers[name] = d
    eligible = overall.pop("cascade_eligible")
    overall["cascade_eligible"] = eligible
    overall["cascade_save_rate"] = _ratio(overall["cascade_saved"], eligible)
    return {"window_min": window_min, "overall": overall, "providers": providers}

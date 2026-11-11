"""Routing/cascade configuration. Validated at startup; invalid -> fail fast."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CascadeSettings(_Strict):
    max_attempts: PositiveInt = 3
    budget_s: PositiveFloat = 20
    lease_s: PositiveFloat = 30
    lookup_retries: PositiveInt = 3
    unknown_max_age_s: PositiveFloat = 900
    # Backoff and sweep knobs for the unknown-outcome and reconciliation policies.
    lookup_backoff_s: float = Field(default=0.2, ge=0)  # between lookups, doubled per try
    reconcile_backoff_min_s: float = Field(default=1, ge=0)  # reconciliation re-check backoff
    reconcile_backoff_max_s: float = Field(default=60, ge=0)
    sweep_interval_s: PositiveFloat = 2


class ProviderConfig(_Strict):
    type: Literal["mock", "braintree"]
    base_url: str | None = None
    enabled: bool = True
    timeout_s: PositiveFloat
    failure_threshold: PositiveInt
    cooldown_s: PositiveFloat
    # None = adapter default (mock: True, braintree: False).
    cancel_blocks_late_charge: bool | None = None
    # Currencies the provider's account can settle; None = any. A payment in another
    # currency is declined by the adapter without a network call (soft: cascade on).
    currencies: list[str] | None = None
    merchant_account_id: str | None = None  # braintree: None = the account's default

    @model_validator(mode="after")
    def _mock_needs_url(self) -> Self:
        if self.type == "mock" and not self.base_url:
            raise ValueError("mock provider needs base_url")
        return self


class Match(_Strict):
    currency: list[str] | None = None
    country: list[str] | None = None
    amount_minor_gte: int | None = None
    amount_minor_lt: int | None = None
    bin_prefix: list[str] | None = None


class Split(_Strict):
    split: dict[str, PositiveInt] = Field(min_length=1)


RouteEntry = str | Split


class RouteRule(_Strict):
    match: Match | None = None
    default: bool = False
    providers: list[RouteEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _match_xor_default(self) -> Self:
        if (self.match is not None) != self.default:
            return self
        raise ValueError("a route rule has either `match` or `default: true`, not both")

    def provider_names(self) -> set[str]:
        names: set[str] = set()
        for e in self.providers:
            names |= {e} if isinstance(e, str) else set(e.split)
        return names


class RoutingConfig(_Strict):
    cascade: CascadeSettings = CascadeSettings()
    providers: dict[str, ProviderConfig] = Field(min_length=1)
    routes: list[RouteRule] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for i, rule in enumerate(self.routes):
            unknown = rule.provider_names() - set(self.providers)
            if unknown:
                raise ValueError(f"routes[{i}] references unknown providers {sorted(unknown)}")
        defaults = [i for i, r in enumerate(self.routes) if r.default]
        if defaults != [len(self.routes) - 1]:
            raise ValueError("exactly one default route is required and it must be last")
        # The lease is renewed on every engine write. The longest stretch without a write is
        # one attempt: charge, then lookup_retries lookups with backoff, then cancel. If that
        # outlasts the lease, a sweeper takes the payment over mid-step (safe thanks to
        # fencing, but it duplicates provider calls and the first worker's work is lost).
        c = self.cascade
        backoff = sum(c.lookup_backoff_s * 2**i for i in range(c.lookup_retries - 1))
        for name, p in self.providers.items():
            worst = p.timeout_s * (c.lookup_retries + 2) + backoff
            if p.enabled and worst >= c.lease_s:
                raise ValueError(
                    f"providers.{name}: one attempt can take {worst:.1f}s "
                    f"(timeout_s x (lookup_retries + 2) + backoff), "
                    f"not below cascade.lease_s={c.lease_s}"
                )
        return self

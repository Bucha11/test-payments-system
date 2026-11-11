"""Routing/cascade configuration (section 9). Validated at startup; invalid -> fail fast."""

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
    # Not in the spec's example; explicit knobs for the policies the spec describes.
    lookup_backoff_s: float = Field(default=0.2, ge=0)  # 8.2: "short backoff", doubled per try
    reconcile_backoff_min_s: float = Field(default=1, ge=0)  # 8.3: "reschedule with backoff"
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
        return self

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Money:
    """Integer minor units + ISO 4217 alphabetic code. No floats anywhere."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("amount_minor must be int")
        if self.amount_minor <= 0:
            raise ValueError("amount_minor must be > 0")
        if len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isupper():
            raise ValueError("currency must be a 3-letter upper-case ISO code")

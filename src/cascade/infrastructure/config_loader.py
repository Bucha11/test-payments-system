from pathlib import Path

import yaml

from cascade.application.config import RoutingConfig


def load_routing_config(path: str | Path) -> RoutingConfig:
    """Parse and validate config/routing.yaml. Raises on any problem (fail fast)."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return RoutingConfig.model_validate(data)

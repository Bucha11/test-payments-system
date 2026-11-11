"""Domain must not depend on I/O frameworks (spec section 3)."""

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[2] / "src" / "cascade" / "domain"
FORBIDDEN = {"sqlalchemy", "httpx", "fastapi", "redis", "asyncpg", "taskiq", "starlette"}


@pytest.mark.parametrize("path", sorted(DOMAIN.glob("*.py")), ids=lambda p: p.name)
def test_domain_has_no_io_imports(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in FORBIDDEN, f"{path.name} imports {name}"
            assert not name.startswith("cascade.infrastructure"), f"{path.name} imports {name}"
            assert not name.startswith("cascade.api"), f"{path.name} imports {name}"

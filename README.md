# Payment Cascading Service

> Статус: **M1 — скелет и домен** (на ревью). Полный README — в M5.

Сервис каскадных платежей: один платёж последовательно пробуется у нескольких
провайдеров, с гарантией **не более одного успешного списания** даже при неизвестном
исходе вызова (таймаут, обрыв, падение воркера).

## Запуск

```bash
make up      # api, worker, postgres, redis, mock_a, mock_b (+ миграции)
make test    # unit + integration (Postgres/Redis из compose)
make demo    # появится в M5
```

Требуется Docker (compose v2) и [uv](https://docs.astral.sh/uv/).

## Что есть в M1

* Слои `domain / application / infrastructure / api`, `mock_provider` (каркас).
* Миграция Alembic: `payments`, `attempts`, `webhook_events`, частичный уникальный
  индекс «один SUCCEEDED attempt на платёж».
* Доменные enum'ы, единая функция переходов для платежа и попытки, таксономия отказов
  soft/hard, Protocol адаптера провайдера.
* Тесты на каждый легальный и нелегальный переход, таксономию, контракт адаптера,
  отсутствие I/O-импортов в домене, ограничения схемы.

Диаграммы состояний и решения — в [DESIGN.md](DESIGN.md); открытые вопросы — в
[NOTES.md](NOTES.md).

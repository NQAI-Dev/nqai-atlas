# NQAI Atlas

Локальное append-only хранилище долговечных фактов, решений, целей и связей NQAI.

Atlas не заменяет OpenClaw: OpenClaw ведёт диалог и выполняет действия, Atlas хранит проверяемую историю решений между сессиями.

```bash
python3 atlas.py add --kind decision --entity nqai-atlas \
  --text "Atlas хранит долговечные решения, но не выполняет действия." \
  --source "telegram:2026-09-12"
python3 atlas.py search --entity nqai-atlas --active
python3 atlas.py history --entity nqai-atlas
python3 atlas.py observe-projects --root /home/openclaw/Projects
python3 atlas.py health-check --entity service:api --status healthy \
  --source "https://api.example/health" --checked-at "2026-09-18T20:00:00Z"
python3 atlas.py suggest
python3 atlas.py verify
```

Записи лежат в `data/records.jsonl`. История не переписывается: новая запись может указать `--supersedes`, а старой записи можно присвоить `status: superseded`. Команда `history --entity ...` выводит все записи сущности по времени, включая замещённые и архивные, и показывает связи `supersedes`/`superseded_by`.

`observe-projects` сохраняет Git-снимок каждого проекта как observation, пропускает неизменившиеся снимки и связывает изменившийся снимок с предыдущим через `supersedes`.

`health-check` сохраняет статус (`healthy`, `degraded`, `unhealthy`, `unknown`), время фактической проверки и источник. Новая проверка замещает только предыдущую запись того же источника; независимые проверки одной сущности остаются видимыми одновременно.

MCP-инструмент `atlas_history` возвращает ту же полную хронологию сущности. MCP публикует все записи как `atlas://records` и шаблон ресурса `atlas://entity/{entity}` для чтения текущего контекста конкретной сущности. Идентификатор сущности в URI должен быть URL-кодирован, например `project%3Anqai-atlas`.

`suggest` сканирует активные записи и предлагает конкретные следующие шаги, не выполняя их сам: устаревшие цели (14+ дней без обновления), старые решения (20+ дней), просроченные health-check (2+ дня) и грязные Git-снимки. Предложения ранжируются по приоритету (high/medium/low) с указанием записи-основания. MCP-инструмент `atlas_suggest` принимает необязательный фильтр по сущности.

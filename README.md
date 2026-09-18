# NQAI Atlas

Локальное append-only хранилище долговечных фактов, решений, целей и связей NQAI.

Atlas не заменяет OpenClaw: OpenClaw ведёт диалог и выполняет действия, Atlas хранит проверяемую историю решений между сессиями.

```bash
python3 atlas.py add --kind decision --entity nqai-atlas \
  --text "Atlas хранит долговечные решения, но не выполняет действия." \
  --source "telegram:2026-09-12"
python3 atlas.py search --entity nqai-atlas --active
python3 atlas.py observe-projects --root /home/openclaw/Projects
python3 atlas.py verify
```

Записи лежат в `data/records.jsonl`. История не переписывается: новая запись может указать `--supersedes`, а старой записи можно присвоить `status: superseded`.

`observe-projects` сохраняет Git-снимок каждого проекта как observation, пропускает неизменившиеся снимки и связывает изменившийся снимок с предыдущим через `supersedes`.

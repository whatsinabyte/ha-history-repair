---
name: Bug report
about: Something is not working correctly
title: "[BUG] "
labels: bug
assignees: ''

---

## Description

A clear description of what is wrong and what you expected to happen.

## Environment

| Item | Value |
|---|---|
| App version | e.g. `0.1.0` |
| Recorder backend | `sqlite` / `mariadb` / `postgres` |
| Home Assistant version | e.g. `2026.8.0` |
| Installation method | HAOS/Supervised add-on / `docker-compose.yml` |
| Log level | `info` / `debug` |

## Sensor involved (if applicable)

| Item | Value |
|---|---|
| `entity_id` | e.g. `sensor.living_room_temperature` |
| Sensor type shown in the entity list | Measurement / Counter / Unknown |

## Steps to reproduce

1.
2.
3.

## What actually happens

Describe what the app does instead of what you expected.

## Relevant log output

Paste the relevant section from the add-on log. Set log level to `debug`
first if the `info` log does not show the problem.

```
paste log lines here
```

## Additional context

Any other information that might be relevant — a recent Home Assistant
backup restore, a recent database migration, other add-ons writing to the
same recorder.

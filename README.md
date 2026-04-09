# ai-house

Сервис с **LangGraph** и специализированными агентами (логи / БД / GitLab). Один процесс, запуск через Docker Compose.

## Как пользоваться

1. **Настройте `.env`** — ключи LLM (`LLM_PROVIDER` и связанные переменные), при необходимости интеграции: Graylog (для MCP-сервера логов), Postgres, GitLab. Включите нужных агентов флагами `AGENT_*_ENABLED`.
2. **Запустите** `docker compose up -d` — API на порту **8020** (см. ниже).
3. **Отправляйте задачу текстом** — `POST /api/chat` с `{"message": "..."}` или OpenWebUI на порту **3000**, если сервис поднят в compose.
4. **Маршрут** и порядок шагов выбирает LLM-оркестратор: он решает, кого вызвать дальше (`db`, `logs`, `code`) и когда ответить сам или завершать через `synthesize`.
5. **Новые сценарии** добавляются в граф и промпт оркестратора: основная логика живёт в `app/orchestration/graph.py` и `app/orchestration/supervisor.py`.

### Общие вопросы (приветствия, мелкий small talk)

Если оркестратор считает, что внешние системы не нужны, он отвечает пользователю **сам**, без отдельного агента общих вопросов. Это убирает лишний цикл `supervisor -> general -> supervisor` и делает ответы на общие вопросы стабильнее.

---

### Маршрутизация (LangGraph)

| Путь в коде | Назначение |
|-------------|------------|
| `app/orchestration/graph.py` | Цикл `supervisor -> specialist -> supervisor -> synthesize` |
| `app/orchestration/supervisor.py` | LLM-оркестратор: выбор следующего шага, задание специалисту, context hint |

---

## Запуск

1. **Подготовка env**

   ```bash
   cp .env.example .env
   ```

   Заполните переменные по таблице в `.env.example`. Минимум для LLM — см. раздел про `LLM_PROVIDER`.

2. **Docker Compose**

   ```bash
   docker compose up -d
   ```

   API: `http://localhost:8020` — `GET /api/health`, `GET /api/status`.  
   OpenWebUI (если сервис в compose): `http://localhost:3000`.

3. **Чат**

   `POST /api/chat` с телом `{"message": "..."}`.  
   OpenAI-совместимо: `GET /v1/models`, `POST /v1/chat/completions` (модель по умолчанию в ответе — `ai-house-default`).

4. **Агенты on/off**

   ```env
   AGENT_LOGS_ENABLED=true
   AGENT_DB_ENABLED=true
   AGENT_CODE_ENABLED=true
   ```

   После изменения `.env`: `docker compose up -d --build`.

5. **Локально**

   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

   `.env` в корне репозитория.

---

## Структура проекта

```
ai-house/
├── app/
│   ├── main.py              # FastAPI
│   ├── graph_entry.py       # Вход: run_user_request → LangGraph
│   ├── config.py
│   ├── orchestration/       # LangGraph: classifier, nodes, graph, runner
│   ├── agents/              # logs / db / code
│   └── shared/
│       ├── llm.py           # Провайдеры LLM (yandex / anthropic / openai)
│       ├── tool_parser.py
│       └── connectors/      # Заглушки; замените на реальные MCP/REST
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

Агенты не импортируют друг друга; вызываются из узлов графа.

---

## Схема

```
                    Пользователь
                         │
                         ▼
                  ┌──────────────┐
                  │ LangGraph    │  keyword / llm, ветки и цепочки
                  └──────┬───────┘
                         │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
   ┌──────────┐    ┌──────────┐    ┌──────────┐
   │ Логи     │    │ БД       │    │ Код      │
   │ Graylog  │    │ Postgres │    │ GitLab   │
   └──────────┘    └──────────┘    └──────────┘
```

| Компонент | Функция |
|-----------|---------|
| **Граф** | Маршрутизация, цепочки (`logs_chain`), расследование (`investigate`), синтез |
| **Агент логов** | Graylog MCP tools: `search_messages`, `aggregate_messages`, `list_streams`, `list_inputs` |
| **Агент БД** | Read-only SQL через MCP |
| **Агент кода** | GitLab REST |

---

## Коннекторы

В `app/shared/connectors/` лежат клиентские обвязки, а сами MCP-серверы можно держать в `app/mcp_servers/`.

---

## Другие стеки (опционально)

Паттерн «узел графа = вызов агента» можно сочетать с другими библиотеками; интерфейс `run(message, context)` в агентах сохранён намеренно.

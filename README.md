# ai-house

Сервис с **LangGraph** и специализированными агентами (логи / БД / GitLab). Один процесс, запуск через Docker Compose.

## Как пользоваться

1. **Настройте `.env`** — ключи LLM (`LLM_PROVIDER` и связанные переменные), при необходимости интеграции: Graylog, Postgres, GitLab. Включите нужных агентов флагами `AGENT_*_ENABLED`.
2. **Запустите** `docker compose up -d` — API на порту **8020** (см. ниже).
3. **Отправляйте задачу текстом** — `POST /api/chat` с `{"message": "..."}` или OpenWebUI на порту **3000**, если сервис поднят в compose.
4. **Маршрут** выбирается автоматически:
   - `GRAPH_ROUTER=keyword` — эвристика по ключевым словам в `app/orchestration/classifier.py`;
   - `GRAPH_ROUTER=llm` — классификация через LLM (нужен рабочий LLM).
5. **Новые сценарии** добавляются в граф: узлы и рёбра в `app/orchestration/graph.py`, правила — в `classifier.py` (и при `llm` — в промпт классификатора).

Устаревшее имя переменной `ORCHESTRATOR_ROUTER` по-прежнему читается, если `GRAPH_ROUTER` не задан.

### Общие вопросы (приветствия, мелкий small talk)

При **keyword**-маршрутизации запрос без явных признаков логов, БД, кода и расследования классифицируется как **`general`** и обрабатывается **агентом общих вопросов** (`app/agents/general/`), только через LLM — без Graylog/Postgres/GitLab.

- Включение: `AGENT_GENERAL_ENABLED=true` (по умолчанию включён). Если выключить, такие запросы перенаправятся к первому доступному специалисту или в ветку «неизвестно».
- Статус в `GET /api/status`: поле `general` — `disabled` | `ok` | `нужен LLM`.

---

### Маршрутизация (LangGraph)

| Путь в коде | Назначение |
|-------------|------------|
| `app/orchestration/graph.py` | Узлы (агенты, расследование, синтез) и условные переходы |
| `app/orchestration/classifier.py` | `keyword` или `llm`, маршруты: `logs`, `db`, `code`, `logs_chain`, `investigate`, `investigate_db_logs`, `general`, … |

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
   AGENT_GENERAL_ENABLED=true
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
| **Агент логов** | Инструменты Graylog (после реализации коннектора) |
| **Агент БД** | Read-only SQL через MCP |
| **Агент кода** | GitLab REST |

---

## Коннекторы

В `app/shared/connectors/` сейчас **заглушки**. Реализуйте `connect` / `call_tool` под ваши MCP или HTTP API; при необходимости добавьте `mcp` в `requirements.txt`.

---

## Другие стеки (опционально)

Паттерн «узел графа = вызов агента» можно сочетать с другими библиотеками; интерфейс `run(message, context)` в агентах сохранён намеренно.

# ai-house

Сервис с **LangGraph** и специализированными агентами (логи / БД / GitLab). Один процесс, запуск через Docker Compose.

## Как пользоваться

1. **Настройте `.env`** — ключи LLM (`LLM_PROVIDER` и связанные переменные), при необходимости интеграции: Graylog MCP, Postgres MCP, GitLab MCP. Для GitLab укажите `GITLAB_URL` и `GITLAB_TOKEN`. Включите нужных агентов флагами `AGENT_*_ENABLED`.
2. **Запустите** `docker compose up -d` — API на порту **8020** (см. ниже).
3. **Отправляйте задачу текстом** — `POST /api/chat` с `{"message": "..."}` или OpenWebUI на порту **3000**, если сервис поднят в compose.
4. **Маршрут** и порядок шагов выбирает LLM-оркестратор: он решает, кого вызвать дальше (`db`, `logs`, `code`) и когда ответить сам или завершать через `synthesize`.
5. **Новые сценарии** добавляются в граф и промпт оркестратора: основная логика живёт в `app/orchestration/graph.py` и `app/orchestration/supervisor.py`.

### Общие вопросы (приветствия, мелкий small talk)

Если оркестратор считает, что внешние системы не нужны, он отвечает пользователю **сам**, без отдельного агента общих вопросов. Это убирает лишний цикл `supervisor -> general -> supervisor` и делает ответы на общие вопросы стабильнее.

---

## Слои системы

1. **HTTP/API слой**  
   `app/main.py` принимает `/api/chat`, `/api/status` и OpenAI-совместимый `/v1/chat/completions`.

2. **Оркестратор**  
   `app/orchestration/supervisor.py` выбирает следующий шаг: `db`, `logs`, `code` или `finish`.

3. **Специалисты**  
   `app/agents/db`, `app/agents/logs`, `app/agents/code` получают только узкую задачу и узкий контекст.

4. **Коннекторы**  
   `app/shared/connectors/*` поднимают MCP-клиенты через stdio и передают вызовы в MCP-серверы.  
   - Graylog -> MCP.
   - Postgres -> MCP.
   - GitLab -> MCP.

5. **Внешние системы**  
   Реальные источники фактов: Graylog, Postgres, GitLab.

6. **Синтез**  
   `app/orchestration/nodes.py::node_synthesize` собирает финальный ответ по последним результатам специалистов.

---

## Оркестрация: предсказуемый флоу

Ниже — **как устроен один пользовательский запрос** через граф и **зачем** так сделано. Цель — избежать типичных отказов мультиагентных систем: бесконечные циклы делегирования, противоречивая сводка из десятка попыток и «умная» модель, которая честно пересказывает хаос в состоянии.

### Поток (один HTTP-запрос к `/api/chat` или `/v1/chat/completions`)

1. **Supervisor (LLM)** читает вопрос пользователя и **сжатое** состояние: последние результаты БД / логов / кода (без истории всех прошлых вызовов одной строкой).
2. Решение в JSON: `next` ∈ `db` | `logs` | `code` | `finish`, плюс при делегировании — поля `task` и `context_hint`.
3. **Детерминированные политики** (код, не только промпт):
   - **Лимит шагов** `GRAPH_SUPERVISOR_MAX_STEPS` — аварийный предел; при превышении — переход к сводке с флагом «лимит шагов».
   - **Лимит вызовов одного специалиста** за запрос (`MAX_SPECIALIST_INVOCATIONS_PER_TURN` в `app/orchestration/specialist_outcome.py`, по умолчанию 6) — страховка от «пилы» оркестратора, если модель упорно выбирает тот же домен.
   - **Дедупликация delegate**: если специалист уже **успешно** ответил для того же отпечатка `(user_message + task + context_hint)`, повторный `delegate` в тот же домен с тем же отпечатком принудительно превращается в `finish` (см. `delegate_fingerprint` в `specialist_outcome.py`).
4. Узел **специалиста** выполняет `run(task, context)` и записывает результат в слот `*_result` **целиком заменяя** предыдущее значение (last-write-wins для слота).
5. При **успехе** (ответ не классифицируется как сбой инструмента/конфига) в состоянии сохраняется **отпечаток успешной задачи** для этого домена (`logs_success_fingerprint` и аналоги).
6. Цикл 1–5 до `finish`, затем узел **synthesize** собирает финальный ответ для пользователя.

### Как должен работать запрос

| Тип запроса | Ожидаемый путь |
|---|---|
| Только логи | `supervisor -> logs -> synthesize` |
| Только БД | `supervisor -> db -> synthesize` |
| Только код | `supervisor -> code -> synthesize` |
| БД + логи | `supervisor -> db -> logs -> synthesize` |
| БД + логи + код | `supervisor -> db -> logs -> code -> synthesize` |

Правило простое: сначала берём **минимальный источник фактов**, потом расширяем цепочку только если текущих данных недостаточно.

### Обоснование (опыт типовых мультиагентных и RAG-систем)

| Идея | Откуда практика | Что делаем в ai-house |
|------|-----------------|------------------------|
| **Single-writer / last observation** | В агентных фреймворках и в отчётах об ошибках «команд агентов» часто ломается именно **накопление сырых трасс** в одном поле: сводочная модель смешивает устаревшие ошибки и новые успехи. Рекомендуют явное **состояние наблюдения** и перезапись актуального снимка. | Слоты `db_result` / `logs_result` / `code_result` **не склеиваются** через `--- этап ---`; хранится последний результат специалиста за запрос. |
| **Явные критерии остановки планировщика** | ReAct / planner: останов по «цель достигнута» или по лимиту итераций; иначе LLM склонен продолжать «на всякий случай». | Промпт STOP POLICY + **дедуп успешного delegate** + **жёсткий счётчик** вызовов домена. |
| **Разделение «ошибка инструмента» и «данные продукта»** | В support-ботах путают HTTP 5xx коннектора с содержимым логов; нужна **классификация** наблюдений. | `looks_like_specialist_failure` в `specialist_outcome.py`; при неуспехе отпечаток успеха сбрасывается, чтобы разрешить повтор после починки. |
| **Graceful degradation при лимите** | При обрезании по шагам пользователь должен понимать, что ответ **неполный по политике**, а не «модель решила». | `supervisor_truncated` → сводка добавляет явное пояснение про лимит шагов. |

Кодовые точки: `app/orchestration/supervisor.py`, `app/orchestration/nodes.py`, `app/orchestration/specialist_outcome.py`, `app/orchestration/state.py`, `app/orchestration/prompts.py`, `app/orchestration/runner.py`.

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
│   ├── orchestration/       # LangGraph: supervisor, nodes, graph, runner, agent_registry, scenarios
│   ├── agents/              # logs / db / code
│   ├── mcp_servers/         # Локальные MCP-серверы для внешних систем
│   └── shared/
│       ├── llm.py           # Провайдеры LLM (yandex / anthropic / openai)
│       ├── tool_parser.py
│       └── connectors/      # MCP-клиенты для Graylog / Postgres / GitLab
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
                  │ LangGraph    │  supervisor → специалист → supervisor → synthesize
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
| **Граф** | LLM-supervisor выбирает специалиста (`db` / `logs` / `code`) или `finish`; узел `synthesize` собирает финальный ответ |
| **Агент логов** | Graylog MCP: `search_messages`, `aggregate_messages`, `list_streams`, `list_inputs`. Агрегация только через API Graylog; для `aggregate_messages` нужно поле, пригодное для terms (часто `logger` / `source`), иначе будет ошибка от ES без скрытых обходных запросов. |
| **Агент БД** | Postgres MCP tools: `query`, `list_tables`, `describe_table` |
| **Агент кода** | GitLab MCP tools: `gitlab_list_projects`, `gitlab_get_file` |

---

## Активный Postgres-агент

Postgres-агент уже не stub:

- читает `POSTGRES_MCP_DSN`, `POSTGRES_URL` или `POSTGRES_DSN`;
- поднимается как отдельный MCP-сервер `app.mcp_servers.postgres`;
- даёт read-only инструменты `query`, `list_tables`, `describe_table`;
- выполняет только `SELECT`, `WITH`, `VALUES`, `EXPLAIN`;
- отсекает DML/DDL и несколько SQL-операций в одном запросе;
- возвращает JSON с `sql`, `executed_sql`, `row_count`, `columns` и `rows`.

Это позволяет использовать его как первый шаг расследования: получить `user_id`, статус, привязанные сущности и только потом идти в логи или код.

---

## Коннекторы

В `app/shared/connectors/` лежат MCP-клиенты.  
В `app/mcp_servers/` лежат локальные MCP-серверы для Graylog, Postgres и GitLab.

---

## Канонический SMS-сценарий

Сценарий для проверок лежит в `app/orchestration/scenarios/sms_delivery.md` и уже участвует в подсказке оркестратора.

Ожидаемый путь:

1. **БД** — найти пользователя по телефону или другому идентификатору, проверить `is_blocked`, активность и связанный `user_id`.
2. **Логи** — найти события отправки SMS по `user_id`, `phone`, `message_id`, `provider` и времени.
3. **Логи с ошибкой** — открыть детали ошибки и понять внешний провайдер, валидацию или таймаут.
4. **Код** — подключать только если логи указывают на баг приложения или повторяющийся внутренний stack trace.
5. **Синтез** — ответить, была ли отправка, что сломалось, и где именно подтверждена причина.

---

## Примеры запросов

| Запрос | Что должно произойти |
|---|---|
| `проверь, активен ли пользователь с телефоном +79990000000` | Только `db`: MCP `query` на `users`, вернуть статус, блокировку, связанный `user_id`. |
| `покажи ошибки отправки SMS по пользователю 123 за сутки` | Сначала `db`, потом `logs`; orchestrator передаст в `db`/`logs` краткие идентификаторы через `context_hint`. |
| `почему пользователю не приходят SMS` | `db -> logs`, а `code` только если в логах есть stack trace или явный баг приложения. |
| `покажи конкретный stack trace по ошибке отправки` | Сначала `logs`, затем `code`, если trace указывает на репозиторий или метод. |
| `какие есть таблицы для пользователей и уведомлений` | Только `db`: MCP `list_tables` и при необходимости `describe_table`. |

Паттерн «узел графа = вызов агента» можно сочетать с другими библиотеками; интерфейс `run(message, context)` в агентах сохранён намеренно.

"""Сборка промптов оркестратора и синтеза."""

from __future__ import annotations

from app.orchestration.scenarios import load_scenarios_text
from app.orchestration.state import GraphState


def summarize_state(state: GraphState, limit: int = 6000) -> str:
    def clip(value: str) -> str:
        text = (value or "").strip()
        if len(text) <= limit:
            return text or "(пусто)"
        return text[: limit - 20] + "\n…(обрезано)"

    return (
        f"## БД\n{clip(state.get('db_result', ''))}\n\n"
        f"## Логи\n{clip(state.get('logs_result', ''))}\n\n"
        f"## Код\n{clip(state.get('code_result', ''))}"
    )


def build_supervisor_system_prompt(allowed_roles: list[str]) -> str:
    allowed_str = ", ".join(allowed_roles) if allowed_roles else "general"
    scenarios = load_scenarios_text()
    base = (
        "Ты оркестратор мультиагентной поддержки. Ты один видишь полный вопрос пользователя и полные результаты "
        "прошлых шагов; специалисты — нет: им нельзя пересылать сырой вопрос целиком и полные дампы других систем.\n"
        f"Доступные специалисты (только из списка): {allowed_str}. Ещё вариант: finish — хватит данных, финальный ответ "
        "соберёт другой узел.\n"
        "На каждом шаге (кроме finish) ты обязан сформулировать:\n"
        "- task — одно чёткое техническое задание для выбранного специалиста. Без отвлечений про другие сервисы, "
        "без пересказа вопроса пользователя, только инструкция исполнителю.\n"
        "- context_hint — коротко (до ~1–2 предложений) только те факты из уже полученных результатов, без которых "
        "специалист не справится. Если ничего не нужно — пустая строка.\n"
        "Правила:\n"
        "- Не вызывай специалиста без нужды.\n"
        "- Общий smalltalk — general.\n"
        "- Если данных достаточно, выбирай finish.\n"
        "- Code нужен только когда БД/логи уже не объясняют проблему, а в данных есть признаки бага приложения.\n"
    )
    if scenarios:
        base += "\nСценарии расследования:\n" + scenarios + "\n"
    base += (
        "\nОтветь только JSON без markdown:\n"
        '{"next":"db|logs|code|general|finish","task":"...","context_hint":"...","reason":"кратко"}\n'
        "При next=finish поля task и context_hint оставь пустыми строками.\n"
    )
    return base


def build_synthesize_system_prompt() -> str:
    return (
        "Ты ведущий инженер поддержки. Специалисты уже отработали под управлением оркестратора; "
        "ниже их результаты (часть блоков может быть пуста). Сведи всё в один связный ответ на языке "
        "пользователя: статус, причины, выводы. Не выдумывай факты, опирайся только на данные выше."
    )

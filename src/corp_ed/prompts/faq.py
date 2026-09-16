from corp_ed.domain.types import ChunkMatch
from corp_ed.llm.types import Message, Role


def _format_matches(matches: list[ChunkMatch]) -> str:
    return "\n\n".join(
        f"Выдержка {number}:\n{match.content}"
        for number, match in enumerate(matches, start=1)
    )


def build_faq_messages(question: str, matches: list[ChunkMatch]) -> list[Message]:
    return [
        Message(
            role=Role.SYSTEM,
            content=(
                "Ты помощник стажёра, который адаптируется в новой компании. "
                "Отвечай на вопрос стажёра исключительно по приведённым выдержкам "
                "из материалов его компании.\n"
                "Если ответа в выдержках нет, честно скажи, что не знаешь, "
                "и предложи спросить руководителя.\n"
                "Не додумывай и не опирайся на общие знания: даже если ты знаешь "
                "ответ, но его нет в выдержках, считай, что ответа нет.\n"
                "В конце ответа укажи номера выдержек, на которых он основан."
            ),
        ),
        Message(
            role=Role.USER,
            content=(
                f"Вопрос: {question}\n\n"
                f"Выдержки из материалов компании:\n\n{_format_matches(matches)}"
            ),
        ),
    ]

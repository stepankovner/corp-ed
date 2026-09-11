from corp_ed.domain.models import Brief
from corp_ed.llm.types import Message, Role


def build_program_messages(brief: Brief) -> list[Message]:
    return [
        Message(
            role=Role.SYSTEM,
            content=(
                "Ты методист по адаптации новых сотрудников. "
                "Составляешь программу адаптации стажёра на 30 дней. "
                "Программа должна покрывать все четыре недели: "
                "Неделя 1, 2, 3, 4. Каждую опиши полностью. "
                "Формат ответа: программа разбита по неделям, для каждой "
                "недели указаны цели, конкретные задачи и критерии того, "
                "что неделя пройдена. Отвечай на русском языке, без "
                "вступлений и пояснений — только сама программа."
            ),
        ),
        Message(
            role=Role.USER,
            content=(
                f"Направление: {brief.track.value}\n"
                f"Должность: {brief.role_title}\n"
                f"Уровень стажёра: {brief.intern_level}\n"
                f"Цели стажировки: {brief.goals}\n"
                f"Задачи: {brief.tasks}\n\n"
                "Составь программу адаптации на 30 дней."
            ),
        ),
    ]

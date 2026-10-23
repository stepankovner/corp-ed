"""Версии промптов и моделей в метрике corp_ed_ml_build_info (BH-49).

Одна строка, значения — константы ML и настройки: на графике видно, с
какой выкатки поехало. Модели шага решения (BH-46) — пусто, пока схема
не встроена.
"""

from corp_ed.core.config import LLMSettings, RagSettings
from corp_ed.core.metrics import ML_BUILD
from corp_ed.prompts.decision import (
    CONTACT_NOTE_VERSION,
    DECISION_PROMPT_VERSION,
    EDITION_ANSWER_PROMPT_VERSION,
)
from corp_ed.prompts.faq import (
    PROMPT_VERSION,
    SEARCH_PROMPT_VERSION,
    SMALL_TALK_PROMPT_VERSION,
)


def ml_build(llm: LLMSettings, rag: RagSettings) -> dict[str, str]:
    return {
        "decision_prompt": DECISION_PROMPT_VERSION,
        "answer_prompt": PROMPT_VERSION,
        "faq_prompt": SEARCH_PROMPT_VERSION if rag.faq_search_step else PROMPT_VERSION,
        "general_prompt": SMALL_TALK_PROMPT_VERSION if rag.faq_small_talk else "",
        "edition_prompt": EDITION_ANSWER_PROMPT_VERSION,
        "contact_prompt": CONTACT_NOTE_VERSION,
        "embedding_model": f"{llm.embedding_model}@{llm.embedding_dim}",
        "answer_model": llm.llm_model,
        "decision_model_cloudru": "",
        "decision_model_mws": "",
        "decision_effort": "",
    }


def publish_ml_build(llm: LLMSettings, rag: RagSettings) -> None:
    ML_BUILD.info(ml_build(llm, rag))

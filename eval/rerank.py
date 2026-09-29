"""M3: реранкер — кросс-энкодер поверх векторного поиска (эксперимент стенда).

    python -m eval.bench ... --rerank [--rerank-depth 30] [--rerank-max-length 512]
    python -m eval.offline_e2e ... --rerank [--rerank-depth 30]

Вектор берёт top-N кандидатов (N = --rerank-depth), кросс-энкодер читает
пару «вопрос — фрагмент» целиком и ставит балл; кандидаты сортируются по
баллу, в выдачу идут первые k. В офлайн-e2e переупорядочиваются только
кандидаты, прошедшие порог по расстоянию (как в продукте: порог — по
вектору, реранкер выбирает лучшие k из прошедших).

Модель по умолчанию — BAAI/bge-reranker-v2-m3 (многоязычная, CPU). Пакет
sentence-transformers в зависимости проекта не входит: стенд запускают из
отдельного окружения (sandbox/.venv-rerank). Баллы кэшируются в
eval/.cache/rerank.json — повторные прогоны не пересчитывают пары.
"""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DEFAULT_RERANKER = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEPTH = 30
DEFAULT_MAX_LENGTH = 512
CACHE_PATH = Path("eval/.cache/rerank.json")
SAVE_EVERY = 100
"""Сохранять кэш баллов каждые SAVE_EVERY новых пар."""

RerankText = Literal["embed", "llm"]
"""Что показываем реранкеру: embed — крошки «Документ > Раздел» + текст
(то же, что видит эмбеддер), llm — только текст фрагмента."""


def rerank_ranking(ranking: Sequence[int], scores: Sequence[float]) -> list[int]:
    """Кандидаты по убыванию балла; при равных баллах — исходный порядок."""
    if len(ranking) != len(scores):
        raise ValueError("ranking и scores разной длины")
    order = sorted(range(len(ranking)), key=lambda i: (-scores[i], i))
    return [ranking[i] for i in order]


def rerank_candidates(
    ranking: Sequence[int],
    distance_of: dict[int, float],
    score: Callable[[Sequence[int]], list[float]],
    *,
    depth: int,
    max_distance: float | None = None,
) -> list[int]:
    """Переупорядочить первые depth кандидатов; остальные — следом, как были.

    max_distance — порог продукта: реранкер видит только прошедших его,
    не прошедшие идут после них в исходном порядке (в e2e их всё равно
    отрежет relevant_matches).
    """
    head = list(ranking[:depth])
    passed = [
        i
        for i in head
        if max_distance is None or (i in distance_of and distance_of[i] <= max_distance)
    ]
    rest = [i for i in head if i not in set(passed)] + list(ranking[depth:])
    if not passed:
        return list(ranking)
    return rerank_ranking(passed, score(passed)) + rest


@dataclass
class CachedReranker:
    """Кросс-энкодер sentence-transformers с кэшем баллов на диске."""

    model: str = DEFAULT_RERANKER
    max_length: int = DEFAULT_MAX_LENGTH
    cache_path: Path | None = CACHE_PATH
    batch_size: int = 16
    # Модели со своей архитектурой в репозитории (gte-multilingual-reranker):
    # код с Hugging Face исполняется — включать только после его просмотра.
    trust_remote_code: bool = False
    # int8 для линейных слоёв: быстрее на CPU, баллы чуть другие — свой кэш.
    quantize: bool = False
    scored_pairs: int = 0
    scoring_ms: float = 0.0
    _encoder: Any = None
    _cache: dict[str, float] = field(default_factory=dict)
    _loaded: bool = False
    _saved_at: int = 0

    def _key(self, query: str, passage: str) -> str:
        raw = f"{self.model}|{self.max_length}|{query}|{passage}"
        if self.quantize:
            raw += "|int8"
        return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.cache_path is not None and self.cache_path.exists():
            self._cache.update(json.loads(self.cache_path.read_text(encoding="utf-8")))

    def save(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")

    def _model(self) -> Any:
        if self._encoder is None:
            from sentence_transformers import CrossEncoder

            self._encoder = CrossEncoder(
                self.model,
                max_length=self.max_length,
                device="cpu",
                trust_remote_code=self.trust_remote_code,
            )
            if self.trust_remote_code:
                rebuild_runtime_buffers(self._encoder.model)
            if self.quantize:
                quantize_int8(self._encoder.model)
        return self._encoder

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Баллы пар «вопрос — фрагмент»; из кэша — без вызова модели."""
        self._load_cache()
        keys = [self._key(query, passage) for passage in passages]
        missing = [i for i, key in enumerate(keys) if key not in self._cache]
        if missing:
            started = time.perf_counter()
            values = self._model().predict(
                [(query, passages[i]) for i in missing], batch_size=self.batch_size
            )
            self.scoring_ms += (time.perf_counter() - started) * 1000
            self.scored_pairs += len(missing)
            for i, value in zip(missing, values, strict=True):
                self._cache[keys[i]] = float(value)
            # Долгий прогон на CPU не должен терять посчитанное при обрыве.
            if self.scored_pairs - self._saved_at >= SAVE_EVERY:
                self.save()
                self._saved_at = self.scored_pairs
        return [self._cache[key] for key in keys]


def quantize_int8(model: Any) -> None:
    """Динамическая int8-квантизация линейных слоёв (CPU), на месте.

    На месте, а не присваиванием: в sentence-transformers 6
    `CrossEncoder.model` — свойство, и присваивание подменяет модуль.
    """
    import torch

    torch.quantization.quantize_dynamic(
        model, {torch.nn.Linear}, dtype=torch.qint8, inplace=True
    )


def rebuild_runtime_buffers(model: Any) -> None:
    """Пересчитать буферы, которых нет в весах, у модели со своим кодом.

    transformers 5 создаёт модель на meta-устройстве и не заполняет
    непостоянные буферы из чужого кода: `position_ids` и таблицы RoPE
    остаются мусором (gte-multilingual-reranker падал с IndexError).
    """
    import torch

    def extended_attention_mask(
        self: Any, attention_mask: Any, input_shape: Any, *args: Any, **kwargs: Any
    ) -> Any:
        # Метод PreTrainedModel из transformers 4, в 5 его нет.
        dtype = torch.float32
        mask = attention_mask[:, None, None, :].to(dtype)
        return (1.0 - mask) * torch.finfo(dtype).min

    for module in model.modules():
        if type(module).__name__ == "NewModel" and not hasattr(
            module, "get_extended_attention_mask"
        ):
            type(module).get_extended_attention_mask = extended_attention_mask
        buffers = dict(module.named_buffers(recurse=False))
        if "position_ids" in buffers:
            module.position_ids = torch.arange(buffers["position_ids"].numel())
        if hasattr(module, "_set_cos_sin_cache") and hasattr(module, "base"):
            dim = module.dim
            module.inv_freq = 1.0 / (
                module.base ** (torch.arange(0, dim, 2).float() / dim)
            )
            module._set_cos_sin_cache(
                seq_len=module.max_position_embeddings,
                device=torch.device("cpu"),
                dtype=torch.float32,
            )


def make_reranker(model: str, max_length: int) -> CachedReranker:
    """Точка подмены в тестах."""
    return CachedReranker(model=model, max_length=max_length)


def config_suffix(
    depth: int, max_length: int, text: RerankText, *, quantize: bool = False
) -> str:
    return (
        f"-rr{depth}"
        + (f"-L{max_length}" if max_length != DEFAULT_MAX_LENGTH else "")
        + ("-llmtext" if text == "llm" else "")
        + ("-int8" if quantize else "")
    )

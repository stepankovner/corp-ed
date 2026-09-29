"""BM25 для офлайн-стенда: полнотекстовая ветка без PostgreSQL.

Зачем: оценить гибрид (M1) и нарезки без ключей Яндекса и без бэкенда.
Токенизация повторяет словарь 'russian' в PostgreSQL: те же стоп-слова
Snowball и тот же стеммер Snowball (snowballstemmer). Ранжирование —
Okapi BM25, а не ts_rank_cd: в проде цифры будут другими, но порядок
величины и направление эффекта стенд показывает. Если eval покажет,
что полнотекстовой ветки мало, настоящий BM25 в PostgreSQL даёт
расширение ParadeDB pg_search (отдельное решение, см. ТЗ M1).
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from functools import lru_cache

import snowballstemmer

_WORD = re.compile(r"\w+", re.UNICODE)

# Стоп-слова Snowball для русского — те же, что в словаре PostgreSQL russian.
_STOPWORDS_TEXT = """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за
    бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну
    вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
    потом себя ничего ей может они тут где есть надо ней для мы тебя их чем
    была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того
    потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
    сейчас были куда зачем всех никогда можно при наконец два об другой хоть
    после над больше тот через эти нас про всего них какая много разве три
    эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой
    им более всегда конечно всю между
"""
RUSSIAN_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())

_stemmer = snowballstemmer.stemmer("russian")


@lru_cache(maxsize=200_000)
def _stem(word: str) -> str:
    return str(_stemmer.stemWord(word))


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(text.casefold().replace("ё", "е")):
        if word in RUSSIAN_STOPWORDS:
            continue
        tokens.append(_stem(word))
    return tokens


class BM25Index:
    def __init__(
        self, documents: Sequence[str], *, k1: float = 1.2, b: float = 0.75
    ) -> None:
        self._k1 = k1
        self._b = b
        self._docs = [Counter(tokenize(text)) for text in documents]
        self._lengths = [sum(counts.values()) for counts in self._docs]
        self._avg_length = (
            sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        )
        frequency: Counter[str] = Counter()
        for counts in self._docs:
            frequency.update(counts.keys())
        n = len(self._docs)
        self._idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for term, df in frequency.items()
        }

    def scores(self, query: str) -> list[float]:
        terms = [term for term in tokenize(query) if term in self._idf]
        result: list[float] = []
        for counts, length in zip(self._docs, self._lengths, strict=True):
            norm = self._k1 * (1 - self._b + self._b * length / (self._avg_length or 1))
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self._idf[term] * tf * (self._k1 + 1) / (tf + norm)
            result.append(score)
        return result

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        """Индексы документов и скоры, по убыванию; нулевые скоры не возвращаются."""
        scored = [(i, s) for i, s in enumerate(self.scores(query)) if s > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

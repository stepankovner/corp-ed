from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UsageResponse(BaseModel):
    """Расход пула кредитов компании за текущий месяц."""

    model_config = ConfigDict(from_attributes=True)

    period_start: datetime
    period_end: datetime
    seats: int
    credits_per_seat: int
    pool: int
    used: int
    remaining: int
    exhausted: bool
    warn_at_percent: int
    warning: bool
    """Потрачено не меньше warn_at_percent пула (или пул исчерпан): фронт
    показывает администратору плашку на всех экранах."""

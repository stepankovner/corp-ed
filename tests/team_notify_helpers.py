"""Поддельный канал уведомлений команде для тестов (П-5)."""


class RecordingNotifier:
    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    def notify(self, text: str) -> None:
        self.sent.append(text)

"""append-only 领域事件存储与版本语义。

每个聚合 (aggregate_type, aggregate_id) 的事件 version 必须从 1 开始、逐 1 递增；
事件一经 append 即不可原地修改或删除，业务更正只能追加后继版本事件。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .validator import validate_event


class EventStoreError(ValueError):
    """追加事件违反信封、版本或幂等约束时抛出。"""


@dataclass
class EventStore:
    _events: list[dict] = field(default_factory=list)
    _versions: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    _event_ids: set[str] = field(default_factory=set)

    def append(self, event: dict) -> dict:
        errors = validate_event(event)
        if errors:
            raise EventStoreError("；".join(errors))

        event_id = event["event_id"]
        if event_id in self._event_ids:
            raise EventStoreError(f"event_id 重复：{event_id}")

        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions[key] + 1
        if event["version"] != expected:
            raise EventStoreError(
                f"聚合 {key} 的版本必须为 {expected}（收到 {event['version']}）；"
                "事件不得跳号、重放或原地改写"
            )

        stored = dict(event)
        self._events.append(stored)
        self._versions[key] = expected
        self._event_ids.add(event_id)
        return stored

    def append_many(self, events: list[dict]) -> list[dict]:
        return [self.append(e) for e in events]

    def all_events(self) -> list[dict]:
        return list(self._events)

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        return [
            e for e in self._events
            if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id
        ]

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions[(aggregate_type, aggregate_id)]

    def reset(self) -> None:
        self._events.clear()
        self._versions.clear()
        self._event_ids.clear()

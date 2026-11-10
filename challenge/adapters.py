from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .validation import find_overlaps, validate_walk


@dataclass
class ImportResult:
    ok: bool
    id: str
    error: str = None
    details: dict = field(default_factory=dict)


class WalkingAdapter(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def get_capabilities(self) -> dict:
        """{supportsWorkouts, supportsRoutes, supportsSimulatedData}"""

    @abstractmethod
    def submit_walk(self, walk) -> ImportResult: ...

    @abstractmethod
    def verify_import(self, walk_id) -> ImportResult: ...


class MockPacerAdapter(WalkingAdapter):
    """Локальный приёмник: ведёт себя как строгая платформа.
    distance_factor < 1 имитирует ситуацию «приложение показывает меньшую дистанцию»."""

    def __init__(self, distance_factor=1.0):
        self.distance_factor = distance_factor
        self.store = {}
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def get_capabilities(self):
        return {"supportsWorkouts": True, "supportsRoutes": True, "supportsSimulatedData": True}

    def submit_walk(self, walk):
        if not self.connected:
            return ImportResult(False, walk.id, "not_connected")
        if walk.id in self.store:
            return ImportResult(False, walk.id, "duplicate_id: повторный импорт отклонён")
        errs = validate_walk(walk)
        if errs:
            return ImportResult(False, walk.id, "rejected", {"errors": errs})
        known = [rec["walk"] for rec in self.store.values()]
        if find_overlaps(known + [walk]) and any(walk.id in pair for pair in find_overlaps(known + [walk])):
            return ImportResult(False, walk.id, "time_conflict: пересечение по времени с другой тренировкой")
        self.store[walk.id] = {"walk": walk, "sha256": walk.sha256(),
                               "shown_distance_m": walk.distance_m * self.distance_factor}
        return ImportResult(True, walk.id, details={"stored": True})

    def verify_import(self, walk_id):
        rec = self.store.get(walk_id)
        if rec is None:
            return ImportResult(False, walk_id, "not_found")
        w = rec["walk"]
        diff = w.distance_m - rec["shown_distance_m"]
        return ImportResult(True, walk_id, details={
            "simulated": w.simulated, "source": w.source, "timestamps_kind": w.timestamps_kind,
            "sha256_match": rec["sha256"] == w.sha256(),
            "source_distance_m": round(w.distance_m, 3),
            "shown_distance_m": round(rec["shown_distance_m"], 3),
            "discrepancy_m": round(diff, 3),
        })


class OfficialHealthAdapter(WalkingAdapter):
    """Экспериментальный адаптер к Apple Health / Health Connect.

    Использует только документированные интерфейсы. Запись тренировок в эти хранилища
    делает мобильное приложение на устройстве с разрешениями пользователя; у настольной
    или серверной программы такого документированного пути нет. Поэтому адаптер честно
    сообщает об ограничении и ничего не пытается обходить."""

    def __init__(self, platform="health_connect"):
        self.platform = platform
        self.last_error = None

    def connect(self):
        self.last_error = (
            f"unsupported: {self.platform} принимает записи только от приложения на самом "
            "устройстве с явными разрешениями пользователя. Документированного способа "
            "передать симулированные GPS-тренировки с сервера нет; обход не выполняется.")
        return False

    def get_capabilities(self):
        return {"supportsWorkouts": False, "supportsRoutes": False, "supportsSimulatedData": False}

    def submit_walk(self, walk):
        return ImportResult(False, walk.id, self.last_error or "unsupported: сначала вызови connect()")

    def verify_import(self, walk_id):
        return ImportResult(False, walk_id, "unsupported: нечего проверять, импорт не выполнялся")

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

R_EARTH = 6371000.0


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


@dataclass
class Walk:
    id: str
    index: int
    seed: int
    start: datetime                      # виртуальное время начала (UTC)
    points: list = field(default_factory=list)   # (lat, lon, ele, t_offset_s)
    simulated: bool = True
    timestamps_kind: str = "virtual"
    source: str = "pacer-sim/0.1"
    geometry_source: str = "synthetic"      # synthetic | graphhopper

    @property
    def distance_m(self):
        p = self.points
        return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(p, p[1:]))

    @property
    def duration_s(self):
        return self.points[-1][3] - self.points[0][3] if self.points else 0.0

    @property
    def avg_kmh(self):
        return self.distance_m / self.duration_s * 3.6 if self.duration_s else 0.0

    @property
    def end(self):
        return self.start + timedelta(seconds=self.duration_s)

    def point_time(self, p):
        return self.start + timedelta(seconds=p[3])

    def sha256(self):
        raw = json.dumps([[round(x, 7) for x in p] for p in self.points]).encode()
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self, with_points=True):
        d = {
            "id": self.id, "index": self.index, "seed": self.seed,
            "simulated": self.simulated, "timestamps_kind": self.timestamps_kind,
            "source": self.source, "geometry_source": self.geometry_source, "sha256": self.sha256(),
            "virtual_start": self.start.isoformat().replace("+00:00", "Z"),
            "virtual_end": self.end.isoformat().replace("+00:00", "Z"),
            "distance_m": round(self.distance_m, 3), "duration_s": round(self.duration_s, 1),
            "avg_kmh": round(self.avg_kmh, 3), "n_points": len(self.points),
        }
        if with_points:
            d["points"] = [[round(p[0], 7), round(p[1], 7), round(p[2], 2), round(p[3], 1)]
                           for p in self.points]
        return d

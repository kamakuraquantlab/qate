import time


class Counter:
    def __init__(self):
        self._v: float = 0.0
        self._c: int = 0

    def add(self, val):
        self._c += 1
        self._v += val

    @property
    def avg(self) -> float:
        if self._c == 0:
            return None
        return self._v / self._c

    @property
    def cnt(self) -> int:
        return self._c

    @property
    def val(self) -> float:
        return self._v


class Stat:
    def __init__(self):
        self.start_ts = time.time()
        self.vals: dict[str, Counter] = {}

    def increment(self, name):
        self.add(name, 1)

    def add(self, name, val):
        if name not in self.vals:
            self.vals[name] = Counter()
        self.vals[name].add(val)

    def get(self, name) -> Counter:
        if name in self.vals:
            return self.vals[name]
        else:
            return Counter()

    def all_vals(self):
        ret = []
        for k, v in self.vals.items():
            ret.append((k, v.val, v.cnt, v.avg))
        return ret

    @property
    def snapshot(self) -> str:
        uptime = time.time() - self.start_ts
        text = f"uptime {uptime:.3f}\n"
        for k, v in self.vals.items():
            text += f"{k} {v.cnt} {v.val:.3f} {v.avg:.3f}\n"
        return text

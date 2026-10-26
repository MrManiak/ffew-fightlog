"""Packed columnar event store (technical plan §5.1).

Events live in typed arrays, one per column, with actor and module tables. A blob is
``zlib(len(header) + header JSON + column bytes)``. Engagement stores hold one log's
slice; fight stores add ``log_id`` and ``dup_group`` columns.
"""

import json
import struct
import sys
import zlib
from array import array

BASE_COLUMNS = (
    ("t", "q"),
    ("kind", "b"),
    ("src", "i"),
    ("tgt", "i"),
    ("module", "i"),
    ("amount", "i"),
    ("quality", "b"),
    ("flags", "h"),
    ("line_no", "i"),
)
FIGHT_COLUMNS = BASE_COLUMNS + (("log_id", "i"), ("dup_group", "i"))
STORE_VERSION = 1


class BlobTooLarge(ValueError):
    pass


class EventStore:
    def __init__(self, columns=BASE_COLUMNS, actors=None, modules=None, meta=None):
        self.columns = tuple(columns)
        self.names = [name for name, _ in self.columns]
        self.data = {name: array(code) for name, code in self.columns}
        self.actors: list[dict] = list(actors or [])
        self.modules: list[str] = list(modules or [])
        self.meta: dict = dict(meta or {})
        self._actor_index = {a["key"]: i for i, a in enumerate(self.actors)}
        self._module_index = {m: i for i, m in enumerate(self.modules)}

    def __len__(self) -> int:
        return len(self.data["t"])

    # building

    def actor_id(self, actor: dict) -> int:
        key = actor["key"]
        idx = self._actor_index.get(key)
        if idx is None:
            idx = len(self.actors)
            self.actors.append(actor)
            self._actor_index[key] = idx
        return idx

    def module_id(self, module: str | None) -> int:
        if not module:
            return -1
        idx = self._module_index.get(module)
        if idx is None:
            idx = len(self.modules)
            self.modules.append(module)
            self._module_index[module] = idx
        return idx

    def append(self, **values) -> None:
        for name in self.names:
            self.data[name].append(values.get(name, -1 if name in ("module", "dup_group") else 0))

    # reading

    def column(self, name: str) -> array:
        return self.data[name]

    def module_name(self, idx: int) -> str | None:
        return self.modules[idx] if idx >= 0 else None

    def rows(self):
        cols = [self.data[name] for name in self.names]
        return zip(*cols)

    def slice(self, start: int, end: int) -> "EventStore":
        out = EventStore(self.columns, self.actors, self.modules, self.meta)
        for name in self.names:
            out.data[name] = self.data[name][start:end]
        return out

    # (de)serialization

    def encode(self, max_bytes: int | None = None) -> bytes:
        header = json.dumps(
            {
                "v": STORE_VERSION,
                "n": len(self),
                "cols": [list(c) for c in self.columns],
                "actors": self.actors,
                "modules": self.modules,
                "meta": self.meta,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        parts = [struct.pack("<I", len(header)), header]
        for name in self.names:
            col = self.data[name]
            if sys.byteorder != "little":  # pragma: no cover - big-endian hosts
                col = array(col.typecode, col)
                col.byteswap()
            parts.append(col.tobytes())
        blob = zlib.compress(b"".join(parts), 6)
        if max_bytes is not None and len(blob) > max_bytes:
            raise BlobTooLarge(f"event store blob is {len(blob)} bytes, limit {max_bytes}")
        return blob

    @classmethod
    def decode(cls, blob: bytes) -> "EventStore":
        raw = zlib.decompress(bytes(blob))
        (header_len,) = struct.unpack_from("<I", raw, 0)
        header = json.loads(raw[4 : 4 + header_len].decode("utf-8"))
        store = cls([tuple(c) for c in header["cols"]], header["actors"], header["modules"], header.get("meta"))
        offset = 4 + header_len
        n = header["n"]
        for name, code in store.columns:
            col = array(code)
            size = col.itemsize * n
            col.frombytes(raw[offset : offset + size])
            if sys.byteorder != "little":  # pragma: no cover
                col.byteswap()
            store.data[name] = col
            offset += size
        return store

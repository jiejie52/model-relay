from __future__ import annotations
from typing import Any

def _immutable(*args, **kwargs):
    raise TypeError("Frozen execution data cannot be mutated")

class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    def __deepcopy__(self, memo): return thaw_json(self)

class FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable
    def __deepcopy__(self, memo): return thaw_json(self)

def freeze_json(value: Any) -> Any:
    if isinstance(value, dict): return FrozenDict({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, list): return FrozenList(freeze_json(v) for v in value)
    if isinstance(value, tuple): return tuple(freeze_json(v) for v in value)
    return value

def thaw_json(value: Any) -> Any:
    if isinstance(value, dict): return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, list): return [thaw_json(v) for v in value]
    if isinstance(value, tuple): return tuple(thaw_json(v) for v in value)
    return value

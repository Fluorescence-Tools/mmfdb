"""A chinet-surface runtime over IMP.bff, for the archive adapter.

``mmfdb.adapters.chinet`` stores fit parameter graphs in MMFDB. It talks
to a node-graph runtime through chinet's Python surface -- ``Node(name=)``,
``Session({key: node})``, ``Port(value=..., oid=..., ...)``, live ``ports``
dicts, ``oid`` document ids, ``to_dict()`` -- because chinet was the
runtime ChiSurf paired with MMFDB. ChiSurf's runtime is now IMP.bff
(its GraphPort/GraphNode/GraphSession read and write chinet's formats), and the vendored
chinet module is gone, so this module provides that same surface on top
of ``IMP.bff`` and the adapter falls back to it when chinet itself is not
importable.

The objects here are serialization vehicles: the adapter builds them from
a fit-state payload, reads them out into MMFDB rows, and discards them.
They are never evaluated, so the node keeps a plain Python ``ports`` dict
(chinet's) that :meth:`CompatNode.fill_input_output_port_lookups` adopts
into the underlying bff node. ``oid`` is bff's document uid. ``link``
returns the linked *Python* object, not the plain proxy SWIG would hand
back for the same C++ port, so ``port.link.oid`` keeps working.
"""
from __future__ import annotations

import uuid
from typing import Any

try:
    import IMP.bff as _bff
except ImportError:  # pragma: no cover - env without IMP
    _bff = None

__all__ = ["Port", "Node", "Session", "DB", "__version__"]

#: IMP.bff replaced chinet; the adapter records this as the software
#: version of the node-graph runtime that produced a stored session.
__version__ = getattr(_bff, "__version__", "") if _bff is not None else ""


def _new_oid() -> str:
    return str(uuid.uuid4())


class Port(_bff.GraphPort):
    """chinet's Port surface on a bff GraphPort (``oid``, None-able bounds)."""

    def __init__(
        self,
        value: Any = 0.0,
        oid: str | None = None,
        name: str = "",
        fixed: bool = False,
        is_output: bool = False,
        is_reactive: bool = False,
        is_bounded: bool = False,
        lb: float = 0.0,
        ub: float = 0.0,
        value_type: int | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            value=value,
            fixed=fixed,
            is_output=is_output,
            is_reactive=is_reactive,
            is_bounded=is_bounded,
            lb=lb,
            ub=ub,
            value_type=value_type,
            name=name,
            oid=oid,
            **kwargs,
        )
        #: The linked Python object, so ``port.link`` hands back a Port
        #: with this class's surface instead of a plain SWIG proxy.
        self._link_py = None

    oid = property(
        lambda self: self.get_uid(),
        lambda self, v: self.set_uid(str(v)),
    )

    #: chinet spelled it ``is_bounded``; bff's pythoncode layer spells the
    #: same accessors ``bounded``. The adapter reads chinet's spelling.
    is_bounded = property(
        lambda self: self.get_is_bounded(),
        lambda self, v: self.set_is_bounded(v),
    )

    @property
    def bounds(self):
        """chinet's bounds: ``(None, None)`` while not bounded."""
        if not self.get_is_bounded():
            return (None, None)
        lb, ub = self.get_lower_bound(), self.get_upper_bound()
        return (
            None if lb != lb else lb,  # NaN is bff's "no bound" marker
            None if ub != ub else ub,
        )

    def set_link(self, other):
        self._link_py = other
        super().set_link(other)

    link = property(lambda self: self._link_py, set_link)


class Node(_bff.GraphNode):
    """chinet's Node surface on a bff GraphNode: a live ``ports`` dict."""

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._ports = {}

    oid = property(
        lambda self: self.get_uid(),
        lambda self, v: self.set_uid(str(v)),
    )

    @property
    def ports(self):
        """The node's ports, by key; assignment adopts the port."""
        return self._ports

    #: chinet's input/output views over the ports dict. They return the
    #: Python port objects (not C++ map copies), so ``port.oid`` and
    #: ``port.link`` keep their chinet surface.
    @property
    def inputs(self):
        return {k: p for k, p in self._ports.items() if not p.get_is_output()}

    @property
    def outputs(self):
        return {k: p for k, p in self._ports.items() if p.get_is_output()}

    #: chinet's callback document fields, as plain attributes on bff's
    #: accessors (a bff node has no Python callback object, so
    #: ``callback_class`` is always None and the adapter records "").
    callback = property(lambda self: self.get_callback())
    callback_type_string = property(lambda self: self.get_callback_type_string())
    callback_class = None
    node_valid_ = property(
        lambda self: self.get_node_valid(),
        lambda self, v: self.set_valid(v),
    )

    def fill_input_output_port_lookups(self):
        """Adopt every port in :attr:`ports` into the underlying node."""
        for key, port in list(self._ports.items()):
            self.add_port(key, port, port.get_is_output())

    def add_port(self, key, port, is_output):
        """Record the Python port, then adopt it into the bff node."""
        self._ports[key] = port
        super().add_port(key, port, is_output)

    def add_input_port(self, key, port):
        self.add_port(key, port, False)

    def add_output_port(self, key, port):
        self.add_port(key, port, True)

class Session:
    """chinet's Session surface: named nodes plus a to_dict document."""

    def __init__(self, nodes: dict[str, Node] | None = None):
        self.name = "session"
        self.oid = _new_oid()
        self.nodes = {}
        #: chinet's BaseObject document dict, which callers mutate.
        self._document = {
            "_id": self.oid,
            "type": "session",
            "name": "session",
            "nodes": {},
        }
        for key, node in (nodes or {}).items():
            self.add_node(key, node)

    def add_node(self, key: str, node: Node):
        self.nodes[key] = node
        self._document.setdefault("nodes", {})[key] = node.oid

    def to_dict(self) -> dict[str, Any]:
        """The legacy monolithic document: session header plus objects."""
        objects: list[dict[str, Any]] = [
            {
                "_id": self.oid,
                "type": "session",
                "name": self.name,
                "nodes": {k: n.oid for k, n in self.nodes.items()},
            }
        ]
        for node_key, node in self.nodes.items():
            objects.append(
                {
                    "_id": node.oid,
                    "type": "node",
                    "name": node.get_name(),
                    "ports": {k: p.oid for k, p in node.ports.items()},
                    "callback": node.get_callback(),
                    "callback_type": node.get_callback_type_string(),
                }
            )
            for port_key, port in node.ports.items():
                value = port.value
                if hasattr(value, "tolist"):
                    value = value.tolist()
                lb, ub = port.bounds
                objects.append(
                    {
                        "_id": port.oid,
                        "type": "port",
                        "name": port.get_name(),
                        "value": value,
                        "value_type": port.get_value_type(),
                        "fixed": port.get_fixed(),
                        "is_output": port.get_is_output(),
                        "is_reactive": port.get_is_reactive(),
                        "is_bounded": port.get_is_bounded(),
                        "lb": lb,
                        "ub": ub,
                        "link": port.link.oid if port.is_linked() else "",
                    }
                )
        doc = objects[0]
        return {"session": doc, "objects": objects}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Session":
        """Rebuild from a :meth:`to_dict` document (or an objects list)."""
        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        header = payload.get("session", {}) if isinstance(payload, dict) else {}
        if not objects and isinstance(payload, dict):
            objects = [payload]
        by_id = {str(o.get("_id")): o for o in objects if isinstance(o, dict)}
        session = cls()
        if header.get("_id"):
            session.oid = str(header["_id"])
        nodes: dict[str, Node] = {}
        ports_by_id: dict[str, Port] = {}
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "node":
                node = Node(name=str(obj.get("name", "")))
                node.oid = str(obj.get("_id", _new_oid()))
                nodes[str(obj.get("_id"))] = node
                for key, port_oid in (obj.get("ports") or {}).items():
                    pdoc = by_id.get(str(port_oid), {})
                    port = Port(
                        value=pdoc.get("value", 0.0),
                        oid=str(port_oid),
                        name=str(pdoc.get("name", key)),
                        fixed=bool(pdoc.get("fixed", False)),
                        is_output=bool(pdoc.get("is_output", False)),
                        is_reactive=bool(pdoc.get("is_reactive", False)),
                        is_bounded=bool(pdoc.get("is_bounded", False)),
                        lb=float(pdoc.get("lb") or 0.0),
                        ub=float(pdoc.get("ub") or 0.0),
                        value_type=pdoc.get("value_type"),
                    )
                    ports_by_id[str(port_oid)] = port
                    node.ports[key] = port
                node.fill_input_output_port_lookups()
        for obj in objects:
            if isinstance(obj, dict) and obj.get("type") == "session":
                for key, node_oid in (obj.get("nodes") or {}).items():
                    node = nodes.get(str(node_oid))
                    if node is not None:
                        session.add_node(str(key), node)
        for port_oid, port in ports_by_id.items():
            link_oid = None
            for obj in objects:
                if isinstance(obj, dict) and str(obj.get("_id")) == port_oid:
                    link_oid = str(obj.get("link") or "") or None
                    break
            if link_oid and link_oid in ports_by_id:
                port.set_link(ports_by_id[link_oid])
        return session


class _DBStub:
    """The object registry chinet's DB was, for the adapter's fallbacks.

    No ``set_backend``/``get_backend``: those routes raise mmfdb's
    "unsupported backend" error rather than pretending to work.
    """

    def __init__(self):
        self._objects = []

    def register(self, obj):
        if obj not in self._objects:
            self._objects.append(obj)

    def iter_objects(self):
        return iter(list(self._objects))

    def has_backend(self):
        return False


DB = _DBStub()

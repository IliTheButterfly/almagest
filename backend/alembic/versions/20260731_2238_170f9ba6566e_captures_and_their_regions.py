"""captures and their regions — the still the scanner used to throw away

The live decode loop turns a frame into a payload and discards the frame. That is
right for the loop and wrong for intake: the half of a reel label that is only
*printed* — manufacturer, date code, a hand-written count — is legible to the
person holding it and to nothing else, and by the time the intake queue is
curated at a desk hours later it is gone.

Three additive changes, no `CHECK` anywhere (`sa.String` + `StrEnumType`, per
CLAUDE.md), so nothing existing moves:

- `captures` — one still, by reference to the `documents` blob that already holds
  the bytes. `text_status` distinguishes "found no text" from "never looked",
  which is the same distinction `ExtractionState.PENDING` draws for datasheets
  and for the same ADR 0005 reason: the OCR pass is allowed to be absent.
- `capture_regions` — one outline each, quad stored as eight explicit integers
  rather than JSON, because it is fixed-arity with no optional members and a
  typed non-null column is what `alembic check` can reason about.
- `pending_intakes.capture_id` — nullable, `SET NULL`. What makes deferring
  honest: the desk pass gets the photograph, not just the payload.

Revision ID: 170f9ba6566e
Revises: b4e2f1c70d38
Create Date: 2026-07-31 22:38:09.384391+00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "170f9ba6566e"
down_revision: Union[str, Sequence[str], None] = "b4e2f1c70d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `sa.String`, never the application's `StrEnumType`/`UtcDateTime`: a
    # migration must not import from `app` (see `alembic/env.py`, which renders
    # both as `sa.String`). 27 is an ISO-8601 instant, 32 a StrEnum — the lengths
    # those types render to, because `alembic check` compares rendered types.
    op.create_table(
        "captures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(length=64), nullable=True),
        sa.Column(
            "text_status", sa.String(length=32), server_default="not_attempted", nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_captures_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["scan_sources.id"],
            name=op.f("fk_captures_source_id_scan_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_captures")),
    )
    with op.batch_alter_table("captures", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_captures_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_captures_document_id"), ["document_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_captures_source_id"), ["source_id"], unique=False)

    op.create_table(
        "capture_regions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("symbology", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("scan_event_id", sa.Integer(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("x0", sa.Integer(), nullable=False),
        sa.Column("y0", sa.Integer(), nullable=False),
        sa.Column("x1", sa.Integer(), nullable=False),
        sa.Column("y1", sa.Integer(), nullable=False),
        sa.Column("x2", sa.Integer(), nullable=False),
        sa.Column("y2", sa.Integer(), nullable=False),
        sa.Column("x3", sa.Integer(), nullable=False),
        sa.Column("y3", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["capture_id"],
            ["captures.id"],
            name=op.f("fk_capture_regions_capture_id_captures"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scan_event_id"],
            ["scan_events.id"],
            name=op.f("fk_capture_regions_scan_event_id_scan_events"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture_regions")),
    )
    with op.batch_alter_table("capture_regions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_capture_regions_capture_order", ["capture_id", "order_index"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_capture_regions_scan_event_id"), ["scan_event_id"], unique=False
        )

    with op.batch_alter_table("pending_intakes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("capture_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_pending_intakes_capture_id"), ["capture_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_pending_intakes_capture_id_captures"),
            "captures",
            ["capture_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Reverse order: `pending_intakes` first because its FK names `captures`,
    # then the regions that CASCADE off it, then the captures themselves.
    with op.batch_alter_table("pending_intakes", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_pending_intakes_capture_id_captures"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_pending_intakes_capture_id"))
        batch_op.drop_column("capture_id")

    with op.batch_alter_table("capture_regions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_capture_regions_scan_event_id"))
        batch_op.drop_index("ix_capture_regions_capture_order")

    op.drop_table("capture_regions")
    with op.batch_alter_table("captures", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_captures_source_id"))
        batch_op.drop_index(batch_op.f("ix_captures_document_id"))
        batch_op.drop_index(batch_op.f("ix_captures_created_at"))

    op.drop_table("captures")

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SQLEnum

from app.db.base import Base


class Priority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class TicketStatus(str, enum.Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    ON_HOLD = "ON_HOLD"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class AISummaryStatus(str, enum.Enum):
    DISABLED = "DISABLED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TicketSource(str, enum.Enum):
    WEB = "WEB"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    WALK_IN = "WALK_IN"


class VoiceCallState(str, enum.Enum):
    GREETING = "GREETING"
    COLLECT_DESCRIPTION = "COLLECT_DESCRIPTION"
    COLLECT_NAME = "COLLECT_NAME"
    COLLECT_PHONE = "COLLECT_PHONE"
    COLLECT_EMAIL = "COLLECT_EMAIL"
    CONFIRM_EMAIL = "CONFIRM_EMAIL"
    CONFIRM_CATEGORY = "CONFIRM_CATEGORY"
    ANYTHING_ELSE = "ANYTHING_ELSE"
    ESCALATED = "ESCALATED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class EscalationReason(str, enum.Enum):
    CALLER_REQUESTED = "CALLER_REQUESTED"
    REPEATED_MISUNDERSTANDING = "REPEATED_MISUNDERSTANDING"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class VoiceCallChannel(str, enum.Enum):
    """How the call reached the orchestrator -- Twilio webhooks or the SIP gateway."""

    TWILIO = "TWILIO"
    SIP = "SIP"


# Valid forward transitions for a ticket's status. Used by TicketService to
# reject nonsensical transitions (e.g. CLOSED -> NEW) before hitting the DB.
VALID_STATUS_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.NEW: {TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.CANCELLED},
    TicketStatus.OPEN: {TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.RESOLVED, TicketStatus.CANCELLED},
    TicketStatus.IN_PROGRESS: {TicketStatus.ON_HOLD, TicketStatus.RESOLVED, TicketStatus.CANCELLED},
    TicketStatus.ON_HOLD: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CANCELLED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},
    TicketStatus.CLOSED: set(),
    TicketStatus.CANCELLED: set(),
}


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_priority: Mapped[Priority | None] = mapped_column(
        SQLEnum(Priority, name="priority_enum"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="category")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)

    caller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(CITEXT, nullable=True)

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        SQLEnum(Priority, name="priority_enum"), nullable=False, default=Priority.MEDIUM
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)

    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary_status: Mapped[AISummaryStatus] = mapped_column(
        SQLEnum(AISummaryStatus, name="ai_summary_status_enum"),
        nullable=False,
        default=AISummaryStatus.DISABLED,
    )
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[TicketStatus] = mapped_column(
        SQLEnum(TicketStatus, name="ticket_status_enum"), nullable=False, default=TicketStatus.NEW
    )
    source: Mapped[TicketSource] = mapped_column(
        SQLEnum(TicketSource, name="ticket_source_enum"), nullable=False, default=TicketSource.WEB
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    category: Mapped["Category"] = relationship(back_populates="tickets")


class VoiceCallSession(Base):
    """Per-call conversation state for the Twilio voice agent.

    Lives in Postgres rather than memory because each webhook turn is an
    independent request that may land on any API replica.
    """

    __tablename__ = "voice_call_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Holds whichever provider's unique call identifier applies -- Twilio's
    # CallSid for `channel=TWILIO`, the SIP Call-ID for `channel=SIP`. Kept
    # under its original name to avoid an API/frontend-schema break; the
    # column plays a channel-agnostic role now.
    twilio_call_sid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    channel: Mapped[VoiceCallChannel] = mapped_column(
        SQLEnum(VoiceCallChannel, name="voice_call_channel_enum"),
        nullable=False,
        default=VoiceCallChannel.TWILIO,
        server_default=VoiceCallChannel.TWILIO.value,
    )
    from_number: Mapped[str] = mapped_column(String(32), nullable=False)
    to_number: Mapped[str] = mapped_column(String(32), nullable=False)

    state: Mapped[VoiceCallState] = mapped_column(
        SQLEnum(VoiceCallState, name="voice_call_state_enum"),
        nullable=False,
        default=VoiceCallState.GREETING,
    )
    collected: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    turns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    misunderstanding_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    email_attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    escalated: Mapped[bool] = mapped_column(nullable=False, default=False)
    escalation_reason: Mapped[EscalationReason | None] = mapped_column(
        SQLEnum(EscalationReason, name="escalation_reason_enum"), nullable=True
    )

    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

import enum
import uuid
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum

class ActionType(enum.Enum):
    JD_CREATED= "JD_CREATED"
    JD_UPDATED= "JD_UPDATED"
    JD_VERSION_CREATED= "JD_VERSION_CREATED"
    JD_CLOSED= "JD_CLOSED"

class EntityType(enum.Enum):
    JOB_DESCRIPTION= "JOB_DESCRIPTION"


# class AuditLog(Base):
#     __tablename__ = "audit_log"
    
#     id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         nullable=False,
#         primary_key=True,
#         default=uuid.uuid4
#     )
    
#     actor_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         nullable=False,
#     )
    
#     action_type: Mapped[ActionType] = mapped_column(
#         SAEnum(ActionType, name="audit_action_type_enum"),
#         nullable=False,
#     )
    
#     entity_type: Mapped[EntityType] = mapped_column(
#         SAEnum(EntityType, name="audit_entity_type_enum"),
#         nullable=False,
#     )
    
#     entity_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True),
#         nullable=False,
#     )
    
#     details: Mapped[dict] = mapped_column(
#         JSONB,
#         nullable=False,
#     )

#     created_at: Mapped[datetime] = mapped_column(
#         DateTime(timezone=True),
#         server_default=func.now(),
#         nullable=False,
#     )
    
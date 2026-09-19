"""
Formal Pydantic Data Validation Model for Redis Agent-to-Agent (A2A) Messages.
Provides strict schema validation, type checking, and ISO timestamp verification.
"""

from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class A2AMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=3, description="Unique message ID (e.g. msg_1710789000_alice_9f8a)")
    from_agent: str = Field(..., alias="from", min_length=1, description="Sender agent name")
    to_agent: str = Field(..., alias="to", min_length=1, description="Recipient name, @tag, or *")
    type: Literal["task", "query", "reply", "status"] = Field(..., description="Message envelope type")
    reply_to: Optional[str] = Field(default=None, description="Original message ID for reply threading")
    tags: List[str] = Field(default_factory=list, description="Associated routing or ticket tags")
    subject: str = Field(..., min_length=1, description="Message subject line")
    body: str = Field(..., min_length=1, description="Task, query, or reply payload content")
    timestamp: str = Field(..., description="ISO-8601 timestamp string")

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        try:
            # Handles '2026-09-18T23:35:00Z' or with timezone offsets
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception as e:
            raise ValueError(f"Field 'timestamp' must be a valid ISO-8601 format: {e}")
        return v

# Locutus Wire Protocol & Schema Specification

This document provides the formal reference specification for messages transmitted across the Redis Locutus inter-assistant bus.

---

## 1. Envelope Schema (JSON)

Every message stored in an agent inbox (`${LOCUTUS_REDIS_PREFIX}inbox:<recipient>`) must be a valid JSON object matching this schema:

```json
{
  "id": "msg_1789777056_alice_83728",
  "from": "alice",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["locutus", "ticket-104"],
  "subject": "Review auth parser changes",
  "body": "Please inspect src/auth.ts and verify if token expiry handles leap years.",
  "timestamp": "2026-09-19T00:17:36Z"
}
```

### Fields

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `id` | `string` | **Yes** | Unique message identifier. Recommended format: `msg_<unix_ts>_<sender>_<random>`. |
| `from` | `string` | **Yes** | Ephemeral identity name of the sending agent. |
| `to` | `string` | **Yes** | Direct recipient name (e.g. `bob`), multicast tag (e.g. `@locutus,qa`), or global broadcast (`*`). |
| `type` | `string` | **Yes** | One of: `"task"`, `"query"`, `"reply"`, `"status"`. |
| `reply_to` | `string` \| `null` | **Yes** | ID of the previous message being replied to, or `null` if initiating a conversation. |
| `tags` | `array[string]` | **Yes** | Routing, project, or ticket tags. |
| `subject` | `string` | **Yes** | Brief human-readable summary of the payload. |
| `body` | `string` | **Yes** | Work instructions, question, or response payload. Keep under 10KB. |
| `timestamp` | `string` | **Yes** | ISO-8601 UTC timestamp string (e.g. `YYYY-MM-DDTHH:MM:SSZ`). |

---

## 2. Message Types & Communication Rules

1. **`task`**:
   - Contains an actionable request (e.g. run a test, inspect a file, implement a feature).
   - Expected response: Worker executes the work and replies unicast with `type: "reply"`.
2. **`query`**:
   - Informational question (e.g. "What branch are you on?", "Is ticket-42 ready?").
   - Expected response: Unicast answer with `type: "reply"`.
3. **`reply`**:
   - Must set `reply_to` matching the sender's original message `id`.
   - **Critical Invariant**: Replies must ALWAYS be sent directly to `from` (O2O). Never reply to group tags or `*`.
4. **`status`**:
   - Informational broadcast (e.g. "Session shutting down", "Build succeeded").
   - No reply expected.

---

## 3. Pydantic Model Reference

```python
from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LocutusMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=3, description="Unique message ID")
    from_agent: str = Field(..., alias="from", min_length=1, description="Sender agent name")
    to_agent: str = Field(..., alias="to", min_length=1, description="Recipient name, @tag, or *")
    type: Literal["task", "query", "reply", "status"] = Field(..., description="Message envelope type")
    reply_to: Optional[str] = Field(default=None, description="Original message ID for reply threading")
    tags: List[str] = Field(default_factory=list, description="Associated routing or ticket tags")
    subject: str = Field(..., min_length=1, description="Message subject line")
    body: str = Field(..., min_length=1, description="Task, query, or reply payload content")
    timestamp: str = Field(..., description="ISO-8601 timestamp string")

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v):
        if isinstance(v, dict):
            return list(v.keys())
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v or []

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception as e:
            raise ValueError(f"Field 'timestamp' must be a valid ISO-8601 format: {e}")
        return v


# Backwards compatibility alias
A2AMessage = LocutusMessage
```

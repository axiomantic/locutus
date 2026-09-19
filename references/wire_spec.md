# A2A Wire Protocol & Schema Specification

This document provides the formal reference specification for messages transmitted across the Redis Agent-to-Agent (A2A) bus.

---

## 1. Envelope Schema (JSON)

Every message stored in an agent inbox (`${A2A_REDIS_PREFIX}inbox:<recipient>`) must be a valid JSON object matching this schema:

```json
{
  "id": "msg_1710789000_alice_9f8a",
  "from": "alice",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["locutus", "ticket-104"],
  "subject": "Review auth parser changes",
  "body": "Please inspect src/auth.ts and verify if token expiry handles leap years.",
  "timestamp": "2026-09-18T23:35:00Z"
}
```

### Fields

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `id` | `string` | **Yes** | Unique message identifier. Recommended format: `msg_<unix_ts>_<sender>_<random>`. |
| `from` | `string` | **Yes** | Ephemeral identity name of the sending agent. |
| `to` | `string` | **Yes** | Direct recipient name (e.g. `bob`), multicast tag (e.g. `locutus,qa`), or global broadcast (`*`). |
| `type` | `string` | **Yes** | One of: `"task"`, `"query"`, `"reply"`, `"status"`. |
| `reply_to` | `string` \| `null` | **Yes** | ID of the previous message being replied to, or `null` if initiating a conversation. |
| `tags` | `array[string]` | **Yes** | Routing, ticket, or domain tags. |
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

class A2AMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=3)
    from_agent: str = Field(..., alias="from", min_length=1)
    to_agent: str = Field(..., alias="to", min_length=1)
    type: Literal["task", "query", "reply", "status"]
    reply_to: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    timestamp: str

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v
```

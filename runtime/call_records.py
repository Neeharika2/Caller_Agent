import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CALL_RECORDS_DIR = Path(__file__).resolve().parents[1] / "call_records"


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata"))


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")[:80] or "call"


def _detect_language(text: str) -> str:
    if re.search(r"[\u0900-\u097f]", text):
        return "Hindi/Devanagari"
    if re.search(r"\b(hai|haan|nahi|kya|kaise|mujhe|aap|mera|naam)\b", text, re.IGNORECASE):
        return "Hinglish/Hindi"
    if text.strip():
        return "English/Unknown"
    return "Unknown"


def _extract_name(text: str) -> str | None:
    patterns = [
        r"\bmy name is\s+([A-Za-z][A-Za-z .'-]{1,50})",
        r"\bi am\s+([A-Za-z][A-Za-z .'-]{1,50})",
        r"\bthis is\s+([A-Za-z][A-Za-z .'-]{1,50})",
        r"\bmera naam\s+([A-Za-z][A-Za-z .'-]{1,50})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            candidate = re.split(
                r"\s+(?:and|but|because|i\s+want|i\s+need|mujhe|mai|main)\b|[,.;!?]",
                candidate,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            return candidate.strip(" .'-") or None
    return None


def _extract_phone(text: str) -> str | None:
    match = re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", text)
    if not match:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    return phone if len(re.sub(r"\D", "", phone)) >= 8 else None


def _infer_intent(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["price", "pricing", "cost", "rate", "plan", "charge"]):
        return "pricing inquiry"
    if any(word in lowered for word in ["book", "appointment", "schedule", "meeting", "callback", "call back"]):
        return "booking or callback request"
    if any(word in lowered for word in ["support", "help", "issue", "problem", "not working"]):
        return "support request"
    if any(word in lowered for word in ["service", "product", "offer", "available", "details"]):
        return "business information inquiry"
    return "general inquiry"


def _infer_urgency(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["urgent", "immediately", "asap", "right now", "emergency"]):
        return "high"
    if any(word in lowered for word in ["today", "soon", "tomorrow", "jaldi"]):
        return "medium"
    return "low"


def _summarize(user_text: str, assistant_text: str) -> str:
    clean_user = " ".join(user_text.split())
    clean_assistant = " ".join(assistant_text.split())
    if clean_user and clean_assistant:
        return f"Caller said: {clean_user[:500]}. Assistant responded about: {clean_assistant[:300]}."
    if clean_user:
        return f"Caller said: {clean_user[:700]}."
    if clean_assistant:
        return f"Assistant spoke: {clean_assistant[:700]}."
    return "No transcript was captured for this call."


@dataclass
class CallRecord:
    source: str = "exotel"
    call_id: str | None = None
    stream_sid: str | None = None
    sample_rate_hz: int | None = None
    started_at: str = field(default_factory=lambda: _now().isoformat())
    ended_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)

    def mark_started(self, stream_sid: str | None, sample_rate_hz: int | None, metadata: dict[str, Any] | None = None):
        self.stream_sid = stream_sid or self.stream_sid
        self.call_id = self.call_id or self.stream_sid
        self.sample_rate_hz = sample_rate_hz or self.sample_rate_hz
        self.events.append(
            {
                "type": "start",
                "timestamp": _now().isoformat(),
                "stream_sid": self.stream_sid,
                "sample_rate_hz": self.sample_rate_hz,
                "metadata": metadata or {},
            }
        )

    def mark_stopped(self):
        if not self.ended_at:
            self.ended_at = _now().isoformat()
        self.events.append({"type": "stop", "timestamp": self.ended_at})

    def add_transcript(self, speaker: str, text: str):
        clean_text = " ".join((text or "").split())
        if not clean_text:
            return
        if self.transcript and self.transcript[-1]["speaker"] == speaker and self.transcript[-1]["text"] == clean_text:
            return
        self.transcript.append(
            {
                "speaker": speaker,
                "text": clean_text,
                "timestamp": _now().isoformat(),
            }
        )

    def build_lead(self) -> dict[str, Any]:
        user_text = " ".join(item["text"] for item in self.transcript if item["speaker"] == "caller")
        assistant_text = " ".join(item["text"] for item in self.transcript if item["speaker"] == "assistant")
        return {
            "caller_name": _extract_name(user_text),
            "phone": _extract_phone(user_text),
            "intent": _infer_intent(user_text),
            "language": _detect_language(user_text),
            "summary": _summarize(user_text, assistant_text),
            "next_action": "review transcript and follow up" if user_text else "no action captured",
            "urgency": _infer_urgency(user_text),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "call_id": self.call_id,
            "stream_sid": self.stream_sid,
            "sample_rate_hz": self.sample_rate_hz,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "lead": self.build_lead(),
            "transcript": self.transcript,
            "events": self.events,
        }

    def save(self) -> Path:
        if not self.ended_at:
            self.ended_at = _now().isoformat()

        CALL_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = _safe_filename(self.started_at.replace(":", "-").replace("+", "_"))
        identifier = _safe_filename(self.call_id or self.stream_sid or "unknown")
        path = CALL_RECORDS_DIR / f"{timestamp}_{identifier}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

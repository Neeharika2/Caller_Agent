import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"
LEADS_DIR = Path(__file__).resolve().parents[1] / "leads"
SUPPORTED_KNOWLEDGE_EXTENSIONS = {".md", ".txt", ".json"}


@dataclass(frozen=True)
class ToolDefinition:
    declaration: dict[str, Any]
    function: Callable[..., Any]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, declaration: dict[str, Any], function: Callable[..., Any]):
        name = declaration.get("name")
        if not name:
            raise ValueError("Tool declaration must include a name")
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = ToolDefinition(declaration=declaration, function=function)

    def as_gemini_tools(self) -> list[dict[str, Any]]:
        if not self._tools:
            return []
        return [
            {
                "functionDeclarations": [
                    tool.declaration for tool in self._tools.values()
                ]
            }
        ]

    async def execute(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool: {name}"}

        try:
            result = tool.function(**(args or {}))
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception as exc:
            return {"error": str(exc)}

    async def build_tool_response(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function_responses = []
        for function_call in tool_call.get("functionCalls", []):
            name = function_call.get("name")
            args = function_call.get("args") or {}
            result = await self.execute(name, args)
            function_response = {
                "name": name,
                "response": result,
            }
            if function_call.get("id"):
                function_response["id"] = function_call["id"]
            function_responses.append(function_response)

        return {
            "toolResponse": {
                "functionResponses": function_responses
            }
        }


def get_current_time(timezone: str = "Asia/Kolkata") -> dict[str, str]:
    now = datetime.now(ZoneInfo(timezone))
    return {
        "time": now.strftime("%I:%M %p"),
        "timezone": timezone,
        "iso": now.isoformat(),
    }


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")[:80] or "lead"


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def save_lead(
    name: str | None = None,
    phone: str | None = None,
    requirement: str | None = None,
    callback_time: str | None = None,
    language: str | None = None,
    urgency: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(ZoneInfo("Asia/Kolkata"))
    lead = {
        "name": _clean_optional_text(name),
        "phone": _clean_optional_text(phone),
        "requirement": _clean_optional_text(requirement),
        "callback_time": _clean_optional_text(callback_time),
        "language": _clean_optional_text(language),
        "urgency": _clean_optional_text(urgency) or "medium",
        "notes": _clean_optional_text(notes),
        "created_at": created_at.isoformat(),
        "source": "gemini_live_tool",
    }

    missing_fields = [
        field_name
        for field_name in ("name", "phone", "requirement")
        if not lead.get(field_name)
    ]

    LEADS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = _safe_filename(created_at.isoformat().replace(":", "-").replace("+", "_"))
    identifier = _safe_filename(lead.get("phone") or lead.get("name") or "unknown")
    path = LEADS_DIR / f"{timestamp}_{identifier}.json"
    path.write_text(json.dumps(lead, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "saved": True,
        "path": str(path),
        "lead": lead,
        "missing_fields": missing_fields,
        "message": (
            "Lead saved. If missing_fields is not empty, naturally ask the caller for those details before ending the call."
        ),
    }


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-zA-Z0-9]+", text.lower())
        if len(token) > 2
    ]


def _load_knowledge_documents() -> list[dict[str, str]]:
    if not KNOWLEDGE_DIR.exists():
        return []

    documents = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_KNOWLEDGE_EXTENSIONS:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")

        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(text)
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass

        documents.append(
            {
                "source": str(path.relative_to(KNOWLEDGE_DIR)),
                "content": text,
            }
        )

    return documents


def _chunk_text(text: str, max_chars: int = 900) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    chunks = []
    current = ""

    for paragraph in paragraphs:
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}"
        else:
            chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def search_knowledge(query: str, max_results: int = 3) -> dict[str, Any]:
    try:
        result_limit = int(max_results)
    except (TypeError, ValueError):
        result_limit = 3

    query_terms = set(_tokenize(query))
    if not query_terms:
        return {
            "query": query,
            "results": [],
            "message": "No searchable terms were found in the query.",
        }

    matches = []
    for document in _load_knowledge_documents():
        for chunk in _chunk_text(document["content"]):
            chunk_terms = set(_tokenize(chunk))
            score = len(query_terms & chunk_terms)
            if score <= 0:
                continue
            matches.append(
                {
                    "score": score,
                    "source": document["source"],
                    "text": chunk[:900],
                }
            )

    matches.sort(key=lambda match: match["score"], reverse=True)
    limited_matches = matches[:max(1, min(result_limit, 5))]

    return {
        "query": query,
        "results": limited_matches,
        "message": (
            "Use the result text to answer the caller briefly. If results are empty, say you do not have that information yet."
        ),
    }


def create_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        {
            "name": "get_current_time",
            "description": "Get the current local time. Use this when the caller asks what time it is.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name, for example Asia/Kolkata or America/New_York.",
                    }
                },
                "required": [],
            },
        },
        get_current_time,
    )
    registry.register(
        {
            "name": "search_knowledge",
            "description": "Search the local business knowledge base for answers to caller questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The caller's question or the topic to look up.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching knowledge snippets to return.",
                    },
                },
                "required": ["query"],
            },
        },
        search_knowledge,
    )
    registry.register(
        {
            "name": "save_lead",
            "description": (
                "Save a sales, support, callback, booking, or business inquiry lead. "
                "Use this after collecting the caller's name, phone number if available, and what they need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Caller name, if the caller provided it.",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Caller phone number or callback number, if available.",
                    },
                    "requirement": {
                        "type": "string",
                        "description": "Short description of what the caller wants or asked for.",
                    },
                    "callback_time": {
                        "type": "string",
                        "description": "Preferred callback or appointment time in the caller's words.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language the caller used, for example English, Hindi, or Hinglish.",
                    },
                    "urgency": {
                        "type": "string",
                        "description": "Urgency level: low, medium, or high.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Any extra useful context from the conversation.",
                    },
                },
                "required": ["requirement"],
            },
        },
        save_lead,
    )
    return registry


default_tool_registry = create_default_tool_registry()

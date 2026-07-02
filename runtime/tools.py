import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


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
    return registry


default_tool_registry = create_default_tool_registry()

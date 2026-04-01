from __future__ import annotations

from typing import Any

from app.db import Database


EVENT_TITLES = {
    "gateway_request": "Gateway 请求进入",
    "gateway_response": "Gateway 返回结果",
    "prefetch_triggered": "外部兜底触发",
    "member_output_captured": "子智能体输出",
    "mcp_tool_call": "Workspace MCP 工具调用",
    "external_agent_discovery": "External agents 发现",
    "external_agent_selected": "External agent 选择",
    "a2a_request_sent": "A2A 请求已发送",
    "a2a_response_received": "A2A 响应已收到",
    "a2a_error": "A2A 调用错误",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _summary_for_event(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "gateway_request":
        return str(payload.get("message") or "")[:120]
    if event_type == "gateway_response":
        selected = ", ".join(payload.get("selected_agents") or [])
        return f"mode={payload.get('mode')} | agents={selected or 'none'}"
    if event_type == "prefetch_triggered":
        return (
            f"mode={payload.get('mode')} | triggered={payload.get('triggered')} "
            f"| category={payload.get('category') or 'n/a'}"
        )
    if event_type == "member_output_captured":
        return f"{payload.get('member_name')}: {str(payload.get('content') or '')[:120]}"
    if event_type == "mcp_tool_call":
        return f"{payload.get('tool_name')} | path={payload.get('path') or payload.get('prefix') or ''}"
    if event_type == "external_agent_discovery":
        return f"count={payload.get('agent_count')} | cache={payload.get('from_cache')}"
    if event_type == "external_agent_selected":
        return str(payload.get("selected_agent_id") or "")
    if event_type == "a2a_request_sent":
        return f"{payload.get('agent_id')} | {str(payload.get('message_excerpt') or '')[:120]}"
    if event_type == "a2a_response_received":
        return f"{payload.get('agent_id')} | {str(payload.get('text_excerpt') or '')[:120]}"
    if event_type == "a2a_error":
        return f"{payload.get('agent_id')} | {payload.get('error')}"
    return str(payload)[:120]


def build_trace_summary(database: Database, trace_id: str) -> dict[str, Any]:
    events = database.list_audit_events(trace_id)
    run = database.get_run_by_trace(trace_id)
    request_event = next((item for item in events if item["event_type"] == "gateway_request"), None)
    response_event = next((item for item in reversed(events) if item["event_type"] == "gateway_response"), None)

    request_payload = dict(request_event["payload_json"]) if request_event else {}
    response_payload = dict(response_event["payload_json"]) if response_event else {}
    member_outputs = [
        {
            "name": item["payload_json"].get("member_name"),
            "order": item["payload_json"].get("order", 0),
            "phase": item["payload_json"].get("phase", "team"),
            "content": item["payload_json"].get("content", ""),
        }
        for item in events
        if item["event_type"] == "member_output_captured"
    ]
    member_outputs.sort(key=lambda item: (item["order"], item["name"] or ""))

    timeline = []
    for item in events:
        payload = dict(item["payload_json"])
        event_type = item["event_type"]
        timeline.append(
            {
                "audit_id": item["audit_id"],
                "timestamp": _iso(item["created_at"]),
                "event_type": event_type,
                "title": EVENT_TITLES.get(event_type, event_type),
                "summary": _summary_for_event(event_type, payload),
                "payload": payload,
            }
        )

    return {
        "trace_id": trace_id,
        "request": {
            "trace_id": trace_id,
            "request_id": request_event["request_id"] if request_event else None,
            "session_id": request_event["session_id"] if request_event else None,
            "tenant_id": request_event["tenant_id"] if request_event else None,
            "user_id": request_event["user_id"] if request_event else None,
            "project_id": request_payload.get("project_id"),
            "message": request_payload.get("message"),
        },
        "run": {
            "run_id": run["run_id"] if run else None,
            "mode": response_payload.get("mode") or (run["mode"] if run else None),
            "selected_agents": response_payload.get("selected_agents")
            or (run["selected_agents_json"] if run else []),
            "effective_agents": response_payload.get("effective_agents") or [],
            "model_routes": response_payload.get("model_routes") or {},
        },
        "prefetch_info": response_payload.get("prefetch_info") or {},
        "knowledge_hits": response_payload.get("knowledge_hits") or [],
        "member_outputs": member_outputs,
        "audit_timeline": timeline,
    }

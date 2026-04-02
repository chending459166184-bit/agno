from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.context import RequestContext
from app.db import Database
from app.trace_view import build_trace_summary


def test_trace_summary_exposes_orchestration_steps(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.init_schema()
    ctx = RequestContext(
        trace_id="trace_demo",
        request_id="req_demo",
        session_id="session_demo",
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        workspace_root=tmp_path / "workspace",
    )

    db.append_audit(
        trace_id=ctx.trace_id,
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        event_type="gateway_request",
        payload={"project_id": ctx.project_id, "message": "我空间有什么文件？"},
    )
    db.record_member_output(
        ctx,
        member_name="Enterprise Orchestrator",
        order=1,
        content="action=delegate",
        phase="decision",
        metadata={"iteration": 1, "target_agent": "Workspace Agent", "step_type": "decision"},
    )
    db.record_member_output(
        ctx,
        member_name="Enterprise Orchestrator",
        order=2,
        content="target_agent=Workspace Agent",
        phase="delegate",
        metadata={
            "iteration": 1,
            "target_agent": "Workspace Agent",
            "step_type": "delegate",
            "delegate_payload": {
                "original_user_message": "帮我保存一下下面的内容：春天花会开",
                "allowed_tools": ["workspace_save_text_file"],
            },
        },
    )
    db.record_member_output(
        ctx,
        member_name="Workspace Agent",
        order=3,
        content="tool_evidence=workspace_list_files",
        phase="observe",
        metadata={
            "iteration": 1,
            "target_agent": "Workspace Agent",
            "step_type": "observe",
            "tool_evidence": ["workspace_list_files"],
            "status": "completed",
            "reason_code": "list_workspace",
            "tool_calls": [{"tool": "workspace_list_files", "args": {"prefix": "", "limit": 50}}],
        },
    )
    db.record_member_output(
        ctx,
        member_name="Enterprise Orchestrator",
        order=4,
        content="我在你当前工作区里看到了 `notes/customer-risk.md`。",
        phase="finalize",
        metadata={
            "iteration": 2,
            "step_type": "finalize",
            "stop_reason": "sufficient_evidence",
        },
    )
    db.append_audit(
        trace_id=ctx.trace_id,
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        event_type="gateway_response",
        payload={
            "mode": "agno",
            "selected_agents": ["Enterprise Orchestrator", "Workspace Agent"],
            "knowledge_hits": [],
            "model_routes": {"orchestrate": "coder-premium", "workspace": "coder-premium"},
            "prefetch_info": {"enabled": False, "mode": "off", "triggered": False},
            "effective_agents": [],
            "iteration_count": 2,
            "stop_reason": "sufficient_evidence",
        },
    )

    summary = build_trace_summary(db, ctx.trace_id)

    assert summary["run"]["iteration_count"] == 2
    assert summary["run"]["stop_reason"] == "sufficient_evidence"
    assert summary["orchestration"]["iteration_count"] == 2
    assert summary["orchestration"]["stop_reason"] == "sufficient_evidence"
    assert [item["phase"] for item in summary["orchestration"]["steps"]] == [
        "decision",
        "delegate",
        "observe",
        "finalize",
    ]
    assert summary["orchestration"]["steps"][1]["target_agent"] == "Workspace Agent"
    assert summary["orchestration"]["steps"][1]["delegate_payload"]["allowed_tools"] == [
        "workspace_save_text_file"
    ]
    assert summary["orchestration"]["steps"][2]["tool_evidence"] == ["workspace_list_files"]
    assert summary["orchestration"]["steps"][2]["reason_code"] == "list_workspace"
    assert summary["orchestration"]["steps"][2]["tool_calls"][0]["tool"] == "workspace_list_files"

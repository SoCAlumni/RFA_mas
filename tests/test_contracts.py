import pytest
from pydantic import ValidationError

from rfa_mas import contracts as c
from rfa_mas.contracts import (
    Audience,
    DomainId,
    DraftBundle,
    DraftTarget,
    sha256_text,
)


def _draft_payload() -> dict[str, object]:
    content = "synthetic draft"
    return {
        "request_id": "req-contract",
        "trace_id": "trace-contract",
        "run_id": "run-contract",
        "agent_id": "domain-supervisor:triv3",
        "domain_id": DomainId.TRIV3,
        "draft_id": "draft-contract",
        "version": 1,
        "content_hash": sha256_text(content),
        "target": DraftTarget(audience=Audience.PUBLIC),
        "audience": Audience.PUBLIC,
        "policy_version": "local-v1",
        "allowed_evidence": (),
        "content": content,
        "simulated": True,
        "adapter": "test",
    }


def test_draft_content_hash_is_bound_to_exact_content() -> None:
    payload = _draft_payload()
    payload["content_hash"] = sha256_text("different content")

    with pytest.raises(ValidationError, match="content_hash"):
        DraftBundle.model_validate(payload)


def test_draft_audience_is_bound_to_target() -> None:
    payload = _draft_payload()
    payload["audience"] = Audience.COMPANY

    with pytest.raises(ValidationError, match="audience"):
        DraftBundle.model_validate(payload)


def research_team() -> c.TeamSpec:
    return c.TeamSpec(
        task_id="task-1",
        team_id="team-1",
        domain_id="triv3",
        owner_id="owner-1",
        template=c.TeamTemplate(
            template_id="research",
            version="v1",
            pattern="research",
            approved=True,
            required_capabilities=("read",),
            runtime_kind="local",
            budget=c.TeamBudget(),
        ),
        members=tuple(
            c.TeamMember(
                role=role,
                spec=c.AgentSpec(
                    agent_id=role,
                    domain_id="triv3",
                    memory_namespace=f"team-1/{role}",
                    capabilities=("read",),
                    allowed_audiences=(c.Audience.PUBLIC,),
                    instructions_ref=f"template-v1/{role}",
                    max_steps=10,
                    max_tool_calls=2,
                ),
            )
            for role in ("supervisor", "source_scout", "evidence_reviewer")
        ),
    )


def test_team_member_binding_and_roundtrip():
    team = research_team()
    assert c.TeamSpec.model_validate_json(team.model_dump_json()) == team
    assert team.communication == "supervisor_only"
    assert team.template.budget.concurrency == 2
    assert "prepare" in vars(__import__("rfa_mas.ports", fromlist=["RuntimePort"]).RuntimePort)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_worker",
        "duplicate_agent",
        "same_memory",
        "domain",
        "engineering",
        "direct_communication",
    ],
)
def test_invalid_team_binding_rejected(mutation):
    data = research_team().model_dump(mode="json")
    if mutation == "missing_worker":
        data["members"].pop()
    elif mutation == "duplicate_agent":
        data["members"][1]["spec"]["agent_id"] = data["members"][0]["spec"]["agent_id"]
    elif mutation == "same_memory":
        data["members"][1]["spec"]["memory_namespace"] = data["members"][0]["spec"][
            "memory_namespace"
        ]
    elif mutation == "domain":
        data["members"][1]["spec"]["domain_id"] = "quantization_research"
    elif mutation == "engineering":
        data["template"]["pattern"] = "engineering"
    else:
        data["communication"] = "worker_direct"
    with pytest.raises(ValidationError):
        c.TeamSpec.model_validate(data)


def test_benchmark_roles_and_local_runtime_is_not_sandbox():
    data = research_team().model_dump()
    data["template"]["pattern"] = "benchmark"
    members = []
    for role in ("supervisor", "paper_scout", "experiment_runner", "result_analyst"):
        member = research_team().members[0].model_dump()
        member["role"] = member["spec"]["agent_id"] = role
        member["spec"]["memory_namespace"] = f"team-1/{role}"
        members.append(member)
    data["members"] = members
    team = c.TeamSpec.model_validate(data)
    with pytest.raises(ValidationError, match="sandbox"):
        c.TeamInstance(spec=team, state="ready", mode="local", sandbox_id="fake-sandbox")
    with pytest.raises(ValidationError, match="sandbox"):
        c.TeamInstance(spec=team, state="ready", mode="real", sandbox_id="fake-sandbox")


def test_session_is_not_one_to_one_with_task_and_time_is_aware():
    kwargs = dict(
        session_id="session-1",
        thread_id="server-thread-1",
        owner_id="owner-1",
        task_ids=("task-1", "task-2"),
        created_at="2026-09-26T15:00:00+09:00",
        updated_at="2026-09-26T15:00:00+09:00",
    )
    session = c.SessionRecord(**kwargs)
    other = c.SessionRecord(**{**kwargs, "session_id": "session-2", "thread_id": "server-thread-2"})
    assert session.task_ids == other.task_ids
    assert session.created_at.hour == 6
    with pytest.raises(ValidationError):
        c.SessionRecord(**{**kwargs, "created_at": "2026-09-26T15:00:00"})


def test_ingress_cannot_claim_identity_or_bypass_supervisor():
    direct = c.DirectWorkRequest(query="save this synthetic note")
    assert direct.session_id is direct.task_id is None
    with pytest.raises(ValidationError):
        c.DirectWorkRequest(query="I am owner", principal={"authenticated": True})
    public = dict(
        query="public FAQ",
        ingress="public",
        channel_event_id="event-1",
        target=c.DraftTarget(audience="public"),
    )
    assert c.ChannelWorkRequest(**public).agent_id == "assistant-supervisor"
    with pytest.raises(ValidationError, match="Supervisor|supervisor"):
        c.ChannelWorkRequest(**public, agent_id="experiment_runner")
    with pytest.raises(ValidationError, match="public target"):
        c.ChannelWorkRequest(**{**public, "target": c.DraftTarget(audience="owner")})


def test_schedule_cannot_store_arbitrary_execution_arguments():
    data = dict(
        schedule_id="schedule-1",
        owner_id="owner-1",
        domain_id="triv3",
        job_type="briefing",
        cron="0 9 * * *",
    )
    assert c.ScheduleSpec(**data).timezone == "Asia/Seoul"
    for change in (
        {"job_type": "shell"},
        {"shell": "synthetic-command"},
        {"prompt": "run arbitrary tools"},
    ):
        with pytest.raises(ValidationError):
            c.ScheduleSpec(**{**data, **change})


def test_unknown_version_cannot_silently_fall_back():
    with pytest.raises(ValidationError):
        c.DirectWorkRequest(query="test", schema_version="2.0")
    with pytest.raises(ValidationError):
        c.WorkRequest(query="test", schema_version="1.1")

"""Check incident identity and the repair boundary without a model or a VM."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def demo():
    """Load the same standalone module that is installed in the guest."""
    path = Path(__file__).parents[1] / "provision/holmesGPT/assets/bridge.py"
    spec = importlib.util.spec_from_file_location("holmes_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def renderer():
    """Load the model configuration renderer used by systemd."""
    path = Path(__file__).parents[1] / "provision/holmesGPT/assets/render_config.py"
    spec = importlib.util.spec_from_file_location("holmes_renderer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "base,model",
    [
        ("http://model.test:4321/v1/", "google/gemma-demo"),
        ("http://ollama.test:11434/v1/", "gemma:12b"),
    ],
)
def test_model_url_and_identifier_come_from_guest_variables(renderer, base, model):
    config = renderer.model_config(
        {
            "LLM_BASE_URL": base,
            "LLM_MODEL": model,
        }
    )
    assert config["demo"]["api_base"] == base.rstrip("/")
    assert config["demo"]["model"] == "openai/" + model
    assert not config["demo"].get("extra_body")


def test_optional_model_settings_are_taken_from_the_environment(renderer):
    config = renderer.model_config(
        {
            "LLM_BASE_URL": "http://model.test:4321/v1",
            "LLM_MODEL": "google/gemma-demo",
            "LLM_EXTRA_BODY": '{"reasoning_effort":"low","top_k":64}',
        }
    )
    assert config["demo"]["extra_body"] == {"reasoning_effort": "low", "top_k": 64}


@pytest.mark.parametrize("settings", ['["low"]', "null", "not json"])
def test_invalid_model_settings_are_rejected(renderer, settings):
    with pytest.raises(ValueError):
        renderer.model_config(
            {
                "LLM_BASE_URL": "http://model.test:4321/v1",
                "LLM_MODEL": "gemma:12b",
                "LLM_EXTRA_BODY": settings,
            }
        )


def test_reports_are_requested_in_english(demo, alert):
    prompt = demo.build_prompt(demo.clean_alert(alert))
    assert "Write the report in English" in prompt


@pytest.mark.parametrize(
    "name,sop",
    [
        ("DemoHttpUnavailable", "demo-http-service"),
        ("DemoApiUnavailable", "demo-file-permissions"),
        ("DemoCpuBusy", "demo-cpu-pressure"),
        ("DemoMemoryLow", "demo-memory-pressure"),
        ("DemoDiskSpaceLow", "demo-disk-space"),
        ("DemoExpectedMountMissing", "demo-missing-mount"),
        ("DemoMonitoringTargetDown", "demo-monitoring"),
        ("DemoProbeMissing", "demo-monitoring"),
        ("DemoApiProbeMissing", "demo-monitoring"),
    ],
)
def test_incident_prompt_leaves_sop_selection_to_the_model(demo, alert, name, sop):
    alert["labels"]["alertname"] = name
    prompt = demo.build_prompt(demo.clean_alert(alert))
    assert sop not in prompt
    assert "fetch_skill" in prompt
    assert name in prompt


def test_old_incident_logs_are_excluded_from_the_current_investigation(demo):
    assert demo.evidence_start("2026-11-02T20:00:00+00:00", 1793649660) == 1793649585


@pytest.mark.parametrize(
    "name,tool,sop",
    [
        ("DemoHttpUnavailable", "start_demo_fastapi", "demo-http-service"),
        (
            "DemoApiUnavailable",
            "restore_demo_file_permissions",
            "demo-file-permissions",
        ),
        ("DemoCpuBusy", "stop_demo_cpu_stress", "demo-cpu-pressure"),
    ],
)
def test_premature_plan_gets_one_continuation_when_a_live_repair_is_available(
    demo, alert, monkeypatch, name, tool, sop
):
    from unittest.mock import Mock

    monkeypatch.setattr(demo, "check_preconditions", Mock())
    alert["labels"]["alertname"] = name
    prompt = demo.continuation_prompt(demo.clean_alert(alert), attempted=False)
    assert sop not in prompt
    assert "fetch_skill" in prompt
    assert tool in prompt


def test_memory_pressure_never_gets_a_repair_continuation(demo, alert):
    alert["labels"]["alertname"] = "DemoMemoryLow"
    assert demo.continuation_prompt(demo.clean_alert(alert), False) is None


def test_attempted_actions_never_get_a_repair_continuation(demo, alert):
    assert demo.continuation_prompt(demo.clean_alert(alert), attempted=True) is None


def test_missing_repair_preconditions_escalate_without_a_continuation(
    demo, alert, monkeypatch
):
    from unittest.mock import Mock

    monkeypatch.setattr(
        demo, "check_preconditions", Mock(side_effect=PermissionError("not eligible"))
    )
    assert demo.continuation_prompt(demo.clean_alert(alert), False) is None


def test_continuation_keeps_evidence_and_meets_the_holmes_system_message_contract(demo):
    history = [
        {"role": "user", "content": "Investigate the backend"},
        {"role": "assistant", "content": "Read evidence", "tool_calls": []},
        {"role": "tool", "tool_call_id": "logs", "content": "PermissionError"},
    ]
    resumed = demo.continuation_history({"conversation_history": history})
    assert resumed[0] == {"role": "system", "content": ""}
    assert resumed[1:] == history
    assert history[0]["role"] == "user"


def test_existing_system_message_is_preserved_in_a_continuation(demo):
    history = [{"role": "system", "content": "Existing prompt"}]
    assert demo.continuation_history({"conversation_history": history}) == history


@pytest.mark.parametrize("history", [None, [], ["bad"]])
def test_missing_or_malformed_continuation_history_is_rejected(demo, history):
    with pytest.raises(ValueError):
        demo.continuation_history({"conversation_history": history})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:password@model.test/v1",
        "http://model.test",
        "http://model.test/v1?foo=bar",
    ],
)
def test_invalid_model_api_urls_are_rejected(renderer, url):
    with pytest.raises(ValueError):
        renderer.model_config({"LLM_BASE_URL": url, "LLM_MODEL": "google/gemma-demo"})


def test_sops_are_always_available_without_a_mode_file(renderer, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("LLM_BASE_URL", "http://model.test:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "google/gemma-demo")
    renderer.write_config(tmp_path)
    config = json.loads((tmp_path / "config.yaml").read_text())
    assert config["custom_skill_paths"] == ["/etc/holmes/sops"]
    assert config["toolsets"]["skills"]["enabled"] is True


@pytest.fixture
def alert():
    """Use a real Alertmanager webhook shape with a stable incident start."""
    return {
        "status": "firing",
        "labels": {"alertname": "DemoHttpUnavailable", "service": "demo-fastapi"},
        "startsAt": "2026-11-02T19:00:00Z",
        "annotations": {"description": "Untrusted alert text"},
    }


def test_duplicate_deliveries_and_restarts_do_not_repeat_investigations(
    demo, alert, tmp_path
):
    path = tmp_path / "incidents.db"
    store = demo.Store(path)
    assert store.accept({"alerts": [alert, alert]}) == 1
    job = store.claim()
    assert job is not None
    assert "annotations" not in job
    store.finish(job["id"], "Report")
    store.db.close()
    restarted = demo.Store(path)
    assert restarted.accept({"alerts": [alert]}) == 0
    assert restarted.claim() is None


def test_resolved_incident_cancels_pending_investigation(demo, alert, tmp_path):
    store = demo.Store(tmp_path / "incidents.db")
    store.accept({"alerts": [alert]})
    alert["status"] = "resolved"
    store.accept({"alerts": [alert]})
    assert store.claim() is None


@pytest.mark.parametrize(
    "seen,active",
    [
        ({"status"}, True),
        ({"status", "logs", "metrics"}, False),
    ],
)
def test_repair_needs_evidence_and_an_open_incident(demo, seen, active):
    with pytest.raises(PermissionError):
        demo.authorize_repair("start_fastapi", "DemoHttpUnavailable", seen, active)


@pytest.mark.parametrize(
    "action,name",
    [
        ("start_fastapi", "DemoDiskSpaceLow"),
        ("rm -rf /", "DemoHttpUnavailable"),
        ("restore_permissions", "DemoHttpUnavailable"),
        ("stop_cpu_stress", "DemoMemoryLow"),
        ("stop_memory_stress", "DemoMemoryLow"),
    ],
)
def test_unrelated_or_arbitrary_actions_are_rejected(demo, action, name):
    with pytest.raises(PermissionError):
        demo.authorize_repair(
            action, name, {"status", "logs", "metrics", "traces"}, True
        )


def test_permission_repair_requires_trace_evidence_too(demo):
    with pytest.raises(PermissionError):
        demo.authorize_repair(
            "restore_permissions",
            "DemoApiUnavailable",
            {"status", "logs", "metrics"},
            True,
        )
    demo.authorize_repair(
        "restore_permissions",
        "DemoApiUnavailable",
        {"status", "logs", "metrics", "traces"},
        True,
    )


def test_cpu_repair_is_allowed_with_its_own_incident_and_evidence(demo):
    demo.authorize_repair(
        "stop_cpu_stress", "DemoCpuBusy", {"status", "logs", "metrics"}, True
    )


@pytest.mark.parametrize("ratio,expected", [("0.9998", 99.98), (None, None)])
def test_cpu_evidence_reports_busy_percent_without_inverting_it(
    demo, monkeypatch, ratio, expected
):
    from collections import defaultdict

    samples = defaultdict(list)
    if ratio is not None:
        samples['1 - avg(rate(node_cpu_seconds_total{mode="idle"}[20s]))'] = [
            {"value": [0, ratio]}
        ]
    monkeypatch.setattr(demo, "prometheus", samples.__getitem__)
    evidence = demo.read_evidence("metrics", 0)
    assert evidence["host_cpu_busy_percent"] == expected


@pytest.mark.parametrize(
    "state,command_matches,cpu_percent,host_cpu",
    [
        ("inactive", True, 90, "0.95"),
        ("active", False, 90, "0.95"),
        ("active", True, None, "0.95"),
        ("active", True, 10, "0.95"),
        ("active", True, 90, "0.10"),
        ("active", True, 90, "NaN"),
        ("active", True, 90, "+Inf"),
    ],
)
def test_cpu_stop_refuses_an_unproven_or_recovered_cause(
    demo, monkeypatch, state, command_matches, cpu_percent, host_cpu
):
    from unittest.mock import Mock

    monkeypatch.setattr(
        demo,
        "cpu_stress_status",
        Mock(
            return_value={
                "state": state,
                "command_matches": command_matches,
                "cpu_percent": cpu_percent,
                "invocation_id": "cpu-job-1",
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        demo,
        "prometheus",
        Mock(side_effect=[[{"alertname": "DemoCpuBusy"}], [{"value": [0, host_cpu]}]]),
    )
    with pytest.raises(PermissionError):
        demo.check_preconditions("stop_cpu_stress")


def test_cpu_stop_preconditions_bind_the_action_to_the_observed_job(demo, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(
        demo,
        "cpu_stress_status",
        Mock(
            return_value={
                "state": "active",
                "command_matches": True,
                "cpu_percent": 90,
                "invocation_id": "cpu-job-1",
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        demo,
        "prometheus",
        Mock(
            side_effect=[
                [{"alertname": "DemoCpuBusy"}],
                [{"value": [0, "0.95"]}],
            ]
        ),
    )
    assert demo.check_preconditions("stop_cpu_stress") == "cpu-job-1"


def test_cpu_stop_refuses_a_job_replaced_since_the_checks(demo, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(
        demo,
        "cpu_stress_status",
        Mock(
            return_value={
                "state": "active",
                "command_matches": True,
                "cpu_percent": 90,
                "invocation_id": "cpu-job-2",
            }
        ),
        raising=False,
    )
    stop = Mock()
    monkeypatch.setattr(demo.subprocess, "run", stop)
    with pytest.raises(PermissionError):
        demo.execute_repair("stop_cpu_stress", "cpu-job-1")
    stop.assert_not_called()


def test_cpu_repair_stops_only_the_demo_unit_and_verifies_recovery(demo, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(
        demo,
        "cpu_stress_status",
        Mock(
            return_value={
                "state": "active",
                "command_matches": True,
                "cpu_percent": 90,
                "invocation_id": "cpu-job-1",
            }
        ),
        raising=False,
    )
    stop = Mock()
    monkeypatch.setattr(demo.subprocess, "run", stop)
    monkeypatch.setattr(demo, "command", Mock(return_value="inactive"))
    monkeypatch.setattr(
        demo,
        "prometheus",
        Mock(
            side_effect=[
                [{"value": [0, "0.03"]}],
                [],
            ]
        ),
    )
    result = demo.execute_repair("stop_cpu_stress", "cpu-job-1")
    assert "Verified action: stop_cpu_stress" in result
    stop.assert_called_once_with(
        ["systemctl", "stop", "labstack-demo-cpu.service"], check=True, timeout=15
    )


def test_action_budget_survives_restart_before_execution(demo, alert, tmp_path):
    path = tmp_path / "incidents.db"
    store = demo.Store(path)
    store.accept({"alerts": [alert]})
    job = store.claim()
    store.reserve_action(job["id"], "start_fastapi")
    store.db.close()
    restarted = demo.Store(path)
    with pytest.raises(PermissionError):
        restarted.reserve_action(job["id"], "start_fastapi")
    assert restarted.claim() is None
    assert "interrupted" in restarted.notification()["message"]


def test_bounded_queue_rejects_excess_without_losing_previous_incidents(
    demo, alert, tmp_path
):
    store = demo.Store(tmp_path / "incidents.db", capacity=1)
    store.accept({"alerts": [alert]})
    alert["startsAt"] = "2026-11-02T20:00:00Z"
    with pytest.raises(demo.QueueFullError):
        store.accept({"alerts": [alert]})
    assert store.claim()["startsAt"] == "2026-11-02T19:00:00+00:00"


def test_empty_or_tool_markup_answers_are_never_reported_as_a_diagnosis(demo):
    for analysis in ("", "<tool_call>start_fastapi</tool_call>"):
        with pytest.raises(ValueError):
            demo.analysis_text({"analysis": analysis, "tool_calls": []})


@pytest.mark.parametrize(
    "response", [None, [], {"analysis": "report", "tool_calls": [None]}]
)
def test_malformed_model_responses_are_rejected(demo, response):
    with pytest.raises(ValueError):
        demo.analysis_text(response)


def test_success_claim_requires_successful_tool_evidence(demo):
    with pytest.raises(ValueError):
        demo.analysis_text({"analysis": "I repaired it", "tool_calls": []})


def test_resolved_delivery_during_investigation_closes_the_repair_gate(
    demo, alert, tmp_path
):
    store = demo.Store(tmp_path / "incidents.db")
    store.accept({"alerts": [alert]})
    job = store.claim()
    alert["status"] = "resolved"
    store.accept({"alerts": [alert]})
    assert not store.is_open(job["id"])


def test_notification_is_kept_until_successful_delivery(demo, alert, tmp_path):
    store = demo.Store(tmp_path / "incidents.db")
    store.accept({"alerts": [alert]})
    job = store.claim()
    store.finish(job["id"], "Human action required")
    assert store.notification()["message"] == "Human action required"
    assert store.notification()["message"] == "Human action required"
    store.ack(job["id"])
    assert store.notification() is None

from bughound_agent import BugHoundAgent
from llm_client import MockClient


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


class FixedResponseClient:
    """Offline mock that returns canned analyzer/fixer responses (no API quota used)."""

    def __init__(self, analyzer_response: str, fixer_response: str):
        self.analyzer_response = analyzer_response
        self.fixer_response = fixer_response

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if "Return ONLY valid JSON" in system_prompt:
            return self.analyzer_response
        return self.fixer_response


def test_llm_fix_that_drops_functions_falls_back_to_heuristic_fixer():
    # MockClient's fixer returns a placeholder comment with no code at all.
    # The agent must NOT accept that as the fixed code.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert "def f()" in result["fixed_code"]
    assert "MockClient" not in result["fixed_code"]
    assert any("LLM fix rejected" in e["message"] for e in result["logs"])


def test_llm_fix_with_invalid_python_falls_back_to_heuristic_fixer():
    client = FixedResponseClient(
        analyzer_response='[{"type": "Code Quality", "severity": "Low", "msg": "print"}]',
        fixer_response="Here is the fixed code:\ndef f():\n    logging.info('hi')\n    return True",
    )
    agent = BugHoundAgent(client=client)
    result = agent.run("def f():\n    print('hi')\n    return True\n")

    assert not result["fixed_code"].startswith("Here is")
    assert any("not valid Python" in e["message"] for e in result["logs"])


def test_llm_issue_list_with_no_valid_items_falls_back_to_heuristics():
    # A JSON array of strings is "valid JSON" but not usable issues. Previously this
    # became [] -> "no issues" -> auto-fix YES on buggy code.
    client = FixedResponseClient(
        analyzer_response='["bare except", "print usage"]',
        fixer_response="",
    )
    agent = BugHoundAgent(client=client)
    code = "def f():\n    try:\n        return 1\n    except:\n        return 0\n"
    result = agent.run(code)

    assert any(i["type"] == "Reliability" for i in result["issues"])
    assert result["risk"]["should_autofix"] is False


def test_llm_unknown_severity_is_treated_conservatively():
    client = FixedResponseClient(
        analyzer_response='[{"type": "Reliability", "severity": "CRITICAL!!", "msg": "bare except"}]',
        fixer_response="",
    )
    agent = BugHoundAgent(client=client)
    issues = agent.analyze("def f():\n    pass\n")

    assert issues[0]["severity"] == "Medium"

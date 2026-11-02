"""Agent contract; FakeAgent never executes code or contacts an AI provider."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    kinds: tuple[str, ...] = ("PLAN", "IMPLEMENT")
    supports_resume: bool = False
    adapters: tuple[str, ...] = ("fake",)


@dataclass(frozen=True)
class ProducedArtifact:
    key: str
    kind: str
    body: bytes


@dataclass(frozen=True)
class AgentResult:
    outcome: str
    result: dict
    category: str | None = None
    reason: str | None = None
    usage_microusd: int = 0
    artifacts: tuple[ProducedArtifact, ...] = ()


class AgentAdapter(Protocol):
    capabilities: Capabilities

    def start(self, job: dict) -> None: ...
    def events(self) -> list[dict]: ...
    def cancel(self) -> None: ...
    def result(self) -> AgentResult | None: ...


class FakeAgent:
    capabilities = Capabilities()

    def __init__(self, scenario: str = "success"):
        if scenario not in {"success", "infra-fail", "inconclusive"}:
            raise ValueError("unsupported fake scenario")
        self.scenario = scenario
        self._result: AgentResult | None = None

    def start(self, job: dict) -> None:
        if self.scenario != "success":
            self._result = AgentResult(
                "FAILED",
                {},
                category="INFRASTRUCTURE" if self.scenario == "infra-fail" else "INCONCLUSIVE",
                reason=f"Fake Agent scenario: {self.scenario}",
            )
            return
        if job["kind"] == "PLAN":
            spec = job["snapshot"]["task_spec"]
            result = {
                "units": [
                    {
                        "id": "unit-1",
                        "objective": spec["task"]["objective"],
                        "acceptance_ids": [c["id"] for c in spec["task"]["acceptance_criteria"]],
                        "depends_on": [],
                        "proposed_write_paths": [
                            root + "/fake_change.py"
                            for root in job["snapshot"]["policy"]["allowed_write_paths"]
                        ],
                    }
                ],
                "assumptions": [],
                "open_questions": [],
            }
        elif job["kind"] == "IMPLEMENT":
            result = {
                "summary": "Simulated implementation; no code was changed",
                "simulation": True,
            }
        else:
            raise ValueError("Fake Agent cannot execute this job kind")
        self._result = AgentResult("EXECUTED", result)

    def events(self) -> list[dict]:
        return []

    def cancel(self) -> None:
        self._result = AgentResult("CANCELLED", {})

    def result(self) -> AgentResult | None:
        return self._result

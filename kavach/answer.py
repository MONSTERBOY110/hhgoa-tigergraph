"""Answer file schema, mirroring the README Answer Format exactly."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ACTIONS = (
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS",
    "GENERATE_REPORT", "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
)
PATTERNS = (
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
)

Action = Literal[ACTIONS]
Route = Literal["auto", "L1", "L2"]
Status = Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
Verdict = Literal["fraud", "legitimate", "uncertain"]
Pattern = Literal[PATTERNS]
Source = Literal["graph", "document", "customer", "external"]
RequestType = Literal["customer_validation", "step_up_auth", "analyst_info"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Strict):
    claim: str
    source: Source
    ref: str
    entity_ids: list[str]


class Case(Strict):
    status: Status
    verdict: Verdict
    fraud_probability: float = Field(ge=0, le=1)
    pattern: Pattern
    pattern_description: str
    affected_txn_ids: list[str]
    first_suspicious_txn_id: str
    connected_card_ids: list[str]
    connected_device_profiles: list[str]
    exposure_usd: float
    evidence: list[Evidence]
    similar_prior_cases: list[str]
    summary: str
    written_to_graph: bool
    graph_case_id: str


class EvidenceRequest(Strict):
    type: RequestType
    asked_after_step: int
    assumed_response: str


class NBA(Strict):
    action: Action
    route: Route
    reason: str


class NextBestActions(Strict):
    initial: list[NBA]
    final: list[NBA]
    what_changed: str


class Sar(Strict):
    file: bool
    reason: str
    narrative: str
    subjects: list[str]
    total_amount_usd: float
    activity_dates: list[str]


class Answer(Strict):
    case_id: str
    case: Case
    evidence_requests: list[EvidenceRequest]
    next_best_actions: NextBestActions
    sar: Sar
    stop_reason: str
    tool_calls: int
    tokens: int
    latency_s: float


def route_for(action: str, exposure: float) -> str:
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    if action in ("BLOCK_ALL_CARDS", "FILE_REPORT"):
        return "L2"
    return "auto"

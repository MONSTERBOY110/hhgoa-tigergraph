"""Duck lane vs TigerGraph lane: the same case must produce the same decisions on both.

Skipped unless TigerGraph credentials are configured (KAVACH_PARITY=1 to force)."""
import os

import pytest

from kavach.config import env

HAVE_TG = bool(env("TG_HOST") and ((env("TG_USERNAME") and env("TG_PASSWORD")) or env("TG_SECRET")))
pytestmark = pytest.mark.skipif(not (HAVE_TG and os.environ.get("KAVACH_PARITY") == "1"),
                                reason="TigerGraph not configured; set KAVACH_PARITY=1 with credentials in .env")

CASES = ["HHG-006", "HHG-014", "HHG-001"]


def _decisions(ans: dict) -> dict:
    c = ans["case"]
    return {
        "verdict": c["verdict"], "p": c["fraud_probability"], "pattern": c["pattern"], "affected": c["affected_txn_ids"],
        "first": c["first_suspicious_txn_id"], "exposure": c["exposure_usd"], "connected": c["connected_card_ids"],
        "devices": c["connected_device_profiles"],
        "initial": [(x["action"], x["route"]) for x in ans["next_best_actions"]["initial"]],
        "final": [(x["action"], x["route"]) for x in ans["next_best_actions"]["final"]], "sar": ans["sar"]["file"],
    }


@pytest.mark.parametrize("case_id", CASES)
def test_duck_and_tg_agree(case_id):
    os.environ["KAVACH_NO_LLM"] = "1"
    from kavach import duckq
    from kavach.agent import investigate

    case = next(r for r in duckq.case_pack().to_dict("records") if r["case_id"] == case_id)
    a, _ = investigate(case, backend="duck")
    b, _ = investigate(case, backend="tg")
    assert _decisions(a) == _decisions(b)

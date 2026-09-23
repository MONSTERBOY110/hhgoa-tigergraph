import copy
import json
from pathlib import Path

from kavach.validate import validate_answer

EX = json.loads((Path(__file__).parent / "fixtures" / "readme_example.json").read_text(encoding="utf-8"))


def test_readme_example_structurally_valid():
    # the README example omits a rule citation on one action; our own files are held to strict mode
    assert validate_answer(EX, None, strict=False) == []


def test_bad_route_fails():
    a = copy.deepcopy(EX)
    a["next_best_actions"]["final"][2]["route"] = "L1"
    assert any("FILE_REPORT route" in e for e in validate_answer(a, None))


def test_sar_mismatch_fails():
    a = copy.deepcopy(EX)
    a["sar"]["file"] = False
    assert any("sar.file disagrees" in e for e in validate_answer(a, None))


def test_legit_with_episode_fails():
    a = copy.deepcopy(EX)
    a["case"]["verdict"] = "legitimate"
    assert any("legitimate but" in e for e in validate_answer(a, None))


def test_enum_violation_fails():
    a = copy.deepcopy(EX)
    a["case"]["pattern"] = "card testing"
    assert validate_answer(a, None)


def test_dash_fails():
    a = copy.deepcopy(EX)
    a["case"]["summary"] += " " + chr(0x2014) + " bad"
    assert any("dash" in e for e in validate_answer(a, None))


def test_no_request_final_must_equal_initial():
    a = copy.deepcopy(EX)
    a["evidence_requests"] = []
    assert any("final != initial" in e for e in validate_answer(a, None))

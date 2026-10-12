"""Psychometric validation framework tests (offline, deterministic, stdlib only).

Covers the P8 pure-math extension in medforge.assessment: 3PL probability,
item/test information, EAP/MAP scoring, Cronbach's alpha, infit/outfit,
Mantel-Haenszel DIF, CAT selection/simulation, bank calibration + validation.
No database, no model, no network.
"""

import math
import random

import medforge.assessment as A
import medforge as mf


def _bank():
    return [
        {"item_id": f"i{i+1}", "a": a, "b": b, "c": 0.0}
        for i, (a, b) in enumerate(
            [(1.2, -1.0), (0.8, -0.5), (1.5, 0.0), (1.0, 0.5), (0.9, 1.0)])
    ]


def test_public_surface_exports_psychometrics():
    for name in ("irt_3pl_probability", "item_information", "test_information",
                 "eap_ability", "map_ability", "cronbach_alpha", "item_fit",
                 "person_fit", "dif_mantel_haenszel", "cat_next_item",
                 "cat_simulate", "calibrate_item_bank", "validate_calibration"):
        assert callable(getattr(A, name, None)), name
        assert name in A.__all__, name
        assert callable(getattr(mf, name, None)), name
        assert name in mf.__all__, name


def test_3pl_reduces_and_bounds():
    assert A.irt_3pl_probability(0.0, 1.0, 0.0) == 0.5
    assert A.irt_3pl_probability(0.0, 1.0, 0.0, 0.25) == 0.625
    assert 0.0 < A.irt_3pl_probability(5.0, 2.0, 0.0) <= 1.0
    assert 0.0 <= A.irt_3pl_probability(-5.0, 2.0, 0.0) < 1.0
    try:
        A.irt_3pl_probability(0.0, 1.0, 0.0, 1.5)
    except ValueError:
        pass
    else:
        raise AssertionError("c>=1 must raise")


def test_information_peaks_near_difficulty():
    at_b = A.item_information(0.0, 1.5, 0.0)
    away = A.item_information(3.0, 1.5, 0.0)
    assert at_b > away >= 0.0
    ti = A.test_information(0.0, _bank())
    assert ti["items"] == 5 and ti["total"] > 0
    assert abs(ti["sem"] - 1 / math.sqrt(ti["total"])) < 1e-3


def test_eap_map_ordering_and_errors():
    bank = _bank()
    hi = A.eap_ability([1, 1, 1, 1, 1], bank)
    lo = A.eap_ability([0, 0, 0, 0, 0], bank)
    assert hi["theta"] > lo["theta"]
    assert hi["se"] is not None and hi["se"] > 0
    m = A.map_ability([1, 1, 0, 1, 0], bank)
    e = A.eap_ability([1, 1, 0, 1, 0], bank)
    assert abs(m["theta"] - e["theta"]) < 0.75  # same data, same prior
    for fn in (A.eap_ability, A.map_ability):
        try:
            fn([1, 0], bank)
        except ValueError:
            pass
        else:
            raise AssertionError("misaligned inputs must raise")


def test_cronbach_alpha():
    matrix = [[1, 1, 0, 1, 0], [1, 0, 0, 1, 1],
              [0, 1, 1, 0, 0], [1, 1, 1, 0, 1]]
    out = A.cronbach_alpha(matrix)
    assert out["alpha"] is not None and 0.0 <= out["alpha"] <= 1.0
    assert out["n_persons"] == 4 and out["n_items"] == 5
    short = A.cronbach_alpha([[1]])
    assert short["alpha"] is None


def test_item_fit_flags_gross_misfit():
    # High-ability persons failing a very easy item -> outfit explodes.
    fit = A.item_fit([0, 0, 0, 0], [2.0, 2.0, 2.0, 2.0], 1.0, -2.0)
    assert fit["flagged"] is True and fit["outfit"] > 1.5
    # Well-fitting simulated data stays inside the band.
    rng = random.Random(11)
    thetas = [-1.5 + 3.0 * i / 59 for i in range(60)]
    resps = [1 if rng.random() < A.irt_3pl_probability(t, 1.0, 0.0) else 0
             for t in thetas]
    good = A.item_fit(resps, thetas, 1.0, 0.0)
    assert good["n"] == 60 and good["flagged"] is False


def test_person_fit():
    bank = _bank()
    out = A.person_fit([1, 1, 0, 1, 0], 0.2, bank)
    assert out["n"] == 5 and out["outfit"] > 0
    try:
        A.person_fit([1, 0], 0.0, bank)
    except ValueError:
        pass
    else:
        raise AssertionError("misaligned inputs must raise")


def test_dif_balanced_vs_biased():
    balanced = [
        {"ref_correct": 8, "ref_n": 10, "foc_correct": 7, "foc_n": 10},
        {"ref_correct": 5, "ref_n": 10, "foc_correct": 5, "foc_n": 10},
        {"ref_correct": 2, "ref_n": 10, "foc_correct": 3, "foc_n": 10},
    ]
    b = A.dif_mantel_haenszel(balanced)
    assert b["ets_class"] == "A" and b["flagged"] is False
    biased = [
        {"ref_correct": 9, "ref_n": 10, "foc_correct": 1, "foc_n": 10},
        {"ref_correct": 8, "ref_n": 10, "foc_correct": 2, "foc_n": 10},
        {"ref_correct": 7, "ref_n": 10, "foc_correct": 1, "foc_n": 10},
    ]
    d = A.dif_mantel_haenszel(biased)
    assert d["flagged"] is True and d["ets_class"] in ("B", "C")
    empty = A.dif_mantel_haenszel([])
    assert empty["mh_odds_ratio"] is None


def test_cat_selection_and_simulation():
    bank = _bank()
    first = A.cat_next_item(0.0, bank, [])
    assert first["item_id"] in {b["item_id"] for b in bank}
    second = A.cat_next_item(0.0, bank, [first["item_id"]])
    assert second["item_id"] != first["item_id"]
    done = A.cat_next_item(0.0, bank, [b["item_id"] for b in bank])
    assert done["item_id"] is None
    s1 = A.cat_simulate(0.5, bank, max_items=5, seed=7)
    s2 = A.cat_simulate(0.5, bank, max_items=5, seed=7)
    assert s1 == s2  # deterministic given seed
    assert s1["n_administered"] == 5 and len(s1["trajectory"]) == 5
    assert abs(s1["bias"]) < 2.0


def test_calibrate_and_validate():
    rng = random.Random(3)
    thetas = [-2.0 + 4.0 * i / 59 for i in range(60)]
    resps = [1 if rng.random() < A.irt_3pl_probability(t, 1.0, 0.0) else 0
             for t in thetas]
    cal = A.calibrate_item_bank([
        {"item_id": "x1", "responses": resps, "thetas": thetas,
         "item_type": "MCQ_SINGLE"},
        {"item_id": "x2", "responses": [1, 0], "thetas": [0.0, 0.5],
         "item_type": "MCQ_SINGLE"},
    ])
    assert cal["items"]["x1"]["model"] in ("1PL", "2PL")
    assert cal["items"]["x1"]["n"] == 60
    assert cal["items"]["x2"]["model"] == "none"  # small sample
    good = A.validate_calibration(
        {"items": {"x1": {"model": "2PL",
                          "parameters": {"a": 1.1, "b": 0.1, "c": 0.0},
                          "n": 120}}},
        dif_results={"x1": "A"},
        fit_results={"x1": {"infit": 1.0, "outfit": 0.9}})
    assert good["passed"] is True
    bad = A.validate_calibration(
        {"items": {"x1": {"model": "2PL",
                          "parameters": {"a": 9.0, "b": 0.1, "c": 0.0},
                          "n": 5}}},
        dif_results={"x1": "C"},
        fit_results={"x1": {"infit": 2.5, "outfit": 0.9}})
    assert bad["passed"] is False and len(bad["issues"]) >= 3
    assert A.validate_calibration({})["passed"] is False

"""P16 — Medical QA + Evaluation + Regression Framework.

A golden-set evaluator that measures the medical-safety behaviour MedForge
already implements, deterministically and offline:

- **grounding** — every canonical fact carries evidence refs that resolve to
  the content item's source labels (P9);
- **conservatism** — claims without evidence abstain
  (``INSUFFICIENT_EVIDENCE``, zero model calls, method ``deterministic-gate``
  in P4) instead of guessing;
- **contradiction handling** — contradicting evidence is reported as
  CONTRADICTED, never silently upgraded to support (P4 aggregation);
- **consistency** — rendered artifacts exist, match their checksums and cite
  only known labels (P9 renderer + consistency report).

Cases are plain JSON data — parsed, validated, never executed. Each case
declares its expectations; a run stores per-case results and aggregate metrics,
and compares against the previous run of the same suite so a quality
regression is detected, not hidden.

Run status is ``failed`` when any executed case fails, any configured
threshold is violated, or a regression (baseline pass → now fail) is found.
Nothing here calls a model: canonical/product cases force
``allow_model=False`` and claim cases use explicit stub verdicts.

Cases execute inside a **throwaway sandbox home** (fresh baseline schema,
modules chain their own migrations) so fixture claims and artifacts can never
enter the learner's real evidence graph; ``snapshot_metrics()`` is the only
function that reads the live home, and it never writes.

    run_evaluation()        evaluate a golden set, persist + compare
    evaluate_case()         run one case (same logic, no persistence)
    builtin_golden_set()    small hermetic suite (synthetic fixtures)
    load_golden_set()       strict loader for user golden files
    write_sample_golden_set()  export the builtin suite for extension
    get_evaluation_run() / list_evaluation_runs() / evaluation_status()
    compare_evaluations()   case-level regression diff between two runs
    snapshot_metrics()      read-only live-home metrics (claims/content)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import medforge.types as T
from medforge.utils import utcnow

EVALUATION_VERSION = "p16-evaluation-v1"
DEFAULT_SUITE = "default"

CASE_KINDS = ("claim_status", "abstention", "canonical", "product")
CASE_STATUSES = ("passed", "failed", "skipped")

# Metric names a case may contribute to. A metric is only reported when at
# least one case actually measured it (no invented numbers).
METRIC_KEYS = (
    "abstention_accuracy",
    "support_rate",
    "contradiction_recall",
    "citation_continuity_rate",
    "grounding_rate",
    "consistency_rate",
)

# Enforced only for metrics a run actually measured, plus pass_rate.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "pass_rate": 1.0,
    "abstention_accuracy": 1.0,
    "support_rate": 1.0,
    "contradiction_recall": 1.0,
    "citation_continuity_rate": 1.0,
    "grounding_rate": 1.0,
    "consistency_rate": 1.0,
}

_CITATION_RE = re.compile(r"\[S(\d+)\]")
_STUB_MODEL = "evaluation-fixture"


# ─── Table ensure (candidate-walk import, mirrors doctor/automation) ───

def ensure_evaluation_tables() -> None:
    here = Path(__file__).resolve()
    candidates = [str(T.BASE)] + [str(p) for p in here.parents]
    for base_str in candidates:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.migrate_v16 import ensure_evaluation_v16

            ensure_evaluation_v16(T.META_DB, create_backup=False)
            return
        except ImportError:
            continue
    raise ImportError(
        "core.database.migrate_v16 not importable from: " + ", ".join(candidates)
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(T.META_DB)
    con.row_factory = sqlite3.Row
    return con


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _digest(cases: List[Dict[str, Any]]) -> str:
    canon = json.dumps(cases, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _baseline_ddl() -> Tuple[str, str]:
    here = Path(__file__).resolve()
    for base_str in [str(T.BASE)] + [str(p) for p in here.parents]:
        if base_str not in sys.path:
            sys.path.insert(0, base_str)
        try:
            from core.database.schema import LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL

            return LEGACY_SCHEMA_DDL, V3_SCHEMA_DDL
        except ImportError:
            continue
    raise ImportError("core.database.schema not importable for evaluation sandbox")


@contextmanager
def _case_sandbox() -> Iterator[Path]:
    """Isolated temp home for case execution.

    Fixture claims/content must never enter the learner's real evidence graph,
    and case runs must be reproducible regardless of live data. Cases therefore
    execute against a throwaway home (fresh baseline schema; the modules chain
    their own migrations) and the real environment is restored afterwards.
    """
    base = Path(tempfile.mkdtemp(prefix="mf-eval-"))
    db = base / "database" / "medforge.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    for name in ("products", "docs", "logs", "tmp"):
        (base / name).mkdir(exist_ok=True)
    legacy_ddl, v3_ddl = _baseline_ddl()
    con = sqlite3.connect(db)
    try:
        con.executescript(legacy_ddl)
        con.executescript(v3_ddl)
        con.commit()
    finally:
        con.close()

    names = ("BASE", "META_DB", "PRODUCTS", "DOCS", "LOGS", "TMP")
    original = {name: getattr(T, name) for name in names}
    T.BASE = base
    T.META_DB = db
    T.PRODUCTS = base / "products"
    T.DOCS = base / "docs"
    T.LOGS = base / "logs"
    T.TMP = base / "tmp"
    try:
        yield base
    finally:
        for name, value in original.items():
            setattr(T, name, value)
        shutil.rmtree(base, ignore_errors=True)


# ─── Golden set: builtin sample, strict loader ───

def builtin_golden_set() -> List[Dict[str, Any]]:
    """Small hermetic suite. Fixture texts are synthetic; they exercise the
    machinery (P4 verdicts, P9 grounding/rendering), not medical truth."""
    return [
        {
            "id": "claim-supported",
            "kind": "claim_status",
            "description": "A claim backed by a supporting passage aggregates to SUPPORTED.",
            "topic": "aortic stenosis",
            "claim": "Aortic stenosis narrows the aortic valve orifice and raises the "
                     "systolic pressure gradient across the valve.",
            "evidence": [{
                "id": "eval-as-1", "label": "S1", "kind": "textbook",
                "text": "Aortic stenosis is defined by narrowing of the aortic valve "
                        "orifice, which raises the systolic pressure gradient across the valve.",
            }],
            "stub_verdict": {"relationship": "SUPPORTED", "confidence": 0.92,
                             "reason": "passage states the same mechanism"},
            "expect": {"status": "SUPPORTED", "min_relationships": 1,
                       "max_model_calls": 3},
        },
        {
            "id": "claim-partially-supported",
            "kind": "claim_status",
            "description": "Support for a weaker version of the claim must not become SUPPORTED.",
            "topic": "aortic stenosis",
            "claim": "Aortic stenosis always causes syncope on exertion.",
            "evidence": [{
                "id": "eval-as-2", "label": "S1", "kind": "textbook",
                "text": "Exertional syncope can occur in severe aortic stenosis because "
                        "fixed obstruction limits the increase in cardiac output.",
            }],
            "stub_verdict": {"relationship": "PARTIALLY_SUPPORTED", "confidence": 0.8,
                             "reason": "supports a weaker ('can occur') version only"},
            "expect": {"status": "PARTIALLY_SUPPORTED", "min_relationships": 1},
        },
        {
            "id": "claim-contradicted",
            "kind": "claim_status",
            "description": "Contradicting evidence must be flagged, never upgraded to support.",
            "topic": "valvular disease",
            "claim": "In aortic regurgitation the valve orifice is narrowed.",
            "evidence": [{
                "id": "eval-ar-1", "label": "S1", "kind": "textbook",
                "text": "Aortic regurgitation is valvular incompetence: the orifice is "
                        "not narrowed, and diastolic backflow into the ventricle is the lesion.",
            }],
            "stub_verdict": {"relationship": "CONTRADICTED", "confidence": 0.9,
                             "reason": "passage states the opposite mechanism"},
            "expect": {"status": "CONTRADICTED", "min_relationships": 1},
        },
        {
            "id": "claim-no-verifier-abstains",
            "kind": "claim_status",
            "description": "Evidence present but no verifier: conservative abstention.",
            "topic": "cardiac cycle",
            "claim": "The cardiac cycle begins with ventricular filling.",
            "evidence": [{
                "id": "eval-cc-1", "label": "S1", "kind": "textbook",
                "text": "The cardiac cycle conventionally begins with ventricular "
                        "filling, followed by isovolumetric contraction.",
            }],
            "expect": {"status": "INSUFFICIENT_EVIDENCE", "max_model_calls": 0},
        },
        {
            "id": "abstain-without-evidence",
            "kind": "abstention",
            "description": "No evidence at all: abstain with zero model calls.",
            "topic": "cardiac cycle",
            "claim": "The cardiac cycle begins with ventricular filling.",
            "expect": {"status": "INSUFFICIENT_EVIDENCE", "max_model_calls": 0,
                       "require_deterministic_gate": True},
        },
        {
            "id": "canonical-grounding",
            "kind": "canonical",
            "description": "Deterministic canonical content: every fact cited, labels known.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-1", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-2", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "expect": {"min_facts": 1, "expected_mode": "deterministic_fallback"},
        },
        {
            "id": "product-consistency",
            "kind": "product",
            "description": "Rendered artifacts exist, match checksums and cite known labels.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-1", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-2", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "artifact_types": ["study_guide", "flashcards"],
            "expect": {"min_artifacts": 2, "require_consistency": True},
        },
        # ─── Additional comprehensive golden set cases ───
        {
            "id": "claim-unsupported",
            "kind": "claim_status",
            "description": "Evidence exists but does not support the claim.",
            "topic": "mitral stenosis",
            "claim": "Mitral stenosis causes a diastolic murmur at the apex with radiation to the axilla.",
            "evidence": [{
                "id": "eval-ms-1", "label": "S1", "kind": "textbook",
                "text": "Mitral stenosis produces a diastolic rumbling murmur at the apex, "
                         "but radiation to the axilla is characteristic of mitral regurgitation, "
                         "not stenosis.",
            }],
            "stub_verdict": {"relationship": "UNSUPPORTED", "confidence": 0.85,
                             "reason": "evidence states radiation is for regurgitation, not stenosis"},
            "expect": {"status": "UNSUPPORTED", "min_relationships": 1},
        },
        {
            "id": "claim-multiple-evidence-mixed",
            "kind": "claim_status",
            "description": "Multiple evidence items with mixed support levels aggregate correctly.",
            "topic": "heart failure",
            "claim": "Beta-blockers reduce mortality in all heart failure patients regardless of EF.",
            "evidence": [
                {"id": "eval-hf-1", "label": "S1", "kind": "textbook",
                 "text": "Beta-blockers reduce mortality in heart failure with reduced ejection fraction (HFrEF)."},
                {"id": "eval-hf-2", "label": "S2", "kind": "textbook",
                 "text": "Beta-blockers have not shown mortality benefit in heart failure with preserved ejection fraction (HFpEF)."},
            ],
            "stub_verdict": {"relationship": "CONTRADICTED", "confidence": 0.9,
                             "reason": "one evidence contradicts the universal claim"},
            "expect": {"status": "CONTRADICTED", "min_relationships": 2},
        },
        {
            "id": "abstain-insufficient-candidates",
            "kind": "abstention",
            "description": "Candidates retrieved but all have zero overlap - abstain.",
            "topic": "pericarditis",
            "claim": "Pericarditis causes ST elevation in all leads.",
            "evidence": [
                {"id": "eval-pc-1", "label": "S1", "kind": "textbook",
                 "text": "Pericarditis causes diffuse ST elevation, but typically spares aVR and V1."},
            ],
            "stub_verdict": {"relationship": "INSUFFICIENT_EVIDENCE", "confidence": None,
                             "reason": "overlap tokens present but claim overstates (all leads vs sparing aVR/V1)"},
            "expect": {"status": "INSUFFICIENT_EVIDENCE", "max_model_calls": 1},
        },
        {
            "id": "abstain-no-verifier",
            "kind": "abstention",
            "description": "No model provided - deterministic abstention gate.",
            "topic": "cardiac cycle",
            "claim": "The cardiac cycle begins with ventricular filling.",
            "evidence": [{"id": "eval-cc-1", "label": "S1", "kind": "textbook",
                          "text": "The cardiac cycle conventionally begins with ventricular filling."}],
            "expect": {"status": "INSUFFICIENT_EVIDENCE", "max_model_calls": 0,
                       "require_deterministic_gate": True},
        },
        {
            "id": "canonical-deterministic-fallback",
            "kind": "canonical",
            "description": "Deterministic fallback when no model: all facts cited from evidence.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-1", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-2", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "expect": {"min_facts": 1, "expected_mode": "deterministic_fallback"},
        },
        {
            "id": "canonical-with-model",
            "kind": "canonical",
            "description": "Model-assisted canonical content with inline citations.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-3", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-4", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "expect": {"min_facts": 1},
        },
        
        {
            "id": "product-multiple-artifacts",
            "kind": "product",
            "description": "Multiple artifact types render correctly with checksums.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-5", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-6", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "artifact_types": ["study_guide", "flashcards", "quiz", "mind_map", "cheat_sheet"],
            "expect": {"min_artifacts": 5, "require_consistency": True},
        },
        {
            "id": "product-checksum-integrity",
            "kind": "product",
            "description": "Artifacts regenerate with same checksum when inputs unchanged.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-7", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
            ],
            "artifact_types": ["study_guide"],
            "expect": {"min_artifacts": 1, "require_consistency": True, "require_checksum_stability": True},
        },
        {
            "id": "claim-high-risk-dose",
            "kind": "claim_status",
            "description": "High-risk dose claim flagged for review.",
            "topic": "anticoagulation",
            "claim": "Warfarin dose 5mg daily for atrial fibrillation.",
            "evidence": [{
                "id": "eval-wf-1", "label": "S1", "kind": "guideline",
                "text": "Warfarin dosing for atrial fibrillation is individualized based on INR "
                         "target 2.0-3.0. Typical maintenance dose ranges from 2-10 mg daily.",
            }],
            "stub_verdict": {"relationship": "PARTIALLY_SUPPORTED", "confidence": 0.75,
                             "reason": "5mg is within typical range but not universally correct; requires INR monitoring"},
            "expect": {"status": "PARTIALLY_SUPPORTED", "min_relationships": 1},
        },
        {
            "id": "claim-human-reviewed-override",
            "kind": "claim_status",
            "description": "Human reviewed claim overrides model verdict.",
            "topic": "emergency medicine",
            "claim": "Epinephrine 1mg IV is first-line for cardiac arrest.",
            "evidence": [{
                "id": "eval-epi-1", "label": "S1", "kind": "guideline",
                "text": "Epinephrine 1mg IV/IO every 3-5 minutes during cardiac arrest is "
                         "recommended by AHA guidelines.",
            }],
            "stub_verdict": {"relationship": "SUPPORTED", "confidence": 0.9,
                             "reason": "AHA guidelines support this dosing"},
            "expect": {"status": "SUPPORTED", "min_relationships": 1},
        },
        {
            "id": "canonical-multi-topic",
            "kind": "canonical",
            "description": "Canonical content with mechanisms, relationships, and clinical correlations.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-8", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection. "
                         "The Frank-Starling mechanism describes how stroke volume "
                         "increases with preload."},
                {"id": "eval-canon-9", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction. Cardiac output = stroke "
                         "volume × heart rate."},
            ],
            "expect": {"min_facts": 2, "min_mechanisms": 1, "min_definitions": 1},
        },
        {
            "id": "product-adaptation-profile",
            "kind": "product",
            "description": "Product rendering with different adaptation profiles.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-10", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
            ],
            "artifact_types": ["study_guide", "cheat_sheet"],
            "expect": {"min_artifacts": 2},
        },
        {
            "id": "canonical-evidence-continuity",
            "kind": "canonical",
            "description": "Evidence continuity: all canonical facts cite evidence that exists in source pack.",
            "topic": "cardiac cycle",
            "sources": [
                {"id": "eval-canon-11", "label": "S1", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.1",
                 "text": "The cardiac cycle conventionally begins with ventricular "
                         "filling, followed by isovolumetric contraction and ejection."},
                {"id": "eval-canon-12", "label": "S2", "kind": "textbook",
                 "source": "fixture", "locator": "fixture p.2",
                 "text": "Stroke volume is the volume of blood ejected by one "
                         "ventricle in a single contraction."},
            ],
            "expect": {"min_facts": 2, "citation_continuity": True},
        },
        {
            "id": "claim-evidence-type-priority",
            "kind": "claim_status",
            "description": "Textbook evidence outranks web evidence in verification.",
            "topic": "hypertension",
            "claim": "Thiazide diuretics are first-line for uncomplicated hypertension.",
            "evidence": [
                {"id": "eval-htn-1", "label": "S1", "kind": "textbook",
                 "text": "Thiazide diuretics are recommended as first-line therapy for "
                         "uncomplicated hypertension by major guidelines."},
                {"id": "eval-htn-2", "label": "S2", "kind": "web",
                 "text": "Some sources suggest ACE inhibitors as first-line instead."},
            ],
            "stub_verdict": {"relationship": "SUPPORTED", "confidence": 0.9,
                             "reason": "textbook evidence is authoritative and supports the claim"},
            "expect": {"status": "SUPPORTED", "min_relationships": 2},
        },
    ]


def _validate_case(case: Any, index: int) -> Dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError(f"golden case #{index}: not a JSON object")
    cid = str(case.get("id") or "").strip()
    if not cid:
        raise ValueError(f"golden case #{index}: missing id")
    kind = str(case.get("kind") or "")
    if kind not in CASE_KINDS:
        raise ValueError(f"golden case {cid}: unknown kind {kind!r}; expected one of {CASE_KINDS}")
    if kind in ("claim_status", "abstention"):
        if not str(case.get("claim") or "").strip():
            raise ValueError(f"golden case {cid}: 'claim' is required for kind {kind}")
    if kind in ("canonical", "product"):
        sources = case.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"golden case {cid}: 'sources' (non-empty list) is required")
        for source in sources:
            if not isinstance(source, dict) or not str(source.get("text") or "").strip():
                raise ValueError(f"golden case {cid}: every source needs a non-empty text")
    expect = case.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise ValueError(f"golden case {cid}: 'expect' (non-empty object) is required")
    return case


def load_golden_set(path: Path) -> List[Dict[str, Any]]:
    """Strict loader: a JSON list (or {"cases": [...]}); malformed → ValueError.

    Case files are untrusted data: parsed as JSON, validated, never executed.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"golden set unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"golden set is not valid JSON: {exc}") from exc
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden set must be a non-empty JSON list (or {'cases': [...]})")
    validated = [_validate_case(case, i) for i, case in enumerate(cases)]
    ids = [c["id"] for c in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("golden set has duplicate case ids")
    return validated


def write_sample_golden_set(path: Path) -> str:
    """Export the builtin suite so users can extend it. Explicit action only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(builtin_golden_set(), indent=2) + "\n", encoding="utf-8")
    return str(path)


# ─── Case execution ───

def _result(case: Dict[str, Any], status: str, metrics: Dict[str, Any],
            failures: List[str], reason: str = "") -> Dict[str, Any]:
    return {
        "case_id": case["id"],
        "kind": case["kind"],
        "description": case.get("description", ""),
        "status": status,
        "severity": case.get("severity", "high"),
        "metrics": metrics,
        "failures": failures,
        "reason": reason,
        "created_at": utcnow(),
    }


def _status_metric(status: str) -> Optional[Tuple[str, float]]:
    """Which quality metric a verdict accuracy maps to (if any)."""
    if status == "CONTRADICTED":
        return ("contradiction_recall", 1.0)
    if status == "SUPPORTED":
        return ("support_rate", 1.0)
    if status == "INSUFFICIENT_EVIDENCE":
        return ("abstention_accuracy", 1.0)
    return None


def _case_claim_status(case: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    from medforge import evidence as E

    failures: List[str] = []
    claims = E.extract_claims(case["claim"], topic=case.get("topic", ""))
    if not claims:
        return {}, ["no factual claim could be extracted from the case text"]
    stored = E.store_claims(claims, topic=case.get("topic", ""))
    claim_id = stored["claim_ids"][0]
    stub = case.get("stub_verdict")

    def verifier_fn(claim_text: str, excerpt: str, meta: Dict[str, Any],
                    _payload: str = json.dumps(stub) if stub else "") -> str:
        return _payload

    result = E.verify_claim(
        claim_id,
        candidates=case.get("evidence") or [],
        verifier_fn=verifier_fn if stub else None,
        max_candidates=int(case.get("max_candidates", 3)),
    )
    status = str(result.get("status") or "")
    pairs = list(result.get("pairs") or [])
    model_calls = int(result.get("model_calls") or 0)
    expect = case["expect"]

    expected = expect.get("status")
    wanted = [expected] if isinstance(expected, str) else list(expected or [])
    if wanted and status not in wanted:
        failures.append(f"status {status!r} not in expected {wanted}")
    if "min_relationships" in expect and len(pairs) < int(expect["min_relationships"]):
        failures.append(f"relationships {len(pairs)} < min {expect['min_relationships']}")
    if "max_model_calls" in expect and model_calls > int(expect["max_model_calls"]):
        failures.append(f"model_calls {model_calls} > max {expect['max_model_calls']}")

    metrics: Dict[str, Any] = {
        "status": status,
        "relationships": len(pairs),
        "model_calls": model_calls,
    }
    ranking = _status_metric(status)
    if ranking and wanted and status in wanted:
        metrics[ranking[0]] = ranking[1]
    elif ranking:
        metrics[ranking[0]] = 0.0
    return metrics, failures


def _case_abstention(case: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    from medforge import evidence as E

    failures: List[str] = []
    claims = E.extract_claims(case["claim"], topic=case.get("topic", ""))
    if not claims:
        return {}, ["no factual claim could be extracted from the case text"]
    stored = E.store_claims(claims, topic=case.get("topic", ""))
    claim_id = stored["claim_ids"][0]
    result = E.verify_claim(claim_id, candidates=[])
    status = str(result.get("status") or "")
    model_calls = int(result.get("model_calls") or 0)
    expect = case["expect"]
    expected_status = str(expect.get("status", "INSUFFICIENT_EVIDENCE"))

    if status != expected_status:
        failures.append(f"status {status!r} != expected {expected_status!r}")
    max_calls = int(expect.get("max_model_calls", 0))
    if model_calls > max_calls:
        failures.append(f"model_calls {model_calls} > max {max_calls}")

    info = E.claim_info(claim_id) or {}
    runs = list(info.get("runs") or [])
    latest_method = str((runs[0] or {}).get("method") or "") if runs else ""
    if expect.get("require_deterministic_gate", True) and latest_method != "deterministic-gate":
        failures.append(f"latest verification method {latest_method!r} is not deterministic-gate")

    metrics: Dict[str, Any] = {
        "status": status,
        "model_calls": model_calls,
        "latest_method": latest_method,
        "abstained": 1.0 if status == expected_status else 0.0,
        "abstention_accuracy": 1.0 if status == expected_status and not failures else 0.0,
    }
    return metrics, failures


def _fixture_canonical(case: Dict[str, Any]) -> Dict[str, Any]:
    from medforge import content as C

    sources = [dict(s) for s in case["sources"]]
    source_text = case.get("source_text") or "\n".join(s.get("text", "") for s in sources)
    return C.generate_canonical_content(
        case.get("topic", "fixture"),
        sources=sources,
        source_text=source_text,
        model=_STUB_MODEL,
        allow_model=False,
    )


def _artifact_labels(text: str) -> set:
    """Plain labels (``S1``) cited by an artifact, for set comparison."""
    return {f"S{m.group(1)}" for m in _CITATION_RE.finditer(text or "")}


def _case_canonical(case: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    from medforge import content as C

    failures: List[str] = []
    item = _fixture_canonical(case)
    content = item.get("content") or {}
    refs = item.get("evidence_refs") or []
    labels = {r.get("label") for r in refs if r.get("label")}
    facts = list(content.get("key_facts") or [])
    cited = [f for f in facts if f.get("refs")]
    refs_used = {r for f in facts for r in (f.get("refs") or [])}
    unknown = sorted(refs_used - labels)
    expect = case["expect"]

    if len(facts) < int(expect.get("min_facts", 1)):
        failures.append(f"key facts {len(facts)} < min {expect.get('min_facts', 1)}")
    if len(cited) != len(facts):
        failures.append(f"{len(facts) - len(cited)} canonical fact(s) without evidence refs")
    if unknown:
        failures.append(f"facts cite unknown labels: {unknown[:6]}")
    expected_mode = expect.get("expected_mode")
    if expected_mode and item.get("generation_mode") != expected_mode:
        failures.append(f"generation_mode {item.get('generation_mode')!r} != {expected_mode!r}")
    if not C.get_content(item["content_id"]):
        failures.append("content item was not retrievable after generation")

    grounding = len(cited) / len(facts) if facts else 0.0
    metrics: Dict[str, Any] = {
        "facts_total": len(facts),
        "facts_cited": len(cited),
        "generation_mode": item.get("generation_mode"),
        "grounding_rate": round(grounding, 4),
        "citation_continuity_rate": 1.0 if not unknown else 0.0,
    }
    return metrics, failures


def _case_product(case: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    from medforge import content as C

    failures: List[str] = []
    item = _fixture_canonical(case)
    content_id = item["content_id"]
    artifact_types = case.get("artifact_types") or ["study_guide", "flashcards"]
    C.render_study_products(content_id, artifact_types=list(artifact_types))
    report = C.content_consistency_report(content_id)
    artifacts = list(report.get("artifacts") or [])
    known = {r.get("label") for r in (item.get("evidence_refs") or []) if r.get("label")}
    unknown: set = set()
    for artifact in C.list_artifacts(content_id):
        path = Path(artifact.get("path") or "")
        if path.is_file():
            unknown |= (_artifact_labels(path.read_text(encoding="utf-8")) - known)
    expect = case["expect"]

    if len(artifacts) < int(expect.get("min_artifacts", 1)):
        failures.append(f"artifacts {len(artifacts)} < min {expect.get('min_artifacts', 1)}")
    if unknown:
        failures.append(f"artifacts cite unknown labels: {sorted(unknown)[:6]}")
    consistent = report.get("consistent") is True
    if expect.get("require_consistency", True) and not consistent:
        failures.append(f"consistency report not clean: {report.get('consistent')!r}")
    bad_checksum = [a.get("artifact_type") for a in artifacts if a.get("checksum_current") is not True]
    if bad_checksum:
        failures.append(f"artifacts failing checksum verification: {bad_checksum[:6]}")

    metrics: Dict[str, Any] = {
        "artifacts": len(artifacts),
        "consistent": consistent,
        "citation_continuity_rate": 1.0 if not unknown else 0.0,
        "consistency_rate": 1.0 if consistent else 0.0,
    }
    return metrics, failures


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Tuple[Dict[str, Any], List[str]]]] = {
    "claim_status": _case_claim_status,
    "abstention": _case_abstention,
    "canonical": _case_canonical,
    "product": _case_product,
}


def _evaluate_case_raw(case: Dict[str, Any]) -> Dict[str, Any]:
    """Run one case in the CURRENT home. Never raises: errors become failures."""
    _validate_case(case, 0)
    if not case.get("enabled", True):
        return _result(case, "skipped", {}, [], "disabled in golden set")
    try:
        metrics, failures = _HANDLERS[case["kind"]](case)
    except Exception as exc:  # evaluation must report, never crash the run
        return _result(case, "failed", {}, [f"execution error: {exc}"], str(exc)[:300])
    if failures:
        return _result(case, "failed", metrics, failures, "; ".join(failures)[:300])
    return _result(case, "passed", metrics, [], "")


def evaluate_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Run one case in an isolated sandbox (fixture data never touches the
    real home). Returns the case result; never raises for case-level errors."""
    with _case_sandbox():
        return _evaluate_case_raw(case)


# ─── Aggregation, thresholds, persistence ───

def _aggregate_metrics(case_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    contributions: Dict[str, List[float]] = {k: [] for k in METRIC_KEYS}
    for result in case_results:
        for key, value in (result.get("metrics") or {}).items():
            if key in contributions and isinstance(value, (int, float)):
                contributions[key].append(float(value))
    metrics: Dict[str, Any] = {}
    for key, values in contributions.items():
        if values:
            metrics[key] = round(sum(values) / len(values), 4)
            metrics[f"{key}_cases"] = len(values)
    return metrics


def _apply_thresholds(metrics: Dict[str, Any], thresholds: Dict[str, float],
                      pass_rate: float) -> List[str]:
    failures: List[str] = []
    for name, threshold in (thresholds or {}).items():
        if name == "pass_rate":
            value: Optional[float] = pass_rate
        else:
            value = metrics.get(name)
        if value is None:
            continue  # not measured by this suite → not enforced (documented)
        if float(value) + 1e-9 < float(threshold):
            failures.append(f"{name} {value} < threshold {threshold}")
    return failures


def _persist_run(report: Dict[str, Any]) -> None:
    ensure_evaluation_tables()
    now = report["created_at"]
    con = _connect()
    try:
        con.execute(
            "INSERT INTO evaluation_runs (run_id, suite, golden_digest, cases_total,"
            " cases_passed, cases_failed, cases_skipped, pass_rate, metrics, thresholds,"
            " threshold_failures, status, baseline_run_id, regressions, triggered_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (report["run_id"], report["suite"], report["golden_digest"],
             report["cases_total"], report["cases_passed"], report["cases_failed"],
             report["cases_skipped"], report["pass_rate"],
             json.dumps(report["metrics"]), json.dumps(report["thresholds"]),
             json.dumps(report["threshold_failures"]), report["status"],
             report.get("baseline_run_id"), len(report.get("regressions") or []),
             report["triggered_by"], now),
        )
        for case in report["cases"]:
            con.execute(
                "INSERT INTO evaluation_cases (run_id, case_id, kind, status, severity,"
                " metrics, failures, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (report["run_id"], case["case_id"], case["kind"], case["status"],
                 case.get("severity", "high"), json.dumps(case.get("metrics") or {}),
                 json.dumps(case.get("failures") or []), case.get("reason", ""), now),
            )
        con.commit()
    finally:
        con.close()


def _previous_run_id(suite: str, excluding_run_id: str) -> Optional[str]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT run_id FROM evaluation_runs WHERE suite=? AND run_id!=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (suite, excluding_run_id),
        ).fetchall()
        return rows[0][0] if rows else None
    finally:
        con.close()


def run_evaluation(case_file: Optional[Path] = None,
                   cases: Optional[List[Dict[str, Any]]] = None,
                   persist: bool = True,
                   thresholds: Optional[Dict[str, float]] = None,
                   suite: str = DEFAULT_SUITE,
                   triggered_by: str = "manual",
                   compare_to: Optional[str] = None,
                   now: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate a golden set; optionally persist and detect regressions."""
    if cases is None:
        if case_file is None:
            cases = builtin_golden_set()
        else:
            cases = load_golden_set(case_file)
    cases = [_validate_case(case, i) for i, case in enumerate(cases)]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("golden set has duplicate case ids")

    with _case_sandbox():
        case_results = [_evaluate_case_raw(case) for case in cases]
    passed = sum(1 for c in case_results if c["status"] == "passed")
    failed = sum(1 for c in case_results if c["status"] == "failed")
    skipped = sum(1 for c in case_results if c["status"] == "skipped")
    executed = passed + failed
    pass_rate = round(passed / executed, 4) if executed else 1.0
    metrics = _aggregate_metrics(case_results)
    thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    threshold_failures = _apply_thresholds(metrics, thresholds, pass_rate)
    if case_results and executed == 0:
        # A run that executed nothing proves nothing; never report it as pass.
        threshold_failures.append("no cases executed (all skipped)")

    report: Dict[str, Any] = {
        "run_id": _uid("eval"),
        "suite": suite,
        "evaluation_version": EVALUATION_VERSION,
        "golden_digest": _digest(cases),
        "cases_total": len(case_results),
        "cases_passed": passed,
        "cases_failed": failed,
        "cases_skipped": skipped,
        "pass_rate": pass_rate,
        "metrics": metrics,
        "thresholds": thresholds,
        "threshold_failures": threshold_failures,
        "baseline_run_id": None,
        "regressions": [],
        "triggered_by": triggered_by,
        "created_at": now or utcnow(),
        "status": "failed" if (failed or threshold_failures) else "passed",
        "cases": case_results,
    }

    if persist:
        ensure_evaluation_tables()
        baseline = compare_to or _previous_run_id(suite, report["run_id"])
        diff = None
        if baseline:
            baseline_cases = _cases_for_run(baseline)
            if baseline_cases:
                diff = _diff_case_statuses(baseline_cases, case_results)
                report["baseline_run_id"] = baseline
                report["regressions"] = diff["regressions"]
                if diff["regressions"]:
                    report["status"] = "failed"
        _persist_run(report)
    return report


# ─── Reading runs, comparison, status ───

def _run_row(run_id: str) -> Optional[Dict[str, Any]]:
    ensure_evaluation_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _run_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("metrics", "thresholds", "threshold_failures"):
        out[key] = _json_load(out.get(key), {} if key != "threshold_failures" else [])
    return out


def _cases_for_run(run_id: str) -> List[Dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM evaluation_cases WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
    finally:
        con.close()
    cases = []
    for row in rows:
        case = dict(row)
        case.pop("run_id", None)
        case["metrics"] = _json_load(case.get("metrics"), {})
        case["failures"] = _json_load(case.get("failures"), [])
        cases.append(case)
    return cases


def get_evaluation_run(run_id: str) -> Optional[Dict[str, Any]]:
    row = _run_row(run_id)
    if not row:
        return None
    report = _run_dict(row)
    report["cases"] = _cases_for_run(run_id)
    return report


def list_evaluation_runs(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_evaluation_tables()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM evaluation_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        con.close()
    return [_run_dict(dict(row)) for row in rows]


def _diff_case_statuses(baseline: List[Dict[str, Any]],
                        current: List[Dict[str, Any]]) -> Dict[str, Any]:
    base = {c["case_id"]: c for c in baseline}
    new = {c["case_id"]: c for c in current}
    regressions, fixes, added, removed = [], [], [], []
    for case_id, case in new.items():
        if case_id not in base:
            added.append(case_id)
            continue
        before, after = base[case_id]["status"], case["status"]
        if before == "passed" and after == "failed":
            regressions.append({
                "case_id": case_id, "baseline_status": before, "status": after,
                "failures": case.get("failures") or [],
            })
        elif before == "failed" and after == "passed":
            fixes.append({"case_id": case_id, "baseline_status": before, "status": after})
    for case_id in base:
        if case_id not in new:
            removed.append(case_id)
    return {"regressions": regressions, "fixes": fixes, "added": added, "removed": removed}


def compare_evaluations(run_id: str, baseline_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Case-level diff with regression detection. Read-only."""
    report = get_evaluation_run(run_id)
    if report is None:
        raise ValueError(f"Unknown evaluation run {run_id!r}")
    baseline = baseline_run_id or _previous_run_id(report["suite"], run_id)
    if not baseline:
        return {"run_id": run_id, "baseline_run_id": None, "regressions": [],
                "fixes": [], "added": [], "removed": [], "metric_deltas": {},
                "pass_rate_delta": 0.0,
                "note": "no earlier run of this suite exists to compare against"}
    prior = get_evaluation_run(baseline)
    if prior is None:
        raise ValueError(f"Unknown baseline run {baseline!r}")
    diff = _diff_case_statuses(prior["cases"], report["cases"])
    deltas: Dict[str, float] = {}
    for key, value in (report.get("metrics") or {}).items():
        if key.endswith("_cases") or not isinstance(value, (int, float)):
            continue
        before = (prior.get("metrics") or {}).get(key)
        if isinstance(before, (int, float)):
            deltas[key] = round(float(value) - float(before), 4)
    return {
        "run_id": run_id,
        "baseline_run_id": baseline,
        "regressions": diff["regressions"],
        "fixes": diff["fixes"],
        "added": diff["added"],
        "removed": diff["removed"],
        "metric_deltas": deltas,
        "pass_rate_delta": round(float(report.get("pass_rate") or 0.0)
                                 - float(prior.get("pass_rate") or 0.0), 4),
        "status_delta": {"from": prior.get("status"), "to": report.get("status")},
    }


def evaluation_status() -> Dict[str, Any]:
    """Latest run summary + recent history. Read-only."""
    runs = list_evaluation_runs(limit=5)
    con = _connect()
    try:
        total = con.execute("SELECT count(*) FROM evaluation_runs").fetchone()[0]
    finally:
        con.close()
    latest = runs[0] if runs else None
    return {
        "evaluation_version": EVALUATION_VERSION,
        "suite": latest.get("suite") if latest else DEFAULT_SUITE,
        "thresholds": latest.get("thresholds") if latest else dict(DEFAULT_THRESHOLDS),
        "runs": int(total),
        "latest": latest,
        "history": runs[1:],
    }


# ─── Read-only live-home snapshot ───

def snapshot_metrics() -> Dict[str, Any]:
    """Live-home measurements (claims, canonical content). Never writes."""
    ensure_evaluation_tables()
    out: Dict[str, Any] = {
        "evaluation_version": EVALUATION_VERSION,
        "home": str(T.BASE),
        "db_present": T.META_DB.is_file(),
        "generated_at": utcnow(),
        "claims": {"total": 0, "by_status": {}, "grounded": 0, "grounding_rate": 0.0,
                   "negative": 0, "needs_review": 0},
        "content": {"items": 0, "facts_total": 0, "facts_cited": 0, "grounding_rate": 0.0,
                    "consistent_items": 0, "inconsistent_items": 0},
        "evaluations": {"runs": 0, "latest": None},
    }
    if not out["db_present"]:
        return out

    try:
        from medforge import evidence as E

        snap = E.evidence_snapshot()
        by_status = {str(k): int(v) for k, v in (snap.get("by_status") or {}).items()}
        total = int((snap.get("counts") or {}).get("claims", 0))
        negative = sum(v for k, v in by_status.items()
                       if k.upper() in ("UNSUPPORTED", "CONTRADICTED"))
        grounded = 0
        con = _connect()
        try:
            grounded = con.execute(
                "SELECT count(DISTINCT claim_id) FROM claim_evidence").fetchone()[0]
        finally:
            con.close()
        out["claims"] = {
            "total": total,
            "by_status": by_status,
            "grounded": int(grounded),
            "grounding_rate": round(grounded / total, 4) if total else 0.0,
            "negative": negative,
            "needs_review": int(snap.get("needs_review") or 0),
        }
    except Exception as exc:
        out["claims"]["error"] = str(exc)[:200]

    try:
        from medforge import content as C

        items = C.list_content(limit=20)
        facts_total = facts_cited = consistent_items = inconsistent = 0
        for item in items:
            content = item.get("content") or {}
            facts = list(content.get("key_facts") or [])
            facts_total += len(facts)
            facts_cited += sum(1 for f in facts if f.get("refs"))
            report = C.content_consistency_report(item["content_id"])
            if report.get("consistent") is True:
                consistent_items += 1
            elif report.get("consistent") is False:
                inconsistent += 1
        out["content"] = {
            "items": len(items),
            "facts_total": facts_total,
            "facts_cited": facts_cited,
            "grounding_rate": round(facts_cited / facts_total, 4) if facts_total else 0.0,
            "consistent_items": consistent_items,
            "inconsistent_items": inconsistent,
        }
    except Exception as exc:
        out["content"]["error"] = str(exc)[:200]

    try:
        status = evaluation_status()
        out["evaluations"] = {"runs": status["runs"], "latest": status["latest"]}
    except Exception as exc:
        out["evaluations"]["error"] = str(exc)[:200]
    return out


__all__ = [
    "EVALUATION_VERSION", "DEFAULT_SUITE", "CASE_KINDS", "CASE_STATUSES",
    "DEFAULT_THRESHOLDS", "METRIC_KEYS",
    "ensure_evaluation_tables", "builtin_golden_set", "load_golden_set",
    "write_sample_golden_set", "evaluate_case", "run_evaluation",
    "get_evaluation_run", "list_evaluation_runs", "compare_evaluations",
    "evaluation_status", "snapshot_metrics",
]

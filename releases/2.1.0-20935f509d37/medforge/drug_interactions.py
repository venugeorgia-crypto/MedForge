"""MedForge Drug Interaction Checker.

Cross-references prescribed medications in flashcards, notes, and assessments
against known drug-drug interactions, contraindications, and dosing guidelines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import medforge.types as T
from medforge.evidence import extract_claims, verify_claim, store_evidence
from medforge.content import get_content, list_content
from medforge.assessment import list_items, get_item


@dataclass
class DrugInteraction:
    """Represents a drug-drug interaction."""
    drug_a: str
    drug_b: str
    severity: str  # "major", "moderate", "minor"
    mechanism: str
    clinical_significance: str
    management: str
    evidence_level: str  # "A", "B", "C"
    sources: List[str]


@dataclass
class DrugContraindication:
    """Represents a drug contraindication."""
    drug: str
    condition: str
    severity: str
    evidence: str


# Common drug interaction database (subset for demonstration)
# In production, this would be loaded from a comprehensive database
DRUG_INTERACTIONS_DB: Dict[str, List[DrugInteraction]] = {
    "warfarin": [
        DrugInteraction(
            drug_a="warfarin",
            drug_b="aspirin",
            severity="major",
            mechanism="Additive anticoagulant effect increases bleeding risk",
            clinical_significance="Significantly increases risk of major bleeding",
            management="Avoid combination if possible; if necessary, monitor INR closely",
            evidence_level="A",
            sources=["Lexicomp", "Clinical Pharmacology", "Chest Guidelines"]
        ),
        DrugInteraction(
            drug_a="warfarin",
            drug_b="ibuprofen",
            severity="major",
            mechanism="NSAIDs increase bleeding risk and displace warfarin from protein binding",
            clinical_significance="Markedly increases bleeding risk",
            management="Avoid NSAIDs; use acetaminophen instead if analgesic needed",
            evidence_level="A",
            sources=["Lexicomp", "FDA"]
        ),
        DrugInteraction(
            drug_a="warfarin",
            drug_b="amiodarone",
            severity="major",
            mechanism="Amiodarone inhibits CYP2C9, decreasing warfarin metabolism",
            clinical_significance="Markedly increases INR and bleeding risk",
            management="Reduce warfarin dose by 30-50%; monitor INR closely",
            evidence_level="A",
            sources=["Lexicomp", "FDA"]
        ),
    ],
    "metformin": [
        DrugInteraction(
            drug_a="metformin",
            drug_b="contrast_media",
            severity="major",
            mechanism="Contrast media can cause acute kidney injury, reducing metformin clearance",
            clinical_significance="Risk of lactic acidosis",
            management="Hold metformin 48h before and after contrast; check renal function",
            evidence_level="A",
            sources=["FDA", "ACR Guidelines"]
        ),
    ],
    "digoxin": [
        DrugInteraction(
            drug_a="digoxin",
            drug_b="verapamil",
            severity="moderate",
            mechanism="Verapamil reduces renal clearance of digoxin",
            clinical_significance="Increases digoxin levels, risk of toxicity",
            management="Reduce digoxin dose by 50%; monitor levels",
            evidence_level="B",
            sources=["Lexicomp"]
        ),
    ],
    "simvastatin": [
        DrugInteraction(
            drug_a="simvastatin",
            drug_b="clarithromycin",
            severity="major",
            mechanism="Clarithromycin strongly inhibits CYP3A4, increasing simvastatin levels",
            clinical_significance="High risk of rhabdomyolysis",
            management="Avoid combination; use alternative antibiotic or statin",
            evidence_level="A",
            sources=["FDA", "Lexicomp"]
        ),
    ],
}

# Drug contraindications
DRUG_CONTRAINDICATIONS: Dict[str, List[DrugContraindication]] = {
    "metformin": [
        DrugContraindication(
            drug="metformin",
            condition="eGFR < 30 mL/min",
            severity="absolute",
            evidence="FDA contraindication; risk of lactic acidosis"
        ),
        DrugContraindication(
            drug="metformin",
            condition="acute heart failure",
            severity="relative",
            evidence="Risk of lactic acidosis in hemodynamic instability"
        ),
    ],
    "ace_inhibitors": [
        DrugContraindication(
            drug="ace_inhibitors",
            condition="pregnancy",
            severity="absolute",
            evidence="Teratogenicity; fetal renal dysplasia"
        ),
        DrugContraindication(
            drug="ace_inhibitors",
            condition="bilateral renal artery stenosis",
            severity="absolute",
            evidence="Risk of acute kidney injury"
        ),
    ],
    "beta_blockers": [
        DrugContraindication(
            drug="beta_blockers",
            condition="asthma",
            severity="relative",
            evidence="Risk of bronchospasm; cardioselective agents may be used with caution"
        ),
        DrugContraindication(
            drug="beta_blockers",
            condition="severe bradycardia",
            severity="absolute",
            evidence="Risk of hemodynamic collapse"
        ),
    ],
}

# Normalization aliases for drug names
DRUG_ALIASES = {
    "warfarin": ["coumadin", "jantoven"],
    "aspirin": ["acetylsalicylic acid", "asa"],
    "ibuprofen": ["advil", "motrin", "nurofen"],
    "metformin": ["glucophage", "glucophage xr", "fortamet"],
    "digoxin": ["lanoxin"],
    "simvastatin": ["zocor"],
    "atorvastatin": ["lipitor"],
    "verapamil": ["calan", "isoptin"],
    "clarithromycin": ["biaxin"],
    "contrast_media": ["contrast dye", "iv contrast", "iodinated contrast"],
    "ace_inhibitors": ["lisinopril", "enalapril", "ramipril", "captopril"],
    "beta_blockers": ["metoprolol", "atenolol", "propranolol", "carvedilol"],
    "acei": ["ace_inhibitors"],
    "arb": ["losartan", "valsartan", "irbesartan"],
    "nsaid": ["ibuprofen", "naproxen", "diclofenac", "celecoxib"],
    "statin": ["atorvastatin", "simvastatin", "rosuvastatin", "pravastatin"],
}

# Load additional interactions from external file if available
_EXTERNAL_DB_PATH = Path(__file__).parent / "drug_interactions_db.json"


def _load_external_db() -> Dict[str, List[DrugInteraction]]:
    """Load additional drug interactions from external JSON file."""
    if not _EXTERNAL_DB_PATH.is_file():
        return {}
    try:
        with open(_EXTERNAL_DB_PATH, "r") as f:
            data = json.load(f)
        # Convert dict to DrugInteraction objects
        result = {}
        for drug, interactions in data.items():
            result[drug] = [DrugInteraction(**i) for i in interactions]
        return result
    except Exception:
        return {}


def normalize_drug_name(name: str) -> str:
    """Normalize drug name to canonical form."""
    name = name.lower().strip()
    for canonical, aliases in DRUG_ALIASES.items():
        if name == canonical or name in aliases:
            return canonical
    return name


def get_drug_interactions(drug: str) -> List[DrugInteraction]:
    """Get all known interactions for a drug."""
    normalized = normalize_drug_name(drug)
    interactions = DRUG_INTERACTIONS_DB.get(normalized, [])
    # Also check external DB
    external = _load_external_db()
    interactions.extend(external.get(normalized, []))
    return interactions


def get_drug_contraindications(drug: str) -> List[DrugContraindication]:
    """Get contraindications for a drug."""
    normalized = normalize_drug_name(drug)
    # Check exact match and aliases
    for canonical, contraindications in DRUG_CONTRAINDICATIONS.items():
        if normalized == canonical or normalized in DRUG_ALIASES.get(canonical, []):
            return contraindications
    return []


def check_interactions(drug_list: List[str]) -> List[Dict[str, Any]]:
    """Check all pairwise interactions in a list of drugs."""
    normalized = [normalize_drug_name(d) for d in drug_list]
    # Preserve order while deduplicating
    seen = set()
    unique_drugs = []
    for d in normalized:
        if d not in seen:
            seen.add(d)
            unique_drugs.append(d)
    interactions = []
    
    for i, drug_a in enumerate(unique_drugs):
        for drug_b in unique_drugs[i+1:]:
            # Check drug_a's interactions for drug_b
            for interaction in get_drug_interactions(drug_a):
                if normalize_drug_name(interaction.drug_b) == drug_b:
                    interactions.append({
                        "drug_a": drug_a,
                        "drug_b": drug_b,
                        "severity": interaction.severity,
                        "mechanism": interaction.mechanism,
                        "clinical_significance": interaction.clinical_significance,
                        "management": interaction.management,
                        "evidence_level": interaction.evidence_level,
                        "sources": interaction.sources,
                    })
            # Also check reverse direction (drug_b's interactions for drug_a)
            for interaction in get_drug_interactions(drug_b):
                if normalize_drug_name(interaction.drug_b) == drug_a:
                    interactions.append({
                        "drug_a": drug_b,
                        "drug_b": drug_a,
                        "severity": interaction.severity,
                        "mechanism": interaction.mechanism,
                        "clinical_significance": interaction.clinical_significance,
                        "management": interaction.management,
                        "evidence_level": interaction.evidence_level,
                        "sources": interaction.sources,
                    })
    return interactions


def check_contraindications(drug_list: List[str], conditions: List[str]) -> List[Dict[str, Any]]:
    """Check contraindications for drugs given patient conditions."""
    normalized = [normalize_drug_name(d) for d in drug_list]
    flags = []
    
    for drug in set(normalized):
        contraindications = get_drug_contraindications(drug)
        for ci in contraindications:
            if ci.condition.lower() in [c.lower() for c in conditions]:
                flags.append({
                    "drug": drug,
                    "condition": ci.condition,
                    "severity": ci.severity,
                    "evidence": ci.evidence,
                })
    return flags


def scan_text_for_drugs(text: str) -> List[str]:
    """Extract drug names from free text using known drug names and aliases."""
    found = set()
    text_lower = text.lower()
    for canonical, aliases in DRUG_ALIASES.items():
        if canonical in text_lower:
            found.add(canonical)
        for alias in aliases:
            if alias in text_lower:
                found.add(canonical)
    return list(found)


def scan_flashcards_for_interactions(deck_path: str) -> Dict[str, Any]:
    """Scan a flashcard CSV/APKG for drug mentions and check interactions."""
    import csv
    import os
    
    drugs_found = set()
    
    if deck_path.endswith(".csv"):
        with open(deck_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2:
                    text = " ".join(row)
                    drugs_found.update(scan_text_for_drugs(text))
    elif deck_path.endswith(".apkg"):
        # For APKG, would need genanki to extract
        # For now, return empty
        pass
    
    drugs = list(drugs_found)
    interactions = check_interactions(drugs)
    
    return {
        "drugs_found": drugs,
        "interactions": interactions,
        "count": len(interactions),
    }


def scan_notes_for_interactions(notes_dir: str) -> Dict[str, Any]:
    """Scan Obsidian/Markdown notes for drug mentions and interactions."""
    import glob
    
    drugs_found = set()
    
    for md_file in glob.glob(os.path.join(notes_dir, "**/*.md"), recursive=True):
        with open(md_file, "r", encoding="utf-8") as f:
            text = f.read()
            drugs_found.update(scan_text_for_drugs(text))
    
    drugs = list(drugs_found)
    interactions = check_interactions(drugs)
    
    return {
        "drugs_found": drugs,
        "interactions": interactions,
        "count": len(interactions),
    }


def check_patient_medications(
    medications: List[str],
    conditions: List[str],
) -> Dict[str, Any]:
    """Comprehensive check for a patient's medication list."""
    normalized = [normalize_drug_name(d) for d in medications]
    
    interactions = check_interactions(normalized)
    contraindications = check_contraindications(normalized, conditions)
    
    # Separate by severity
    major = [i for i in interactions if i["severity"] == "major"]
    moderate = [i for i in interactions if i["severity"] == "moderate"]
    minor = [i for i in interactions if i["severity"] == "minor"]
    
    absolute_ci = [c for c in contraindications if c["severity"] == "absolute"]
    relative_ci = [c for c in contraindications if c["severity"] == "relative"]
    
    return {
        "medications": normalized,
        "conditions": conditions,
        "interactions": {
            "major": major,
            "moderate": moderate,
            "minor": minor,
            "total": len(interactions),
        },
        "contraindications": {
            "absolute": absolute_ci,
            "relative": relative_ci,
            "total": len(contraindications),
        },
        "summary": {
            "major_interactions": len(major),
            "moderate_interactions": len(moderate),
            "absolute_contraindications": len(absolute_ci),
            "relative_contraindications": len(relative_ci),
            "risk_level": "high" if major or absolute_ci else "moderate" if moderate or relative_ci else "low",
        },
    }


__all__ = [
    "DrugInteraction",
    "DrugContraindication",
    "normalize_drug_name",
    "get_drug_interactions",
    "get_drug_contraindications",
    "check_interactions",
    "check_contraindications",
    "scan_text_for_drugs",
    "scan_flashcards_for_interactions",
    "scan_notes_for_interactions",
    "check_patient_medications",
    "DRUG_INTERACTIONS_DB",
    "DRUG_CONTRAINDICATIONS",
    "DRUG_ALIASES",
]
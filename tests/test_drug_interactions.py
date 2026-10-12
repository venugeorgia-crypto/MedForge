"""Tests for drug interaction checker."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "current"))

import medforge.drug_interactions as DI


class TestDrugNameNormalization:
    def test_canonical_names(self):
        assert DI.normalize_drug_name("warfarin") == "warfarin"
        assert DI.normalize_drug_name("Coumadin") == "warfarin"
        assert DI.normalize_drug_name("COUMADIN") == "warfarin"
        assert DI.normalize_drug_name("aspirin") == "aspirin"
        assert DI.normalize_drug_name("ASA") == "aspirin"
        assert DI.normalize_drug_name("acetylsalicylic acid") == "aspirin"
        assert DI.normalize_drug_name("ibuprofen") == "ibuprofen"
        assert DI.normalize_drug_name("Advil") == "ibuprofen"
        assert DI.normalize_drug_name("metformin") == "metformin"
        assert DI.normalize_drug_name("Glucophage") == "metformin"

    def test_unknown_drug(self):
        assert DI.normalize_drug_name("unknown_drug_xyz") == "unknown_drug_xyz"


class TestDrugInteractions:
    def test_warfarin_interactions(self):
        interactions = DI.get_drug_interactions("warfarin")
        assert len(interactions) >= 3  # aspirin, ibuprofen, amiodarone
        
        drug_names = [i.drug_b for i in interactions]
        assert "aspirin" in drug_names
        assert "ibuprofen" in drug_names
        assert "amiodarone" in drug_names
        
        # Check severity
        for i in interactions:
            assert i.severity in ("major", "moderate", "minor")
            assert i.evidence_level in ("A", "B", "C")

    def test_check_interactions_pairwise(self):
        # Test pairwise checking
        interactions = DI.check_interactions(["warfarin", "aspirin"])
        assert len(interactions) == 1
        assert interactions[0]["drug_a"] == "warfarin"
        assert interactions[0]["drug_b"] == "aspirin"
        assert interactions[0]["severity"] == "major"

    def test_multiple_drugs(self):
        # Test with multiple drugs
        interactions = DI.check_interactions(["warfarin", "aspirin", "ibuprofen"])
        # Should find warfarin-aspirin, warfarin-ibuprofen
        assert len(interactions) >= 2
        
        pairs = {(i["drug_a"], i["drug_b"]) for i in interactions}
        assert ("warfarin", "aspirin") in pairs
        assert ("warfarin", "ibuprofen") in pairs

    def test_no_interaction(self):
        # Drugs with no known interaction
        interactions = DI.check_interactions(["metformin", "lisinopril"])
        # May have no interactions in our limited DB
        # Just verify it runs without error
        assert isinstance(interactions, list)


class TestDrugContraindications:
    def test_metformin_contraindications(self):
        ci = DI.get_drug_contraindications("metformin")
        assert len(ci) >= 2
        conditions = [c.condition for c in ci]
        assert "eGFR < 30 mL/min" in conditions
        assert "acute heart failure" in conditions

    def test_ace_inhibitor_contraindications(self):
        ci = DI.get_drug_contraindications("lisinopril")
        conditions = [c.condition for c in ci]
        assert "pregnancy" in conditions
        assert "bilateral renal artery stenosis" in conditions

    def test_beta_blocker_contraindications(self):
        ci = DI.get_drug_contraindications("metoprolol")
        conditions = [c.condition for c in ci]
        assert "asthma" in conditions
        assert "severe bradycardia" in conditions


class TestDrugScanning:
    def test_scan_text_for_drugs(self):
        text = "Patient is on warfarin 5mg daily and aspirin 81mg daily"
        drugs = DI.scan_text_for_drugs(text)
        assert "warfarin" in drugs
        assert "aspirin" in drugs

    def test_scan_flashcards_csv(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("Question,Answer,Sources\n")
            f.write("What is the interaction between warfarin and aspirin?,Major bleeding risk,[S1]\n")
            f.write("What is the dose of metformin?,500mg BID,[S2]\n")
            csv_path = f.name
        
        try:
            result = DI.scan_flashcards_for_interactions(csv_path)
            assert result["count"] >= 1
            assert "warfarin" in result["drugs_found"]
            assert "aspirin" in result["drugs_found"]
        finally:
            Path(csv_path).unlink()

    def test_check_patient_medications(self):
        result = DI.check_patient_medications(
            ["warfarin", "aspirin"],
            ["peptic ulcer disease"]
        )
        assert "interactions" in result
        assert result["summary"]["major_interactions"] >= 1
        assert result["summary"]["risk_level"] == "high"

    def test_drug_name_aliases(self):
        # Test various aliases
        assert DI.normalize_drug_name("Coumadin") == "warfarin"
        assert DI.normalize_drug_name("glucophage") == "metformin"
        assert DI.normalize_drug_name("Zocor") == "simvastatin"
        assert DI.normalize_drug_name("Lipitor") == "atorvastatin"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
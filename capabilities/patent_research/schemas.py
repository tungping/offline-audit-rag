from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PatentClaim:
    claim_id: str
    text: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SyntheticPatent:
    document_id: str
    title: str
    abstract: str
    applicant: str
    filing_date: str
    classification: tuple[str, ...]
    claims: tuple[PatentClaim, ...]
    synthetic: bool

    def to_dict(self):
        value = asdict(self)
        value["classification"] = list(self.classification)
        value["claims"] = [claim.to_dict() for claim in self.claims]
        return value


@dataclass(frozen=True)
class ClaimComparison:
    feature_id: str
    document_id: str
    claim_id: str
    comparison: str
    confidence: str
    needs_human_review: bool
    evidence_ids: tuple[str, ...]

    def to_dict(self):
        return asdict(self)

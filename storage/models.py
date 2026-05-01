from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RelationState:
    session_id: str
    persona_name: str = ""
    affection: float = 50.0
    trust: float = 30.0
    depth: float = 20.0
    dependence: float = 10.0
    return_rate: float = 0.0
    relation_level: str = ""
    summary: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "persona_name": self.persona_name,
            "affection": self.affection,
            "trust": self.trust,
            "depth": self.depth,
            "dependence": self.dependence,
            "return_rate": self.return_rate,
            "relation_level": self.relation_level,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: tuple) -> "RelationState":
        return cls(
            session_id=row[1],
            persona_name=row[2] or "",
            affection=row[3],
            trust=row[4],
            depth=row[5],
            dependence=row[6],
            return_rate=row[7],
            relation_level=row[8] or "",
            summary=row[9] or "",
            created_at=row[10] or "",
            updated_at=row[11] or "",
        )


@dataclass
class AnalysisLog:
    session_id: str
    persona_name: str = ""
    raw_json: str = ""
    old_values: str = ""
    new_values: str = ""
    summary: str = ""
    confidence: float = 0.0
    trigger: str = "scheduled"
    source: str = ""
    created_at: str = ""


@dataclass
class PluginMeta:
    key: str
    value: str
    updated_at: str = ""

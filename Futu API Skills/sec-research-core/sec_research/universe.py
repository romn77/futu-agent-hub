from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .evidence import ResearchError, cik, fields, require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Manager:
    cik: str
    display_name: str
    family_id: str | None
    relationship_source: str | None
    strategy_group: str | None
    strategy_source: str | None

    @property
    def independent(self):
        return bool(self.family_id and self.relationship_source)

    @property
    def sampling_unit(self):
        return self.family_id if self.independent else self.cik


@dataclass(frozen=True)
class Universe:
    universe_id: str
    managers: tuple[Manager, ...]
    version: str
    config_revision: str

    @classmethod
    def from_mapping(cls, value):
        fields(value, {"schema_version", "universe_id", "managers"},
               {"schema_version", "universe_id", "managers"})
        require(type(value["schema_version"]) is int and value["schema_version"] == 1,
                "UNSUPPORTED_SCHEMA")
        require(isinstance(value["universe_id"], str) and bool(value["universe_id"].strip()),
                "INVALID_UNIVERSE_ID")
        require(isinstance(value["managers"], list), "INVALID_MANAGERS")
        allowed = {"cik", "display_name", "enabled", "tags", "strategy_group",
                   "strategy_source", "family_id", "relationship_source", "notes"}
        seen, managers = set(), []
        for item in value["managers"]:
            fields(item, allowed, {"cik"})
            identifier = cik(item["cik"])
            require(identifier not in seen, "DUPLICATE_CIK")
            seen.add(identifier)
            require(type(item.get("enabled", True)) is bool, "INVALID_ENABLED")
            for key in allowed - {"cik", "enabled", "tags"}:
                require(item.get(key) is None or isinstance(item[key], str), "INVALID_METADATA")
            require(isinstance(item.get("tags", []), list) and
                    all(isinstance(x, str) for x in item.get("tags", [])), "INVALID_TAGS")
            if item.get("enabled", True):
                managers.append(Manager(identifier, item.get("display_name") or identifier,
                                        item.get("family_id"), item.get("relationship_source"),
                                        item.get("strategy_group"), item.get("strategy_source")))
        require(bool(managers), "EMPTY_UNIVERSE", "Supply your institution list; no default pool")
        managers.sort(key=lambda m: m.cik)
        membership = [(m.cik, m.family_id, m.relationship_source) for m in managers]
        return cls(value["universe_id"], tuple(managers), digest(membership), digest(value))

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            data = stream.read(1_000_001)
        require(len(data) <= 1_000_000, "CONFIG_TOO_LARGE")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            try:
                import yaml
            except ImportError:
                raise ResearchError("YAML_UNAVAILABLE",
                                    "Use a JSON institution list or an existing interpreter with PyYAML") from None
            try:
                value = yaml.safe_load(data)
            except yaml.YAMLError:
                raise ResearchError("INVALID_YAML") from None
        return cls.from_mapping(value)

    def public_summary(self):
        return {"universe_id": self.universe_id, "universe_version": self.version,
                "config_revision": self.config_revision,
                "managers": [{"cik": m.cik, "name": m.display_name,
                              "sampling_unit": m.sampling_unit,
                              "independence_verified": m.independent,
                              "strategy_group": m.strategy_group if m.strategy_source else None}
                             for m in self.managers]}

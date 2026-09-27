"""Service registry (D25): the one list of services the platform knows about."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "services.yaml"


@dataclass(frozen=True)
class Service:
    name: str
    repo: str
    path_prefix: str
    port: int
    health_path: str

    @property
    def db_name(self) -> str:
        """Logical database (and DB user) for this service, e.g. service-a -> service_a."""
        return self.name.replace("-", "_")

    @property
    def db_reader(self) -> str:
        """Read-only DB user that previews use to copy this service's data from main."""
        return f"{self.db_name}_reader"


@dataclass(frozen=True)
class Registry:
    github_owner: str
    services: tuple[Service, ...]

    def service(self, name: str) -> Service:
        for svc in self.services:
            if svc.name == name:
                return svc
        raise KeyError(f"unknown service {name!r}; registered: {[s.name for s in self.services]}")


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> Registry:
    raw = yaml.safe_load(path.read_text())
    services = tuple(Service(**entry) for entry in raw["services"])
    names = [s.name for s in services]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate service names in {path}: {names}")
    owner = os.environ.get("PREVIEW_ENV_GITHUB_OWNER") or raw["github_owner"]
    return Registry(github_owner=owner, services=services)

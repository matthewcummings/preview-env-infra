"""EnvSpec: the fully resolved description of one environment, handed from the reconciler to CDK."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceSpec:
    ref: str  # branch the service runs, e.g. "preview/checkout/api" or "main"
    sha: str  # full commit SHA
    image: str  # image reference, ideally pinned by digest: <repo>@sha256:...


@dataclass(frozen=True)
class EnvSpec:
    env: str  # "main" or a group name
    services: dict[str, ServiceSpec]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> EnvSpec:
        raw = json.loads(text)
        return cls(
            env=raw["env"],
            services={name: ServiceSpec(**svc) for name, svc in raw["services"].items()},
        )

    def write(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n")

    @classmethod
    def read(cls, path: Path) -> EnvSpec:
        return cls.from_json(path.read_text())

"""信息源注册表加载：读 backend/config/sources.yaml（配置数据，非代码，见 04 §2.1）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from worker.schemas import SourceConfig

SOURCES_YAML_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"


@lru_cache(maxsize=1)
def load_sources() -> dict[str, SourceConfig]:
    raw = yaml.safe_load(SOURCES_YAML_PATH.read_text(encoding="utf-8"))
    sources = [SourceConfig(**item) for item in raw["sources"]]
    return {source.name: source for source in sources}


def get_source(name: str) -> SourceConfig:
    sources = load_sources()
    if name not in sources:
        raise KeyError(f"信息源注册表里没有 {name} 这条记录（backend/config/sources.yaml）")
    return sources[name]

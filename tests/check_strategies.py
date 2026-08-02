"""Check all 10 strategies metadata and requirements."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json

from backend.strategies.registry import get_registry
from backend.config import settings

registry = get_registry()
strategies_dir = settings.PROJECT_ROOT / "backend" / "strategies"
registry.scan_directory(strategies_dir)

all_meta = registry.list_all()
print(f"Total strategies: {len(all_meta)}")
print("=" * 100)

for meta in all_meta:
    modes = [m.value for m in meta.supported_modes]
    params_info = []
    for p in meta.params:
        default_str = json.dumps(p.default, ensure_ascii=False) if not isinstance(p.default, str) else p.default
        params_info.append(f"{p.name}={default_str}")

    print(f"\n{'─' * 60}")
    print(f"ID: {meta.strategy_id}")
    print(f"Name: {meta.display_name}")
    print(f"Category: {meta.category.value}")
    print(f"Modes: {', '.join(modes)}")
    print(f"Requires training: {meta.requires_training}")
    print(f"Retrain frequency: {meta.retrain_frequency.value}")
    print(f"Max position: {meta.max_position_pct}")
    print(f"Params: {', '.join(params_info)}")
    print(f"Sub-strategies: {len(meta.sub_strategies)}")

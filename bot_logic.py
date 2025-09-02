import json
import random
from pathlib import Path

# Загружаем сид-тосты (позже заменим на базу данных)
_TOASTS = json.loads(Path("toasts_seed.json").read_text(encoding="utf-8"))

def random_toast(category: str | None = None) -> str:
    cats = list(_TOASTS.keys())
    if not category or category not in _TOASTS:
        category = random.choice(cats)
    return random.choice(_TOASTS[category])

"""Configurable warnings for unusual or potentially unreliable model plans."""
from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from app.core.content_intelligence import ContentSuggestion


@dataclass(frozen=True)
class PlanSanity:
    warnings: tuple[str, ...]
    requires_confirmation: bool


def evaluate_plan(suggestions: Iterable[ContentSuggestion]) -> PlanSanity:
    items = list(suggestions)
    if not items:
        return PlanSanity((), False)
    total = len(items)
    targets = Counter(item.target_path for item in items)
    warnings = []

    max_folders = int(
        os.getenv(
            "ORGANIZER_MAX_PLAN_FOLDERS",
            str(max(8, math.ceil(total * 0.35))),
        )
    )
    if len(targets) > max_folders:
        warnings.append(
            f"The plan creates {len(targets)} destination folders for {total} files "
            f"(configured limit {max_folders})."
        )

    concentration = float(os.getenv("ORGANIZER_PLAN_CONCENTRATION", "0.85"))
    if total >= 10 and max(targets.values()) / total >= concentration:
        name, count = targets.most_common(1)[0]
        warnings.append(
            f"{count} of {total} files target one folder ({name}); verify the categories."
        )

    misc_count = sum(
        1 for item in items if item.category.casefold() == "misc"
    )
    misc_ratio = float(os.getenv("ORGANIZER_PLAN_MISC_RATIO", "0.4"))
    if total >= 5 and misc_count / total >= misc_ratio:
        warnings.append(
            f"{misc_count} of {total} files were assigned to Misc; evidence or provider output may be weak."
        )

    one_file_folders = sum(1 for count in targets.values() if count == 1)
    if total >= 10 and one_file_folders / max(1, len(targets)) >= 0.5:
        warnings.append(
            f"{one_file_folders} proposed folders contain only one file; review taxonomy sprawl."
        )

    return PlanSanity(tuple(warnings), bool(warnings))

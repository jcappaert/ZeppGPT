"""Evidence-backed Zepp workout type mappings.

Only IDs corroborated by payload structure and compatible implementations of
the Zepp workout API are named. Unknown IDs remain representable.
"""

from __future__ import annotations

from .models import WorkoutType

_KNOWN_TYPES = {
    1: WorkoutType(1, "running", "running", True),
    6: WorkoutType(6, "walking", "walking", True),
    14: WorkoutType(14, "indoor_pool_swimming", "swimming", True),
    # Corroborated by a compatible Zepp workout-history implementation and the
    # API's hiking-specific climb and elevation fields.
    22: WorkoutType(22, "hiking", "hiking", True),
    # Corroborated by rowing distance, stroke totals/rates, pull time, and
    # return time. Huami's device workout enumeration also assigns 0x17 (23)
    # to RowingMachine.
    23: WorkoutType(23, "rowing_machine", "rowing", True),
}


def resolve_workout_type(zepp_type_id: int | None) -> WorkoutType:
    if zepp_type_id in _KNOWN_TYPES:
        return _KNOWN_TYPES[zepp_type_id]
    return WorkoutType(zepp_type_id, "unknown", "unknown", False)

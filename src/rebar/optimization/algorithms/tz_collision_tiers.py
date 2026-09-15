"""Finite opt-in Z proposals; acceptance requires the independent tier checker."""
import math
from time import perf_counter

from ..services.shaped_collisions import check_shaped_collisions
from ..services.tz_collision_tiers import (
    ELIGIBLE_DIRECTIONS, MAXIMUM_CHANGES, PROFILE_ID, bar_key, check_xy_inventory,
    make_second_tier, outside_ids, pair_keys, validate_baseline,
)
from ..services.tz_outer_scope import check_tz_outer_bar


def propose_tz_collision_tiers(before, lanes, problem, actual_host, *, maximum_candidate_changes=64, time_limit_s=120):
    if (type(maximum_candidate_changes) is not int or not 1 <= maximum_candidate_changes <= MAXIMUM_CHANGES
            or isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float))
            or not math.isfinite(time_limit_s) or not 0 < time_limit_s <= 120):
        raise ValueError("The Z hypothesis requires bounded 1..64 attempts and 0..120 seconds")
    sources = validate_baseline(before, lanes, problem, actual_host)
    pairs = check_shaped_collisions(before)
    outer = set(outside_ids(before, actual_host))
    current, attempts, started = list(before), [], perf_counter()
    budget_exhausted = False
    for index, original in enumerate(before):
        proven, uncertain = (pair_keys(pairs, c) for c in ("proven_collision_pairs", "uncertain_pairs"))
        if (str(original.direction) not in ELIGIBLE_DIRECTIONS or original.shape_kind != "straight"
                or not any(bar_key(original) in pair for pair in proven | uncertain)):
            continue
        if len(attempts) >= maximum_candidate_changes or perf_counter()-started >= time_limit_s:
            budget_exhausted = True
            break
        candidate = make_second_tier(original)
        trial = [*current]
        trial[index] = candidate
        check_xy_inventory(before, tuple(trial), sources)
        record = {"direction": bar_key(original)[0], "bar_id": original.id}
        if check_tz_outer_bar(candidate, actual_host)["status"] != "pass" and bar_key(candidate) not in outer:
            record["outcome"] = "rollback_new_external_body_failure"
        else:
            proposed = check_shaped_collisions(tuple(trial))
            new_proven, new_uncertain = (pair_keys(proposed, c) for c in ("proven_collision_pairs", "uncertain_pairs"))
            if new_proven-proven or new_uncertain-uncertain:
                record.update(outcome="rollback_new_or_uncertain_3D_pair",
                    new_proven_pairs=sorted(new_proven-proven), new_uncertain_pairs=sorted(new_uncertain-uncertain))
            elif any(bar_key(candidate) in pair for pair in new_proven | new_uncertain):
                record["outcome"] = "rollback_changed_bar_still_conflicts"
            elif len(new_proven)+len(new_uncertain) >= len(proven)+len(uncertain):
                record["outcome"] = "rollback_no_conflict_improvement"
            else:
                current, pairs = trial, proposed
                record["outcome"] = "proposed_second_tier"
        attempts.append(record)
    after = tuple(current)
    check_xy_inventory(before, after, sources)
    return after, {"profile_id": PROFILE_ID, "attempts": attempts, "candidate_changes": len(attempts),
        "maximum_candidate_changes": maximum_candidate_changes, "time_limit_s": time_limit_s,
        "runtime_s": perf_counter()-started, "budget_exhausted": budget_exhausted,
        "global_optimality_proven": False, "independent_final_checker_required": True,
        "placement_eligible": False, "engineering_approval": False}

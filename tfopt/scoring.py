from __future__ import annotations

from models import Job


def drop_penalty(job: Job) -> int:
    """
    Higher penalty means the solver should work harder to keep the job.

    Assumptions used for this dataset:
    - higher numeric priority is more important
    - higher delivery_preference is more important
    - jobs with known area ownership are slightly more protected
    - larger orders matter, but should not dominate explicit business priority
    """
    penalty = 1_000_000
    penalty += (job.priority or 0) * 120_000
    penalty += (job.delivery_preference or 0) * 25_000
    penalty += min(job.quantity, 50) * 2_000
    penalty += int(job.weight_int * 600)

    normalized_type = job.job_type.lower()
    if normalized_type == "medium":
        penalty += 40_000
    elif normalized_type == "small":
        penalty += 15_000

    if job.has_area_id:
        penalty += 60_000
    else:
        penalty -= 120_000

    return max(100_000, penalty)

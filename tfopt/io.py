from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, List, Sequence

from models import Job


def normalize_columns(fieldnames: Sequence[str]) -> Dict[str, str]:
    """Normalize CSV headers so schema matching is case/space tolerant."""
    return {name.strip().lower(): name for name in fieldnames}


def load_jobs(csv_path: Path) -> List[Job]:
    """Load CSV rows into typed Job records using the project schema aliases."""
    jobs: List[Job] = []

    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Input CSV is missing a header row.")

        field_map = normalize_columns(reader.fieldnames)

        lat_key = field_map.get("lat", field_map.get("latitude"))
        lon_key = field_map.get("lon", field_map.get("longitude"))
        weight_key = field_map.get("weight", field_map.get("total_weight"))
        area_key = field_map.get("area_id")
        if not all([lat_key, lon_key, weight_key, area_key]):
            raise ValueError(
                "CSV must contain latitude/longitude, total_weight/weight, and area_id columns."
            )

        job_id_key = field_map.get("job_id", field_map.get("user_id"))
        name_key = field_map.get("name", field_map.get("store_name"))
        quantity_key = field_map.get("total_quantity")
        priority_key = field_map.get("priority")
        delivery_key = field_map.get("delivery_preference")
        type_key = field_map.get("types")

        for row_num, row in enumerate(reader, start=2):
            raw_id = str(row[job_id_key]).strip() if job_id_key else f"job_{row_num - 1}"
            raw_name = str(row[name_key]).strip() if name_key else raw_id
            priority = None
            if priority_key and str(row[priority_key]).strip():
                priority = int(float(row[priority_key]))
            delivery_preference = None
            if delivery_key and str(row[delivery_key]).strip():
                delivery_preference = int(float(row[delivery_key]))
            area_id = str(row[area_key]).strip()

            jobs.append(
                Job(
                    job_id=raw_id,
                    name=raw_name,
                    lat=float(row[lat_key]),
                    lon=float(row[lon_key]),
                    weight=float(row[weight_key]),
                    area_id=area_id,
                    quantity=int(float(row[quantity_key])) if quantity_key else 1,
                    priority=priority,
                    delivery_preference=delivery_preference,
                    job_type=str(row[type_key]).strip() if type_key else "",
                    has_area_id=bool(area_id),
                )
            )

    return jobs


def write_json(data: object, output_path: Path) -> None:
    """Write pretty-printed JSON to disk."""
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

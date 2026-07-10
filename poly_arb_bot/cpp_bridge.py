import csv
import subprocess
from io import StringIO
from pathlib import Path
from typing import Iterable, List

from .models import PositionCurve
from .pnl_curve import PnlCurve, calculate_curve


def score_positions_cpp(positions: Iterable[PositionCurve], exe_path: Path, require_cpp: bool = False) -> List[PnlCurve]:
    positions = list(positions)
    if not exe_path.exists():
        if require_cpp:
            raise FileNotFoundError(f"C++ engine not found: {exe_path}")
        return [calculate_curve(position) for position in positions]

    payload = StringIO()
    writer = csv.writer(payload, delimiter="\t", lineterminator="\n")
    writer.writerow(["market_id", "title", "up_shares", "up_cost", "down_shares", "down_cost"])
    for position in positions:
        writer.writerow([
            position.market_id,
            position.title,
            position.up_shares,
            position.up_cost,
            position.down_shares,
            position.down_cost,
        ])

    result = subprocess.run(
        [str(exe_path)],
        input=payload.getvalue(),
        text=True,
        capture_output=True,
        check=True,
    )
    reader = csv.DictReader(StringIO(result.stdout), delimiter="\t")
    return [
        PnlCurve(
            market_id=row["market_id"],
            title=row["title"],
            total_cost=float(row["total_cost"]),
            pnl_if_up=float(row["pnl_if_up"]),
            pnl_if_down=float(row["pnl_if_down"]),
            classification=row["classification"],
        )
        for row in reader
    ]

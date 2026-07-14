import gzip
from pathlib import Path


def history_paths(path):
    path = Path(path)
    rotated = []
    for candidate in path.parent.glob(path.name + ".*"):
        suffix = candidate.name[len(path.name) + 1:].split(".", 1)[0]
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    return [candidate for _, candidate in sorted(rotated, reverse=True)] + [path]


def open_history(path):
    return gzip.open(path, "rt", encoding="utf-8") if str(path).endswith(".gz") else Path(path).open(encoding="utf-8")

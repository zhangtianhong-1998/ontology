"""Acquire KPIOnto from its publisher and verify the pinned ontology file."""

import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen


COMMIT = "1c36644a40123447fb6469b9832865b4b4c2f7ba"
URL = f"https://raw.githubusercontent.com/KDMG/kpionto/{COMMIT}/kpionto.ttl"
SHA256 = "1cb6a3a81ecaeb76d2ef592181d80339ce98c09bac60f8558e8f37a0b7cebef2"
ROUTES = ("route1/metadata_with_ontology", "route2", "route3")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="previously acquired local kpionto.ttl")
    source.add_argument("--download", action="store_true", help="fetch directly from upstream")
    args = parser.parse_args()

    if args.download:
        with urlopen(URL, timeout=30) as response:
            data = response.read()
    else:
        data = args.source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        parser.error(f"SHA-256 mismatch: {digest}")

    root = Path(__file__).resolve().parents[1]
    for route in ROUTES:
        target = root / route / "ontologies/KPIOnto/kpionto.ttl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(target)


if __name__ == "__main__":
    main()

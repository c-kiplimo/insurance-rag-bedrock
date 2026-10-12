"""Generate SYNTHETIC claim notes containing fake PHI/PII.

Public insurance documents contain no personal data, so this exists to exercise the redaction
path end-to-end. Every identifier is invented (555-01xx phone numbers, 900-series SSNs, which
are never issued). Nothing here refers to a real person.
"""

from __future__ import annotations

import random
from pathlib import Path

_FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery"]
_LAST = ["Rivera", "Nguyen", "Okafor", "Lindqvist", "Haddad", "Kowalski", "Mbeki", "Tanaka"]
_STREETS = ["Maple Ave", "Harbor Rd", "Cedar Ln", "Lakeview Dr", "Oak St"]
_LOSS = [
    "Water entered the ground floor after heavy rain; about 4 inches stood in the basement.",
    "River overflow reached the foundation; the furnace and water heater in the basement failed.",
    "Storm surge flooded the first floor; drywall and flooring need replacement.",
    "Insured reports sump pump failure during a flood event and damage to a finished basement.",
]
_NOTES = [
    "Adjuster asked whether the finished basement improvements are covered under the policy.",
    "Insured asked if personal belongings stored in the basement are covered.",
    "Proof of loss was discussed; insured was reminded of the filing deadline in the policy.",
    "Insured asked about the deductible and how it applies to the building claim.",
]


def generate(out_dir: Path, count: int = 25, seed: int = 7) -> list[Path]:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(1, count + 1):
        name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        ssn = f"900-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"
        phone = f"555-01{rng.randint(0, 99):02d}"
        phone = f"(555) {phone}"
        text = (
            f"# SYNTHETIC claim file CLM-{1000 + i}\n\n"
            f"Insured: {name}\n"
            f"SSN: {ssn}\n"
            f"Phone: {phone}\n"
            f"Email: {name.split()[0].lower()}.{name.split()[1].lower()}@example.com\n"
            f"DOB: {rng.randint(1, 12)}/{rng.randint(1, 28)}/{rng.randint(1950, 1995)}\n"
            f"Address: {rng.randint(10, 999)} {rng.choice(_STREETS)}\n"
            f"MRN: {rng.randint(100000, 999999)}\n\n"
            f"## Loss description\n\n{rng.choice(_LOSS)}\n\n"
            f"## Adjuster notes\n\n{rng.choice(_NOTES)} "
            f"Insured {name} can be reached at {phone}.\n"
        )
        path = out_dir / f"synthetic-claim-{i:03d}.md"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths

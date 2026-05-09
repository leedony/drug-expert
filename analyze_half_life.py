import argparse
import logging
import re
import signal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)


HALF_LIFE_PATTERNS = [
    re.compile(
        r"(?i)\b(?:terminal\s+)?half[-\s]?life(?:\s*\(t1/2\))?\s*(?:was|is|of|:|=)?\s*(approximately\s+|about\s+|~\s*)?([0-9]+(?:\.[0-9]+)?)\s*(hours?|hrs?|h|days?|d|weeks?|w)\b"
    ),
    re.compile(
        r"(?i)\bt1/2\s*(?:was|is|of|:|=)?\s*(approximately\s+|about\s+|~\s*)?([0-9]+(?:\.[0-9]+)?)\s*(hours?|hrs?|h|days?|d|weeks?|w)\b"
    ),
]


def normalize_unit(unit: str) -> str:
    u = unit.lower()
    if u in {"hour", "hours", "hr", "hrs", "h"}:
        return "hour"
    if u in {"day", "days", "d"}:
        return "day"
    if u in {"week", "weeks", "w"}:
        return "week"
    return unit


def unit_to_hours(value: float, unit: str) -> float:
    u = normalize_unit(unit)
    if u == "hour":
        return value
    if u == "day":
        return value * 24.0
    if u == "week":
        return value * 168.0
    return value


def extract_text_chunks(pdf_path: Path, max_pages: int = 6) -> List[str]:
    chunks: List[str] = []
    def _timeout_handler(signum, frame):
        raise TimeoutError("pdf parse timeout")
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(8)
        reader = PdfReader(str(pdf_path))
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)
    except Exception:
        return []
    finally:
        signal.alarm(0)
    return chunks


def extract_half_life_mentions(text: str) -> List[Tuple[float, str, str]]:
    hits: List[Tuple[float, str, str]] = []
    lines = text.splitlines()
    for line in lines:
        line_clean = re.sub(r"\s+", " ", line).strip()
        if not line_clean:
            continue
        for pattern in HALF_LIFE_PATTERNS:
            for m in pattern.finditer(line_clean):
                value = float(m.group(2))
                unit = normalize_unit(m.group(3))
                hits.append((value, unit, line_clean))
    return hits


def pick_best_hit(hits: List[Tuple[float, str, str]]) -> Optional[Tuple[float, str, str]]:
    if not hits:
        return None
    # Heuristic: choose the median in hour-converted space to avoid outlier mentions.
    ordered = sorted(hits, key=lambda x: unit_to_hours(x[0], x[1]))
    return ordered[len(ordered) // 2]


def analyze_drug_folder(drug_dir: Path) -> Dict[str, str]:
    pdfs = sorted(drug_dir.glob("*.pdf"))
    all_hits: List[Tuple[float, str, str, str]] = []
    scanned_files = 0
    for pdf in pdfs:
        scanned_files += 1
        chunks = extract_text_chunks(pdf)
        for chunk in chunks:
            mentions = extract_half_life_mentions(chunk)
            for value, unit, sentence in mentions:
                all_hits.append((value, unit, sentence, pdf.name))

    if not all_hits:
        return {
            "drug_folder": drug_dir.name,
            "pdf_count": str(len(pdfs)),
            "scanned_files": str(scanned_files),
            "half_life_value": "",
            "half_life_unit": "",
            "half_life_hours": "",
            "source_file": "",
            "evidence": "",
            "status": "not_found_in_downloaded_pdfs",
        }

    best = pick_best_hit([(v, u, s) for v, u, s, _ in all_hits])
    assert best is not None
    best_value, best_unit, best_sentence = best
    source_file = next((f for v, u, s, f in all_hits if v == best_value and u == best_unit and s == best_sentence), "")
    return {
        "drug_folder": drug_dir.name,
        "pdf_count": str(len(pdfs)),
        "scanned_files": str(scanned_files),
        "half_life_value": str(best_value),
        "half_life_unit": best_unit,
        "half_life_hours": f"{unit_to_hours(best_value, best_unit):.2f}",
        "source_file": source_file,
        "evidence": best_sentence[:1000],
        "status": "found",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract drug half-life from downloaded PDFs")
    parser.add_argument(
        "--project-dir",
        default="/home/test/work/pubmed/projects/cortellis_full_project",
        help="Project root containing drugs/ and reports/",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    drugs_dir = project_dir / "drugs"
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, str]] = []
    drug_dirs = sorted([d for d in drugs_dir.iterdir() if d.is_dir()])
    total = len(drug_dirs)
    for idx, drug_dir in enumerate(drug_dirs, start=1):
        row = analyze_drug_folder(drug_dir)
        rows.append(row)
        if idx % 100 == 0 or idx == total:
            print(f"processed {idx}/{total}", flush=True)

    df = pd.DataFrame(rows)
    output = reports_dir / "drug_half_life_report.xlsx"
    df.to_excel(output, index=False)
    print(f"written {output}", flush=True)


if __name__ == "__main__":
    main()

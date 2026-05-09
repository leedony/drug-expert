import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd

from app import api_download, safe_name
from analyze_half_life import analyze_drug_folder


def parse_main_drug_name(row: pd.Series) -> str:
    generic = str(row.get("Generic Name", "") or "").strip()
    brand = str(row.get("Brand Name", "") or "").strip()
    code = str(row.get("Code Name", "") or "").strip()
    cand = generic or brand or code
    cand = cand.split("\n")[0].strip()
    cand = re.sub(r"\s*\(.*?\)\s*", " ", cand).strip()
    cand = re.sub(r"\s+", " ", cand).strip()
    return cand


def load_state(path: Path) -> Dict[str, Dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: Dict[str, Dict]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Full pipeline: download literature + extract half-life")
    parser.add_argument("--xlsx", required=True, help="Input xlsx path")
    parser.add_argument("--sheet", default="Product List", help="Sheet containing drug info")
    parser.add_argument("--per-drug", type=int, default=10, help="Max OA papers per drug")
    parser.add_argument("--sleep", type=float, default=0.5, help="Seconds between drugs")
    parser.add_argument("--max-drugs", type=int, default=0, help="Limit records for testing, 0 means all")
    parser.add_argument(
        "--project-dir",
        default="/home/test/work/pubmed/projects/cortellis_full_project",
        help="Project output directory",
    )
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")

    project_dir = Path(args.project_dir)
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    state_path = reports_dir / "pipeline_half_life_state.json"
    output_xlsx = reports_dir / "pipeline_half_life_summary.xlsx"

    state = load_state(state_path)

    df = pd.read_excel(xlsx_path, sheet_name=args.sheet)
    if "Entry Number" not in df.columns:
        raise ValueError("Expected column: Entry Number")
    df = df.drop_duplicates(subset=["Entry Number"]).reset_index(drop=True)
    if args.max_drugs and args.max_drugs > 0:
        df = df.head(args.max_drugs)

    total = len(df)
    for idx, row in df.iterrows():
        entry_number = str(row.get("Entry Number", "")).strip()
        drug_name = parse_main_drug_name(row)
        if not drug_name:
            continue
        key = f"{entry_number}:{drug_name}"
        if state.get(key, {}).get("done"):
            continue

        category = safe_name(drug_name, 100)
        try:
            dl = api_download(term=drug_name, max_results=args.per_drug, category=category)
            folder = Path("/home/test/work/pubmed/downloads") / category
            half = analyze_drug_folder(folder)

            row_result = {
                "done": True,
                "entry_number": entry_number,
                "drug_name": drug_name,
                "category": category,
                "downloaded_count": dl.get("downloaded_count", 0),
                "deduped_count": dl.get("deduped_count", 0),
                "skipped_count": dl.get("skipped_count", 0),
                "failed_count": dl.get("failed_count", 0),
                "half_life_status": half.get("status", ""),
                "half_life_value": half.get("half_life_value", ""),
                "half_life_unit": half.get("half_life_unit", ""),
                "half_life_hours": half.get("half_life_hours", ""),
                "half_life_source_file": half.get("source_file", ""),
                "half_life_evidence": half.get("evidence", ""),
            }
            state[key] = row_result
            save_state(state_path, state)

            # persist incremental summary each drug
            pd.DataFrame(list(state.values())).to_excel(output_xlsx, index=False)
            print(
                f"[{idx+1}/{total}] {drug_name}: dl={row_result['downloaded_count']}, "
                f"dedup={row_result['deduped_count']}, half={row_result['half_life_value']} {row_result['half_life_unit']}",
                flush=True,
            )
            time.sleep(max(args.sleep, 0))
        except Exception as e:
            state[key] = {
                "done": False,
                "entry_number": entry_number,
                "drug_name": drug_name,
                "category": category,
                "error": str(e),
            }
            save_state(state_path, state)
            pd.DataFrame(list(state.values())).to_excel(output_xlsx, index=False)
            print(f"[{idx+1}/{total}] {drug_name}: ERROR {e}", flush=True)

    print(f"Pipeline finished. Summary: {output_xlsx}", flush=True)
    print(f"State file: {state_path}", flush=True)


if __name__ == "__main__":
    main()

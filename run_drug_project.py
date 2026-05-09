import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable, TypeVar

import pandas as pd
import requests

from app import (
    BASE_DIR,
    download_pdf,
    fetch_pmcids_for_pmids,
    fetch_pubmed_metadata,
    find_existing_record,
    load_download_index,
    normalize,
    pdf_sha256,
    resolve_oa_pdf_link,
    resolve_pdf_link_aws,
    safe_name,
    save_download_index,
    upsert_record,
    esearch_pubmed,
)

T = TypeVar("T")


def with_retry(fn: Callable[[], T], retries: int = 3, base_sleep: float = 1.2) -> T:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:  # network/transient upstream issues
            last_exc = e
            if attempt < retries - 1:
                time.sleep(base_sleep * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def parse_main_drug_name(row: pd.Series) -> str:
    generic = str(row.get("Generic Name", "") or "").strip()
    brand = str(row.get("Brand Name", "") or "").strip()
    code = str(row.get("Code Name", "") or "").strip()

    cand = generic or brand or code
    cand = cand.split("\n")[0].strip()
    cand = re.sub(r"\s*\(.*?\)\s*", " ", cand).strip()
    cand = re.sub(r"\s+", " ", cand).strip()
    return cand


def build_query(row: pd.Series, fallback_name: str) -> str:
    generic = str(row.get("Generic Name", "") or "").strip().split("\n")[0]
    brand = str(row.get("Brand Name", "") or "").strip().split("\n")[0]
    code = str(row.get("Code Name", "") or "").strip().split("\n")[0]

    terms = [t.strip() for t in [generic, brand, code, fallback_name] if t and t.strip()]
    unique_terms: List[str] = []
    seen = set()
    for t in terms:
        key = normalize(t)
        if key and key not in seen:
            seen.add(key)
            unique_terms.append(t)

    if not unique_terms:
        return fallback_name
    if len(unique_terms) == 1:
        return f"\"{unique_terms[0]}\""
    return " OR ".join([f"\"{t}\"" for t in unique_terms[:4]])


def ensure_project_paths(project_dir: Path) -> Dict[str, Path]:
    project_dir.mkdir(parents=True, exist_ok=True)
    p = {
        "root": project_dir,
        "drugs": project_dir / "drugs",
        "reports": project_dir / "reports",
        "state": project_dir / "run_state.json",
        "permission_xlsx": project_dir / "reports" / "permission_required.xlsx",
        "summary_xlsx": project_dir / "reports" / "summary.xlsx",
    }
    p["drugs"].mkdir(parents=True, exist_ok=True)
    p["reports"].mkdir(parents=True, exist_ok=True)
    return p


def load_state(state_path: Path) -> Dict[str, Dict]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state_path: Path, state: Dict[str, Dict]) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Drug literature OA downloader project runner")
    parser.add_argument("--xlsx", required=True, help="Input drug excel path")
    parser.add_argument("--sheet", default="Product List", help="Sheet name containing drug records")
    parser.add_argument("--per-drug", type=int, default=8, help="Max OA PDFs per drug")
    parser.add_argument("--max-drugs", type=int, default=0, help="Limit drug count for testing, 0 means all")
    parser.add_argument("--sleep", type=float, default=0.7, help="Sleep seconds between drugs")
    parser.add_argument("--project-name", default="drug_literature_project", help="Output project folder name")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")

    project_dir = BASE_DIR / "projects" / safe_name(args.project_name, 120)
    paths = ensure_project_paths(project_dir)
    state = load_state(paths["state"])
    index = load_download_index()

    df = pd.read_excel(xlsx_path, sheet_name=args.sheet)
    if "Entry Number" not in df.columns:
        raise ValueError("Expected 'Entry Number' column not found")

    df = df.drop_duplicates(subset=["Entry Number"])
    df = df.reset_index(drop=True)
    if args.max_drugs and args.max_drugs > 0:
        df = df.head(args.max_drugs)

    permission_rows: List[Dict] = []
    summary_rows: List[Dict] = []

    for i, row in df.iterrows():
        entry_id = str(row.get("Entry Number"))
        drug_name = parse_main_drug_name(row)
        if not drug_name:
            continue

        state_key = f"{entry_id}:{drug_name}"
        if state.get(state_key, {}).get("done"):
            continue

        query = build_query(row, drug_name)
        drug_dir = paths["drugs"] / safe_name(drug_name, 100)
        drug_dir.mkdir(parents=True, exist_ok=True)

        downloaded_count = 0
        deduped_count = 0
        non_oa_count = 0
        failed_count = 0
        candidates_count = 0

        try:
            pmids = with_retry(lambda: esearch_pubmed(term=query, max_results=max(args.per_drug * 5, 30)))
            pubmed_papers = with_retry(lambda: fetch_pubmed_metadata(pmids))
            candidates_count = len(pubmed_papers)
            pmc_map = with_retry(lambda: fetch_pmcids_for_pmids(pmids))

            for p in pubmed_papers:
                if downloaded_count >= args.per_drug:
                    break
                pmcid = p.get("pmcid") or pmc_map.get(p.get("pmid", ""))
                if not pmcid:
                    non_oa_count += 1
                    permission_rows.append(
                        {
                            "entry_number": entry_id,
                            "drug_name": drug_name,
                            "query": query,
                            "pmid": p.get("pmid", ""),
                            "doi": p.get("doi", ""),
                            "title": p.get("title", ""),
                            "journal": p.get("journal", ""),
                            "year": p.get("year", ""),
                            "reason": "No PMCID/OA not available automatically",
                        }
                    )
                    continue

                p_with_pmc = {**p, "pmcid": pmcid}
                existing = find_existing_record(index, p_with_pmc)
                if existing:
                    deduped_count += 1
                    continue

                pdf_url: Optional[str] = with_retry(
                    lambda: (resolve_pdf_link_aws(pmcid) or resolve_oa_pdf_link(pmcid)),
                    retries=2,
                    base_sleep=0.8,
                )
                if not pdf_url:
                    non_oa_count += 1
                    permission_rows.append(
                        {
                            "entry_number": entry_id,
                            "drug_name": drug_name,
                            "query": query,
                            "pmid": p.get("pmid", ""),
                            "doi": p.get("doi", ""),
                            "title": p.get("title", ""),
                            "journal": p.get("journal", ""),
                            "year": p.get("year", ""),
                            "reason": "No OA PDF URL found",
                        }
                    )
                    continue

                filename = f"{p.get('year', 'unknown')}_{safe_name(p.get('pmid','unknown'))}_{safe_name(p.get('title','untitled'), 60)}.pdf"
                target = drug_dir / filename
                ok = with_retry(lambda: download_pdf(pdf_url, target), retries=2, base_sleep=0.8)
                if not ok:
                    if target.exists():
                        target.unlink()
                    failed_count += 1
                    continue

                checksum = pdf_sha256(target)
                upsert_record(index, p_with_pmc, str(target.relative_to(BASE_DIR)), checksum)
                downloaded_count += 1

            state[state_key] = {
                "done": True,
                "drug_name": drug_name,
                "entry_number": entry_id,
                "query": query,
                "candidates_count": candidates_count,
                "downloaded_count": downloaded_count,
                "deduped_count": deduped_count,
                "non_oa_count": non_oa_count,
                "failed_count": failed_count,
                "folder": str(drug_dir.relative_to(BASE_DIR)),
            }
            save_state(paths["state"], state)
            save_download_index(index)

            summary_rows.append(state[state_key])
            print(
                f"[{i+1}/{len(df)}] {drug_name}: downloaded={downloaded_count}, deduped={deduped_count}, non_oa={non_oa_count}, failed={failed_count}",
                flush=True,
            )
            time.sleep(max(args.sleep, 0))
        except requests.RequestException as e:
            state[state_key] = {"done": False, "drug_name": drug_name, "entry_number": entry_id, "error": str(e)}
            save_state(paths["state"], state)
            summary_rows.append(
                {
                    "done": False,
                    "drug_name": drug_name,
                    "entry_number": entry_id,
                    "query": query,
                    "candidates_count": candidates_count,
                    "downloaded_count": downloaded_count,
                    "deduped_count": deduped_count,
                    "non_oa_count": non_oa_count,
                    "failed_count": failed_count + 1,
                    "folder": str(drug_dir.relative_to(BASE_DIR)),
                    "error": str(e),
                }
            )

    # include prior run state in summary export for resumable runs
    all_summary = list(state.values())
    pd.DataFrame(all_summary).to_excel(paths["summary_xlsx"], index=False)
    pd.DataFrame(permission_rows).to_excel(paths["permission_xlsx"], index=False)
    save_download_index(index)
    print(f"Project completed/resumed. Output folder: {project_dir}", flush=True)
    print(f"Summary: {paths['summary_xlsx']}", flush=True)
    print(f"Permission required list: {paths['permission_xlsx']}", flush=True)


if __name__ == "__main__":
    main()

import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd

from app import api_download, safe_name
from analyze_half_life import analyze_drug_folder


def clean(v) -> str:
    if pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat"} else s


def top_aliases(row: pd.Series, limit: int = 3) -> List[str]:
    vals: List[str] = []
    for col in ["Generic Name", "Brand Name", "Code Name"]:
        raw = clean(row.get(col, ""))
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        vals.extend(parts[:2])
    uniq = []
    seen = set()
    for v in vals:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(v)
    return uniq[:limit]


def build_queries(aliases: List[str], max_queries: int = 6) -> List[str]:
    q: List[str] = []
    for a in aliases:
        q.append(a)
        q.append(f"{a} pharmacokinetics")
        q.append(f"{a} half-life")
        q.append(f"{a} t1/2")
        q.append(f"{a} phase I pharmacokinetics")
    # dedupe preserve order
    out = []
    seen = set()
    for x in q:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out[:max_queries]


def main() -> None:
    parser = argparse.ArgumentParser(description="Enhanced rerun for missing half-life drugs")
    parser.add_argument("--xlsx", required=True, help="Cortellis source xlsx")
    parser.add_argument(
        "--summary",
        default="/home/test/work/pubmed/projects/cortellis_full_project/reports/pipeline_half_life_summary.xlsx",
        help="Existing pipeline summary",
    )
    parser.add_argument(
        "--output",
        default="/home/test/work/pubmed/projects/cortellis_full_project/reports/pipeline_half_life_summary_enhanced.xlsx",
        help="Enhanced output summary",
    )
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep seconds per drug")
    parser.add_argument("--max-drugs", type=int, default=0, help="Limit rerun count")
    args = parser.parse_args()

    src = pd.read_excel(args.xlsx, sheet_name="Product List")
    old = pd.read_excel(args.summary)
    if "entry_number" not in old.columns:
        raise ValueError("summary missing entry_number")
    old["entry_number"] = old["entry_number"].astype(str)
    src["Entry Number"] = src["Entry Number"].astype(str)

    merged = old.merge(
        src[["Entry Number", "Generic Name", "Brand Name", "Code Name"]],
        left_on="entry_number",
        right_on="Entry Number",
        how="left",
    )

    need = merged[merged["half_life_status"] != "found"].copy()
    if args.max_drugs and args.max_drugs > 0:
        need = need.head(args.max_drugs)

    results: Dict[str, Dict] = {}
    total = len(need)
    for idx, row in enumerate(need.itertuples(index=False), start=1):
        entry = str(getattr(row, "entry_number"))
        drug_name = clean(getattr(row, "drug_name"))
        if not drug_name:
            continue
        category = safe_name(drug_name, 100)
        aliases = top_aliases(pd.Series(row._asdict()))
        if not aliases:
            aliases = [drug_name]
        queries = build_queries(aliases, max_queries=6)

        agg = {"downloaded": 0, "deduped": 0, "skipped": 0, "failed": 0}
        manual_links = []
        for q in queries:
            try:
                r = api_download(term=q, max_results=8, category=category)
            except Exception:
                continue
            agg["downloaded"] += r.get("downloaded_count", 0)
            agg["deduped"] += r.get("deduped_count", 0)
            agg["skipped"] += r.get("skipped_count", 0)
            agg["failed"] += r.get("failed_count", 0)
            for bucket in ("skipped", "failed"):
                for x in r.get(bucket, []):
                    doi = clean(x.get("doi", ""))
                    pmid = clean(x.get("pmid", ""))
                    if doi or pmid:
                        manual_links.append(
                            {
                                "doi": doi,
                                "pmid": pmid,
                                "doi_link": f"https://doi.org/{doi}" if doi else "",
                                "pubmed_link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                            }
                        )
            if agg["downloaded"] > 0 and agg["deduped"] > 20:
                break

        folder = Path("/home/test/work/pubmed/downloads") / category
        half = analyze_drug_folder(folder)
        # unique links
        seen = set()
        uniq_links = []
        for m in manual_links:
            k = (m["doi"], m["pmid"])
            if k in seen:
                continue
            seen.add(k)
            uniq_links.append(m)

        results[entry] = {
            "entry_number": entry,
            "drug_name": drug_name,
            "category": category,
            "enhanced_queries_used": "; ".join(queries[:8]),
            "enhanced_downloaded_count": agg["downloaded"],
            "enhanced_deduped_count": agg["deduped"],
            "enhanced_skipped_count": agg["skipped"],
            "enhanced_failed_count": agg["failed"],
            "manual_link_count": len(uniq_links),
            "half_life_status": half.get("status", ""),
            "half_life_value": half.get("half_life_value", ""),
            "half_life_unit": half.get("half_life_unit", ""),
            "half_life_hours": half.get("half_life_hours", ""),
            "half_life_source_file": half.get("source_file", ""),
            "half_life_evidence": half.get("evidence", ""),
        }

        print(
            f"[{idx}/{total}] {drug_name}: dl={agg['downloaded']}, dedup={agg['deduped']}, half={half.get('half_life_value','')} {half.get('half_life_unit','')}",
            flush=True,
        )
        time.sleep(max(args.sleep, 0))

    enhanced_df = pd.DataFrame(list(results.values()))
    old_base = old.copy()
    old_base["entry_number"] = old_base["entry_number"].astype(str)
    final = old_base.merge(enhanced_df, on=["entry_number", "drug_name"], how="left", suffixes=("", "_enh"))

    # prefer enhanced half-life when found
    final["half_life_status_final"] = final["half_life_status"]
    mask = final["half_life_status_enh"] == "found"
    final.loc[mask, "half_life_status_final"] = "found"
    final["half_life_value_final"] = final["half_life_value"]
    final["half_life_unit_final"] = final["half_life_unit"]
    final["half_life_hours_final"] = final["half_life_hours"]
    final["half_life_source_file_final"] = final["half_life_source_file"]
    final["half_life_evidence_final"] = final["half_life_evidence"]
    final["half_life_value_final"] = final["half_life_value_final"].astype(str)
    final["half_life_hours_final"] = final["half_life_hours_final"].astype(str)
    final.loc[mask, "half_life_value_final"] = final.loc[mask, "half_life_value_enh"].astype(str)
    final.loc[mask, "half_life_unit_final"] = final.loc[mask, "half_life_unit_enh"]
    final.loc[mask, "half_life_hours_final"] = final.loc[mask, "half_life_hours_enh"].astype(str)
    final.loc[mask, "half_life_source_file_final"] = final.loc[mask, "half_life_source_file_enh"]
    final.loc[mask, "half_life_evidence_final"] = final.loc[mask, "half_life_evidence_enh"]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    final.to_excel(out, index=False)

    # export enhanced-only rows
    enhanced_only = enhanced_df.sort_values(["half_life_status", "drug_name"], ascending=[False, True])
    enhanced_only.to_excel(out.with_name(out.stem + "_enhanced_only.xlsx"), index=False)
    Path(str(out) + ".state.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Merge per-drug dossier extracts into a copy of the FC master table (new file only)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from run_drug_expert_full_from_inn_table import parse_half_life_from_text

MASTER_XLSX = ROOT / "reports" / "halflife_fc_master_table_elimination_merged_filled_with_fc_subtype.xlsx"
DOSSIER_DIR = ROOT / "reports" / "drug_expert_full" / "per_drug_dossiers"
OUTPUT_XLSX = ROOT / "reports" / "halflife_fc_master_table_elimination_merged_filled_with_fc_subtype__dossier_summary.xlsx"


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip())[:180]


def dossier_path(primary: str) -> Path | None:
    for candidate in (DOSSIER_DIR / f"{safe_filename(primary)}.xlsx", DOSSIER_DIR / f"{primary}.xlsx"):
        if candidate.is_file() and not candidate.name.startswith(".~"):
            return candidate
    return None


def kv_summary(df: pd.DataFrame) -> dict[str, str]:
    if df.empty or "field" not in df.columns:
        return {}
    out = {}
    for _, r in df.iterrows():
        f, v = r.get("field"), r.get("value")
        if pd.notna(f):
            out[str(f)] = "" if pd.isna(v) else str(v)
    return out


def parse_half_life_range(text: str) -> dict | None:
    """Parse ranges like '5.5 to 7 days' -> midpoint in hours."""
    if not text:
        return None
    m = re.search(
        r"([\d.]+)\s*(?:to|–|-)\s*([\d.]+)\s*(day|days|d|hour|hours|hr|hrs|h|week|weeks|w)\b",
        text,
        re.I,
    )
    if not m:
        return None
    v1, v2, unit = float(m.group(1)), float(m.group(2)), m.group(3).lower()
    mid = (v1 + v2) / 2.0
    if unit.startswith("d"):
        hours = mid * 24
    elif unit.startswith("w"):
        hours = mid * 168
    else:
        hours = mid
    return {
        "half_life_value": mid,
        "half_life_unit": unit,
        "half_life_hours": round(hours, 3),
        "half_life_evidence_snippet": text[:200],
    }


def to_float(x) -> float | None:
    try:
        if pd.isna(x) or str(x).strip() == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def pick_best_half_life(hl: pd.DataFrame) -> dict:
    empty = {
        "dossier_hl_best_value": "",
        "dossier_hl_best_unit": "",
        "dossier_hl_best_hours": "",
        "dossier_hl_best_days": "",
        "dossier_hl_best_source": "",
        "dossier_hl_best_validation": "",
        "dossier_hl_best_notes": "",
    }
    if hl.empty or "validation_status" not in hl.columns:
        return empty

    false_pos = hl[hl["validation_status"].astype(str) == "FALSE_POSITIVE"]
    fp_val = false_pos.iloc[0]["value"] if not false_pos.empty else ""

    validated = hl[hl["validation_status"].astype(str) == "VALIDATED"].copy()
    pick = None
    if not validated.empty:
        local = validated[validated["source"].astype(str).str.contains("Local PDF", case=False, na=False)]
        pick = local.iloc[0] if not local.empty else validated.iloc[0]
    else:
        for pref in ("DrugBank", "DailyMed", "Skill"):
            sub = hl[hl["source"].astype(str).str.contains(pref, case=False, na=False)]
            if not sub.empty:
                pick = sub.iloc[0]
                break

    if pick is None:
        out = empty.copy()
        out["dossier_hl_report_false_positive"] = fp_val
        out["dossier_hl_has_false_positive_report"] = "Y" if fp_val != "" else "N"
        return out

    hours = to_float(pick.get("hours_equiv"))
    value = pick.get("value", "")
    unit = pick.get("unit", "")
    notes = str(pick.get("notes", "") or "")[:500]
    parsed = parse_half_life_from_text(f"{value} {notes}") or parse_half_life_range(f"{value} {notes}")
    if hours is None and parsed:
        hours = parsed.get("half_life_hours")
    if parsed:
        if not unit and parsed.get("half_life_unit"):
            unit = parsed.get("half_life_unit")
        if (not value or value == notes) and parsed.get("half_life_value"):
            value = parsed.get("half_life_value")

    days = round(hours / 24, 4) if hours is not None else ""

    validated_txt = "; ".join(
        f"{r['source']}: {r['value']} {r.get('unit','')} ({r.get('hours_equiv','')} h) [{r['validation_status']}]"
        for _, r in hl.iterrows()
        if str(r.get("validation_status")) in ("VALIDATED", "FALSE_POSITIVE")
    )[:3000]

    return {
        "dossier_hl_best_value": value,
        "dossier_hl_best_unit": unit,
        "dossier_hl_best_hours": hours if hours is not None else "",
        "dossier_hl_best_days": days,
        "dossier_hl_best_source": pick.get("source", ""),
        "dossier_hl_best_validation": pick.get("validation_status", ""),
        "dossier_hl_best_notes": notes,
        "dossier_hl_validated_summary": validated_txt,
        "dossier_hl_validated_count": int((hl["validation_status"].astype(str) == "VALIDATED").sum()),
        "dossier_hl_false_positive_count": int((hl["validation_status"].astype(str) == "FALSE_POSITIVE").sum()),
        "dossier_hl_report_false_positive": fp_val,
        "dossier_hl_has_false_positive_report": "Y" if fp_val != "" else "N",
        "dossier_hl_evidence_row_count": len(hl),
    }


def summarize_trials(ct: pd.DataFrame) -> dict:
    if ct.empty or (len(ct) == 1 and "note" in ct.columns):
        return {
            "dossier_trials_count": 0,
            "dossier_trials_nct_list": "",
            "dossier_trials_phases": "",
            "dossier_trials_statuses": "",
            "dossier_trials_enrollment_total": "",
            "dossier_trials_summary": "",
            "dossier_trials_top_titles": "",
        }
    if "nct_number" not in ct.columns:
        return {"dossier_trials_count": 0, "dossier_trials_summary": "malformed sheet"}

    parts: list[str] = []
    ncts: list[str] = []
    phases: set[str] = set()
    statuses: set[str] = set()
    enroll: list[int] = []
    for _, r in ct.iterrows():
        nct = str(r.get("nct_number", "") or "").strip()
        if not nct or nct.lower() == "nan":
            continue
        ncts.append(nct)
        phase = str(r.get("phase", "") or "")
        status = str(r.get("status", "") or "")
        n = r.get("enrollment", "")
        if phase:
            phases.add(phase)
        if status:
            statuses.add(status)
        parts.append(f"{nct} [{phase}] {status} (n={n})")
        try:
            if str(n).isdigit():
                enroll.append(int(n))
        except Exception:
            pass

    titles = ct.get("official_title", pd.Series(dtype=str)).dropna().astype(str).head(3).tolist()

    return {
        "dossier_trials_count": len(ncts),
        "dossier_trials_nct_list": "; ".join(ncts),
        "dossier_trials_phases": "; ".join(sorted(phases)),
        "dossier_trials_statuses": "; ".join(sorted(statuses)),
        "dossier_trials_enrollment_total": sum(enroll) if enroll else "",
        "dossier_trials_summary": " | ".join(parts[:12])[:4000],
        "dossier_trials_top_titles": " | ".join(t[:120] for t in titles)[:1500],
    }


def summarize_pubmed(pub: pd.DataFrame) -> dict:
    if pub.empty or (len(pub) == 1 and "note" in pub.columns):
        return {
            "dossier_pubmed_count": 0,
            "dossier_pubmed_pk_count": 0,
            "dossier_pubmed_pk_pmids": "",
            "dossier_pubmed_top_titles": "",
        }
    pk = pub[pub.get("pk_related", False) == True] if "pk_related" in pub.columns else pub.iloc[0:0]
    top = pub.head(5)
    return {
        "dossier_pubmed_count": len(pub),
        "dossier_pubmed_pk_count": len(pk),
        "dossier_pubmed_pk_pmids": "; ".join(pk.get("pmid", pd.Series(dtype=str)).astype(str).head(10)),
        "dossier_pubmed_top_titles": " | ".join(top.get("title", pd.Series(dtype=str)).astype(str).str[:100].head(5)),
    }


def summarize_nct_evidence(nct: pd.DataFrame) -> dict:
    if nct.empty or (len(nct) == 1 and "note" in nct.columns):
        return {
            "dossier_nct_evidence_rows": 0,
            "dossier_nct_efficacy_hits": 0,
            "dossier_nct_toxicity_hits": 0,
            "dossier_nct_linked_pmids": "",
        }
    eff = (nct.get("evidence_type", pd.Series(dtype=str)).astype(str) == "efficacy").sum()
    tox = (nct.get("evidence_type", pd.Series(dtype=str)).astype(str) == "toxicity").sum()
    pmids = nct.get("pmid", pd.Series(dtype=str)).dropna().astype(str)
    pmids = "; ".join(p for p in pmids.unique() if p and p != "nan")[:1500]
    return {
        "dossier_nct_evidence_rows": len(nct),
        "dossier_nct_efficacy_hits": int(eff),
        "dossier_nct_toxicity_hits": int(tox),
        "dossier_nct_linked_pmids": pmids,
    }


def extract_from_dossier(primary: str) -> dict:
    path = dossier_path(primary)
    base = {
        "dossier_file": str(path) if path else "",
        "dossier_found": "Y" if path else "N",
    }
    if not path:
        return base

    try:
        xl = pd.ExcelFile(path)
        summary = kv_summary(pd.read_excel(path, sheet_name="drug_summary"))
        hl = pd.read_excel(path, sheet_name="half_life_evidence") if "half_life_evidence" in xl.sheet_names else pd.DataFrame()
        ct = pd.read_excel(path, sheet_name="clinical_trials") if "clinical_trials" in xl.sheet_names else pd.DataFrame()
        pub = pd.read_excel(path, sheet_name="pubmed_full_abstracts") if "pubmed_full_abstracts" in xl.sheet_names else pd.DataFrame()
        nct = pd.read_excel(path, sheet_name="nct_pubmed_evidence") if "nct_pubmed_evidence" in xl.sheet_names else pd.DataFrame()
        pdf_ex = pd.read_excel(path, sheet_name="pdf_excerpts") if "pdf_excerpts" in xl.sheet_names else pd.DataFrame()
        inv = pd.read_excel(path, sheet_name="local_pdf_inventory") if "local_pdf_inventory" in xl.sheet_names else pd.DataFrame()
    except Exception as e:
        base["dossier_extract_error"] = str(e)[:300]
        return base

    base.update(pick_best_half_life(hl))
    base.update(summarize_trials(ct))
    base.update(summarize_pubmed(pub))
    base.update(summarize_nct_evidence(nct))
    base["dossier_pdf_excerpt_count"] = len(pdf_ex) if not pdf_ex.empty and "note" not in pdf_ex.columns else 0
    base["dossier_local_pdf_count"] = len(inv) if not inv.empty and "note" not in inv.columns else 0
    base["dossier_warning"] = summary.get("WARNING_report_half_life", summary.get("WARNING", ""))
    base["dossier_validated_pdf_hl_count"] = summary.get("validated_pdf_half_life_count", "")
    base["dossier_recommended_pdf_hl"] = summary.get("recommended_pdf_half_life", "")
    return base


def main() -> None:
    if not MASTER_XLSX.is_file():
        raise SystemExit(f"Master table not found: {MASTER_XLSX}")

    master = pd.read_excel(MASTER_XLSX)
    rows = []
    for i, primary in enumerate(master["Primary_name_clean"].astype(str).str.strip(), 1):
        if i % 50 == 0:
            print(f"  {i}/{len(master)}", flush=True)
        rows.append(extract_from_dossier(primary))

    enrich = pd.DataFrame(rows)
    out = pd.concat([master.reset_index(drop=True), enrich], axis=1)

    OUTPUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as w:
        out.to_excel(w, sheet_name="master_with_dossier", index=False)
        # audit: only dossier columns
        dossier_cols = ["Primary_name_clean"] + [c for c in out.columns if c.startswith("dossier_")]
        out[dossier_cols].to_excel(w, sheet_name="dossier_columns_only", index=False)

    print(f"Written: {OUTPUT_XLSX}")
    print(f"Rows: {len(out)}, dossier found: {(enrich['dossier_found']=='Y').sum()}")
    print(f"With best half-life hours: {(enrich['dossier_hl_best_hours']!='').sum()}")


if __name__ == "__main__":
    main()

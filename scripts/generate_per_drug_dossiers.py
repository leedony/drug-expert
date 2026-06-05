#!/usr/bin/env python3
"""Generate per-drug evidence dossier Excel files (chelomab-style logic).

Reads drugs from all_drugs_full_report.xlsx (summary_full + optional biosimilars),
validates PDF half-life matches against drug aliases, and writes one workbook per drug.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for p in (ROOT, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from analyze_half_life import extract_half_life_mentions, extract_text_chunks, unit_to_hours
from run_drug_expert_full_from_inn_table import has_drug_mention, norm, parse_half_life_from_text

REPORT_XLSX = ROOT / "reports" / "drug_expert_full" / "all_drugs_full_report.xlsx"
CT_CSV = ROOT / "reports" / "drug_expert_full" / "all_drugs_clinicaltrials.csv"
NCT_CSV = ROOT / "reports" / "drug_expert_full" / "all_drugs_nct_evidence.csv"
OUT_DIR = ROOT / "reports" / "drug_expert_full" / "per_drug_dossiers"
STATE_JSON = OUT_DIR / "_progress.json"
DOWNLOADS = ROOT / "downloads"
CORTELLIS_DRUGS = ROOT / "projects" / "cortellis_full_project" / "drugs"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CT_API = "https://clinicaltrials.gov/api/v2/studies"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "pubmed-drug-dossier-batch/1.0"

# openpyxl rejects some control chars in cell text
_ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_for_excel(obj):
    if isinstance(obj, str):
        return _ILLEGAL_XLSX_RE.sub("", obj)
    if isinstance(obj, dict):
        return {k: sanitize_for_excel(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_excel(v) for v in obj]
    return obj


def sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(lambda x: sanitize_for_excel(x) if isinstance(x, str) else x)
    return out


def safe_filename(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", (name or "unknown").strip())
    return s[:180] if s else "unknown"


def http_get(url: str, **kwargs):
    kwargs.setdefault("timeout", 90)
    for attempt in range(4):
        try:
            r = SESSION.get(url, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


def parse_aliases(row: pd.Series, primary: str) -> list[str]:
    names: list[str] = []
    if primary:
        names.append(str(primary).strip())
    for col in ("Generic Name", "Code Name", "Brand Name", "drug_name"):
        val = row.get(col)
        if pd.isna(val):
            continue
        for part in re.split(r"[;,/]", str(val)):
            part = re.sub(r"\([^)]*\)", "", part).strip()
            part = re.sub(r"\s+", " ", part)
            if len(part) >= 3:
                names.append(part)
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = norm(n)
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out


def mentions_any_alias(text: str, aliases: list[str]) -> bool:
    if not text:
        return False
    for a in aliases:
        if has_drug_mention(text, a):
            return True
    return False


def pmid_from_pdf_name(filename: str) -> str:
    m = re.search(r"_(\d{7,8})_", filename or "")
    return m.group(1) if m else ""


def scan_validated_pdf_hits(drug_dir: Path, aliases: list[str]) -> list[dict]:
    if not drug_dir.is_dir():
        return []
    rows = []
    for pdf in sorted(drug_dir.glob("*.pdf")):
        for chunk in extract_text_chunks(pdf, max_pages=8):
            chunk_norm = re.sub(r"\s+", " ", chunk)
            for value, unit, sentence in extract_half_life_mentions(chunk_norm):
                if not mentions_any_alias(sentence, aliases) and not mentions_any_alias(chunk_norm[:4000], aliases):
                    continue
                rows.append(
                    {
                        "source_file": pdf.name,
                        "local_path": str(pdf),
                        "pmid": pmid_from_pdf_name(pdf.name),
                        "half_life_value": value,
                        "half_life_unit": unit,
                        "half_life_hours": round(unit_to_hours(value, unit), 3),
                        "evidence_snippet": sentence[:800],
                        "drug_mention_validated": True,
                    }
                )
    return rows


def build_half_life_sheet(row: pd.Series, aliases: list[str], pdf_hits: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    primary = str(row.get("Primary_name_clean", ""))

    for label, col in (
        ("DrugBank (master table)", "half_life_DrugBank"),
        ("DailyMed (master table)", "half_life_DailyMed"),
        ("Skill / pipeline (master table)", "half_life_Skill"),
    ):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            parsed = parse_half_life_from_text(str(val))
            rows.append(
                {
                    "source": label,
                    "value": parsed["half_life_value"] if parsed else str(val),
                    "unit": parsed["half_life_unit"] if parsed else "",
                    "hours_equiv": parsed["half_life_hours"] if parsed else "",
                    "citation": col,
                    "validation_status": "master_table_text",
                    "notes": str(val)[:500],
                }
            )

    report_status = row.get("half_life_status")
    report_file = row.get("half_life_source_file")
    report_snip = row.get("half_life_evidence_snippet")
    if str(report_status) == "found" and pd.notna(report_file):
        valid = mentions_any_alias(str(report_snip or ""), aliases)
        rows.append(
            {
                "source": "all_drugs_full_report PDF match",
                "value": row.get("half_life_value", ""),
                "unit": row.get("half_life_unit", ""),
                "hours_equiv": row.get("half_life_hours", ""),
                "citation": str(report_file),
                "validation_status": "VALIDATED" if valid else "FALSE_POSITIVE",
                "notes": (str(report_snip or "")[:500] + (" — 证据句未提及该药别名" if not valid else "")),
            }
        )

    for hit in pdf_hits:
        rows.append(
            {
                "source": f"Local PDF ({hit['source_file']})",
                "value": hit["half_life_value"],
                "unit": hit["half_life_unit"],
                "hours_equiv": hit["half_life_hours"],
                "citation": f"https://pubmed.ncbi.nlm.nih.gov/{hit['pmid']}/" if hit.get("pmid") else hit["local_path"],
                "validation_status": "VALIDATED",
                "notes": hit["evidence_snippet"][:500],
            }
        )

    if not rows:
        rows.append(
            {
                "source": "none",
                "value": "",
                "unit": "",
                "hours_equiv": "",
                "citation": "",
                "validation_status": "not_found",
                "notes": "无 master 文本、无经校验的 PDF 半衰期",
            }
        )
    return pd.DataFrame(rows)


def build_pdf_excerpts(pdf_hits: list[dict], row: pd.Series, aliases: list[str]) -> pd.DataFrame:
    excerpts = []
    for hit in pdf_hits[:12]:
        excerpts.append(
            {
                "source_file": hit["source_file"],
                "pmid": hit.get("pmid", ""),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{hit['pmid']}/" if hit.get("pmid") else "",
                "topic": "Validated half-life mention",
                "extracted_text": hit["evidence_snippet"],
                "local_path": hit["local_path"],
            }
        )
    report_file = row.get("half_life_source_file")
    report_snip = row.get("half_life_evidence_snippet")
    if pd.notna(report_file) and str(report_file).strip():
        valid = mentions_any_alias(str(report_snip or ""), aliases)
        excerpts.append(
            {
                "source_file": str(report_file),
                "pmid": pmid_from_pdf_name(str(report_file)),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid_from_pdf_name(str(report_file))}/"
                if pmid_from_pdf_name(str(report_file))
                else "",
                "topic": "Report PDF match (VALIDATED)" if valid else "Report PDF match (FALSE_POSITIVE)",
                "extracted_text": str(report_snip or "")[:800],
                "local_path": str(DOWNLOADS / str(row.get("Primary_name_clean", "")) / str(report_file)),
            }
        )
    return pd.DataFrame(excerpts)


def pubmed_for_drug(primary: str, aliases: list[str], retmax: int = 15) -> pd.DataFrame:
    queries = [primary] + [a for a in aliases[1:4] if a != primary]
    seen: set[str] = set()
    pmids: list[str] = []
    for q in queries:
        if not q:
            continue
        try:
            r = http_get(
                f"{NCBI}/esearch.fcgi",
                params={"db": "pubmed", "term": q, "retmax": retmax, "retmode": "json"},
            )
            for pid in r.json().get("esearchresult", {}).get("idlist", []):
                if pid not in seen:
                    seen.add(pid)
                    pmids.append(pid)
        except Exception:
            continue
        time.sleep(0.11)

    if not pmids:
        return pd.DataFrame(columns=["pmid", "year", "journal", "title", "doi", "pubmed_url", "doi_url", "abstract_full_text", "pk_related"])

    rows = []
    for i in range(0, len(pmids), 40):
        batch = pmids[i : i + 40]
        try:
            r = http_get(f"{NCBI}/efetch.fcgi", params={"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
        except Exception:
            continue
        root = ET.fromstring(r.text)
        for art in root.findall(".//PubmedArticle"):
            pmid = (art.findtext(".//PMID") or "").strip()
            title = (art.findtext(".//ArticleTitle") or "").strip()
            journal = (art.findtext(".//Journal/Title") or "").strip()
            year = (art.findtext(".//PubDate/Year") or "").strip()
            doi = ""
            for a in art.findall(".//ArticleId"):
                if a.attrib.get("IdType") == "doi":
                    doi = (a.text or "").strip()
            abs_parts = []
            for x in art.findall(".//Abstract/AbstractText"):
                label = x.attrib.get("Label", "")
                abs_parts.append((f"{label}: " if label else "") + (x.text or "").strip())
            abstract = re.sub(r"\s+", " ", " ".join(abs_parts))
            rows.append(
                {
                    "pmid": pmid,
                    "year": year,
                    "journal": journal,
                    "title": title,
                    "doi": doi,
                    "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "doi_url": f"https://doi.org/{doi}" if doi else "",
                    "abstract_full_text": abstract,
                    "pk_related": bool(
                        re.search(
                            r"half[- ]life|t1/2|pharmacokinetic|clearance|biodistribution|dosimetry",
                            abstract + title,
                            re.I,
                        )
                    ),
                }
            )
        time.sleep(0.11)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["pk_related", "year"], ascending=[False, False])
    return df


def trials_from_cache(primary: str, ct_df: pd.DataFrame) -> pd.DataFrame:
    if ct_df is None or ct_df.empty:
        return pd.DataFrame()
    sub = ct_df[ct_df["drug_name"].astype(str).str.lower() == primary.lower()].copy()
    return sub


def nct_evidence_from_cache(primary: str, nct_df: pd.DataFrame) -> pd.DataFrame:
    if nct_df is None or nct_df.empty:
        return pd.DataFrame()
    return nct_df[nct_df["drug_name"].astype(str).str.lower() == primary.lower()].copy()


def supplement_trials_api(primary: str, aliases: list[str], existing: pd.DataFrame, max_extra: int = 5) -> pd.DataFrame:
    if len(existing) >= max_extra:
        return existing
    extra_rows = []
    seen = set(existing["nct_number"].astype(str).tolist()) if not existing.empty and "nct_number" in existing.columns else set()
    for q in [primary] + aliases[1:2]:
        try:
            r = http_get(CT_API, params={"query.term": q, "pageSize": 20, "format": "json"})
        except Exception:
            continue
        for s in r.json().get("studies", []):
            proto = s.get("protocolSection", {})
            ident = proto.get("identificationModule", {})
            nct = ident.get("nctId", "")
            if not nct or nct in seen:
                continue
            blob = str(ident) + str(proto.get("armsInterventionsModule", ""))
            if not mentions_any_alias(blob, aliases):
                continue
            seen.add(nct)
            design = proto.get("designModule", {})
            status = proto.get("statusModule", {})
            cond = proto.get("conditionsModule", {})
            arms = proto.get("armsInterventionsModule", {})
            desc = proto.get("descriptionModule", {})
            extra_rows.append(
                {
                    "nct_number": nct,
                    "clinicaltrials_url": f"https://clinicaltrials.gov/study/{nct}",
                    "official_title": ident.get("officialTitle", ident.get("briefTitle", "")),
                    "phase": ",".join(design.get("phases", []) or []) or "N/A",
                    "enrollment": (design.get("enrollmentInfo") or {}).get("count", ""),
                    "status": status.get("overallStatus", ""),
                    "conditions": "; ".join(cond.get("conditions", []) or []),
                    "interventions": "; ".join(
                        i.get("name", "") for i in (arms.get("interventions", []) or []) if isinstance(i, dict)
                    ),
                    "brief_summary": re.sub(r"\s+", " ", desc.get("briefSummary", "") or "")[:2000],
                    "source": "api_supplement",
                }
            )
            if len(extra_rows) >= max_extra:
                break
        time.sleep(0.2)
    if not extra_rows:
        return existing
    return pd.concat([existing, pd.DataFrame(extra_rows)], ignore_index=True)


def local_pdf_inventory(primary: str) -> pd.DataFrame:
    rows = []
    for folder, label in ((DOWNLOADS / primary, "downloads"), (CORTELLIS_DRUGS / primary, "cortellis")):
        if not folder.is_dir():
            continue
        for pdf in sorted(folder.glob("*.pdf")):
            rows.append({"library": label, "filename": pdf.name, "path": str(pdf), "pmid": pmid_from_pdf_name(pdf.name)})
    return pd.DataFrame(rows)


def build_summary_sheet(row: pd.Series, aliases: list[str], pdf_hits: list[dict]) -> pd.DataFrame:
    fields = [
        "Primary_name_clean",
        "Generic Name",
        "Code Name",
        "Brand Name",
        "Target",
        "Product Category",
        "Drug Type",
        "Highest Phase",
        "half_life_status",
        "half_life_value",
        "half_life_unit",
        "half_life_hours",
        "half_life_days",
        "half_life_source_file",
        "half_life_evidence_snippet",
        "half_life_source_type",
        "clinicaltrials_count",
        "downloaded_count",
    ]
    rows = [{"field": "aliases_used_for_validation", "value": "; ".join(aliases[:12])}]
    for f in fields:
        if f in row.index and pd.notna(row.get(f)):
            rows.append({"field": f, "value": row.get(f)})
    validated = [h for h in pdf_hits if h.get("drug_mention_validated")]
    rows.append({"field": "validated_pdf_half_life_count", "value": len(validated)})
    if validated:
        best = validated[0]
        rows.append(
            {
                "field": "recommended_pdf_half_life",
                "value": f"{best['half_life_value']} {best['half_life_unit']} ({best['half_life_hours']} h)",
            }
        )
    report_snip = str(row.get("half_life_evidence_snippet") or "")
    if str(row.get("half_life_status")) == "found" and report_snip and not mentions_any_alias(report_snip, aliases):
        rows.append(
            {
                "field": "WARNING_report_half_life",
                "value": "总表 PDF 半衰期证据未提及该药别名，可能为误匹配（见 half_life_evidence / pdf_excerpts）",
            }
        )
    return pd.DataFrame(rows)


def process_drug(
    row: pd.Series,
    ct_df: pd.DataFrame,
    nct_df: pd.DataFrame,
    *,
    skip_pubmed: bool = False,
    pubmed_retmax: int = 15,
) -> Path:
    primary = str(row["Primary_name_clean"]).strip()
    aliases = parse_aliases(row, primary)

    pdf_hits: list[dict] = []
    for folder in (DOWNLOADS / primary, CORTELLIS_DRUGS / primary):
        pdf_hits.extend(scan_validated_pdf_hits(folder, aliases))
    # dedupe by file+value
    seen = set()
    deduped = []
    for h in pdf_hits:
        key = (h["source_file"], h["half_life_value"], h["half_life_unit"])
        if key not in seen:
            seen.add(key)
            deduped.append(h)
    pdf_hits = deduped

    out_path = OUT_DIR / f"{safe_filename(primary)}.xlsx"
    trials = trials_from_cache(primary, ct_df)
    if trials.empty:
        trials = supplement_trials_api(primary, aliases, trials)
    nct_ev = nct_evidence_from_cache(primary, nct_df)
    pubmed_df = pd.DataFrame() if skip_pubmed else pubmed_for_drug(primary, aliases, retmax=pubmed_retmax)

    sheets = [
        ("drug_summary", build_summary_sheet(row, aliases, pdf_hits)),
        ("half_life_evidence", build_half_life_sheet(row, aliases, pdf_hits)),
        ("pdf_excerpts", build_pdf_excerpts(pdf_hits, row, aliases)),
        (
            "pubmed_full_abstracts",
            pubmed_df if not pubmed_df.empty else pd.DataFrame([{"note": "No PubMed hits"}]),
        ),
        (
            "clinical_trials",
            trials if not trials.empty else pd.DataFrame([{"note": "No ClinicalTrials.gov rows in cache/API"}]),
        ),
        (
            "nct_pubmed_evidence",
            nct_ev if not nct_ev.empty else pd.DataFrame([{"note": "No NCT-linked PubMed evidence"}]),
        ),
        ("local_pdf_inventory", local_pdf_inventory(primary)),
    ]
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        for sheet_name, sheet_df in sheets:
            sanitize_df(sheet_df).to_excel(w, sheet_name=sheet_name, index=False)

    return out_path


def load_drugs(include_biosimilars: bool) -> pd.DataFrame:
    frames = [pd.read_excel(REPORT_XLSX, sheet_name="summary_full")]
    if include_biosimilars:
        frames.append(pd.read_excel(REPORT_XLSX, sheet_name="biosimilars"))
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["Primary_name_clean"], keep="first")
    return df


def load_state() -> set[str]:
    if not STATE_JSON.exists():
        return set()
    try:
        return set(json.loads(STATE_JSON.read_text(encoding="utf-8")).get("done", []))
    except Exception:
        return set()


def save_state(done: set[str]) -> None:
    STATE_JSON.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate per-drug dossier Excel files")
    parser.add_argument("--include-biosimilars", action="store_true", help="Also process biosimilars sheet")
    parser.add_argument("--skip-pubmed", action="store_true", help="Skip PubMed API (faster replay)")
    parser.add_argument("--pubmed-retmax", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0, help="Max drugs to process (0=all)")
    parser.add_argument("--drug", type=str, default="", help="Process single drug only")
    parser.add_argument("--force", action="store_true", help="Reprocess even if in progress state")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_drugs(args.include_biosimilars)
    ct_df = pd.read_csv(CT_CSV) if CT_CSV.exists() else pd.DataFrame()
    nct_df = pd.read_csv(NCT_CSV) if NCT_CSV.exists() else pd.DataFrame()

    drugs = df["Primary_name_clean"].astype(str).str.strip().tolist()
    if args.drug:
        drugs = [d for d in drugs if d.lower() == args.drug.strip().lower()]
    if args.limit > 0:
        drugs = drugs[: args.limit]

    done = set() if args.force else load_state()
    total = len(drugs)
    errors: list[dict] = []

    for i, primary in enumerate(drugs, 1):
        if primary in done and not args.force:
            continue
        row = df[df["Primary_name_clean"].astype(str) == primary].iloc[0]
        print(f"[{i}/{total}] {primary}", flush=True)
        try:
            path = process_drug(
                row,
                ct_df,
                nct_df,
                skip_pubmed=args.skip_pubmed,
                pubmed_retmax=args.pubmed_retmax,
            )
            print(f"  -> {path.name}", flush=True)
            done.add(primary)
            save_state(done)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            errors.append({"drug": primary, "error": str(e)})

    if errors:
        pd.DataFrame(errors).to_csv(OUT_DIR / "_errors.csv", index=False)

    index_rows = []
    for xlsx in sorted(OUT_DIR.glob("*.xlsx")):
        if xlsx.name.startswith("_"):
            continue
        index_rows.append({"Primary_name_clean": xlsx.stem, "dossier_xlsx": str(xlsx)})
    if index_rows:
        pd.DataFrame(index_rows).to_csv(OUT_DIR / "dossier_index.csv", index=False)

    print(f"Done. {len(done)} dossiers in {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()

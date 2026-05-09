import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import time


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = DOWNLOAD_DIR / "download_index.json"

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PMC_OA_API = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
REQUEST_TIMEOUT = 25
PMC_AWS_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "pubmed-local-downloader/0.1 (mailto:local@example.com)",
    }
)

app = FastAPI(title="PubMed OA PDF Downloader")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BatchItem(BaseModel):
    term: str = Field(..., min_length=1)
    max_results: int = Field(10, ge=1, le=200)
    category: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    items: List[BatchItem] = Field(..., min_length=1, max_length=200)
    interval_seconds: float = Field(0.6, ge=0, le=10)


def safe_name(value: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return cleaned[:max_len] or "unknown"


def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def load_download_index() -> Dict[str, Any]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"records": []}


def save_download_index(index: Dict[str, Any]) -> None:
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def bootstrap_index_from_files(index: Dict[str, Any]) -> Dict[str, Any]:
    records = index.get("records", [])
    known_paths = {r.get("file_path") for r in records}
    for pdf in DOWNLOAD_DIR.glob("**/*.pdf"):
        rel = str(pdf.relative_to(BASE_DIR))
        if rel in known_paths:
            continue
        # Existing filename format: year_pmid_title.pdf; pmid may be empty.
        m = re.match(r"^\d{4}_([^_]+)_.*\.pdf$", pdf.name)
        pmid = m.group(1) if m else ""
        if pmid in {"", "unknown"}:
            pmid = ""
        records.append(
            {
                "pmcid": "",
                "pmid": pmid,
                "doi": "",
                "title": pdf.stem,
                "file_path": rel,
                "sha256": "",
            }
        )
    index["records"] = records
    return index


def find_existing_record(index: Dict[str, Any], paper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pmcid = normalize(paper.get("pmcid"))
    pmid = normalize(paper.get("pmid"))
    doi = normalize(paper.get("doi"))
    for rec in index.get("records", []):
        if pmcid and normalize(rec.get("pmcid")) == pmcid:
            return rec
        if pmid and normalize(rec.get("pmid")) == pmid:
            return rec
        if doi and normalize(rec.get("doi")) == doi:
            return rec
    return None


def upsert_record(index: Dict[str, Any], paper: Dict[str, Any], file_path: str, sha256: str) -> None:
    existing = find_existing_record(index, paper)
    new_record = {
        "pmcid": paper.get("pmcid") or "",
        "pmid": paper.get("pmid") or "",
        "doi": paper.get("doi") or "",
        "title": paper.get("title") or "",
        "file_path": file_path,
        "sha256": sha256,
    }
    if existing:
        existing.update(new_record)
    else:
        index.setdefault("records", []).append(new_record)


def request_xml(url: str, params: Dict[str, Any]) -> ET.Element:
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return ET.fromstring(resp.text)


def esearch_pubmed(term: str, max_results: int) -> List[str]:
    root = request_xml(
        f"{NCBI_BASE}/esearch.fcgi",
        {
            "db": "pubmed",
            "term": term,
            "retmax": max_results,
            "retmode": "xml",
            "sort": "relevance",
        },
    )
    return [elem.text for elem in root.findall(".//IdList/Id") if elem.text]


def esearch_pmc(term: str, max_results: int) -> List[str]:
    query = f"{term} AND (open_access[filter] OR author_manuscript[filter]) AND has_pdf[filter]"
    root = request_xml(
        f"{NCBI_BASE}/esearch.fcgi",
        {
            "db": "pmc",
            "term": query,
            "retmax": max_results,
            "retmode": "xml",
            "sort": "relevance",
        },
    )
    return [elem.text for elem in root.findall(".//IdList/Id") if elem.text]


def fetch_pubmed_metadata(pmids: List[str]) -> List[Dict[str, Any]]:
    if not pmids:
        return []

    root = request_xml(
        f"{NCBI_BASE}/efetch.fcgi",
        {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        },
    )
    papers: List[Dict[str, Any]] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID")
        title = article.findtext(".//ArticleTitle") or "untitled"
        journal = article.findtext(".//Journal/Title") or "unknown_journal"
        year = (
            article.findtext(".//PubDate/Year")
            or article.findtext(".//ArticleDate/Year")
            or "unknown_year"
        )

        pmcid: Optional[str] = None
        doi: Optional[str] = None
        for aid in article.findall(".//ArticleIdList/ArticleId"):
            id_type = aid.attrib.get("IdType", "")
            if id_type == "pmc" and aid.text:
                pmcid = aid.text if aid.text.startswith("PMC") else f"PMC{aid.text}"
            if id_type == "doi" and aid.text:
                doi = aid.text

        if pmid:
            papers.append(
                {
                    "pmid": pmid,
                    "title": title.strip(),
                    "journal": journal.strip(),
                    "year": year.strip(),
                    "pmcid": pmcid,
                    "doi": doi,
                }
            )
    return papers


def fetch_pmc_metadata(pmc_ids: List[str]) -> List[Dict[str, Any]]:
    if not pmc_ids:
        return []
    root = request_xml(
        f"{NCBI_BASE}/efetch.fcgi",
        {
            "db": "pmc",
            "id": ",".join(pmc_ids),
            "retmode": "xml",
        },
    )
    papers: List[Dict[str, Any]] = []
    for article in root.findall(".//article"):
        pmcid = article.findtext(".//article-id[@pub-id-type='pmcid']")
        if not pmcid:
            continue
        pmcid = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
        pmid = article.findtext(".//article-id[@pub-id-type='pmid']")
        doi = article.findtext(".//article-id[@pub-id-type='doi']")
        title = "".join(article.findtext(".//article-title") or "untitled").strip()
        journal = (article.findtext(".//journal-title") or "unknown_journal").strip()
        year = (
            article.findtext(".//pub-date/year")
            or article.findtext(".//history/date/year")
            or "unknown_year"
        )
        papers.append(
            {
                "pmid": pmid or "",
                "title": title,
                "journal": journal,
                "year": str(year).strip(),
                "pmcid": pmcid,
                "doi": doi,
            }
        )
    return papers


def fetch_pmcids_for_pmids(pmids: List[str]) -> Dict[str, str]:
    if not pmids:
        return {}
    root = request_xml(
        f"{NCBI_BASE}/elink.fcgi",
        {
            "dbfrom": "pubmed",
            "db": "pmc",
            "id": ",".join(pmids),
            "retmode": "xml",
        },
    )
    mapping: Dict[str, str] = {}
    for linkset in root.findall(".//LinkSet"):
        pmid = linkset.findtext("./IdList/Id")
        if not pmid:
            continue
        pmc_id = linkset.findtext(".//LinkSetDb/Link/Id")
        if pmc_id:
            mapping[pmid] = f"PMC{pmc_id}"
    return mapping


def resolve_oa_pdf_link(pmcid: str) -> Optional[str]:
    root = request_xml(PMC_OA_API, {"id": pmcid})
    # OA API may return <record> with nested <link format="pdf" href="...">
    for link in root.findall(".//record/link"):
        if link.attrib.get("format") == "pdf":
            href = link.attrib.get("href")
            if href:
                if href.startswith("ftp://"):
                    return "https://" + href.replace("ftp://", "", 1)
                return href
    return None


def resolve_pdf_link_aws(pmcid: str) -> Optional[str]:
    # Most records have version 1; probe a few versions to be safe.
    for version in (1, 2, 3):
        base = f"{pmcid}.{version}"
        url = f"{PMC_AWS_BASE}/{base}/{base}.pdf"
        try:
            resp = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                return url
        except requests.RequestException:
            continue
    return None


def pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_pdf(url: str, target_path: Path) -> bool:
    resp = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    if resp.status_code != 200:
        return False
    content_type = (resp.headers.get("content-type") or "").lower()
    if "pdf" not in content_type and not url.lower().endswith(".pdf"):
        # Still allow download if magic bytes match.
        pass

    with target_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    with target_path.open("rb") as f:
        header = f.read(5)
    return header == b"%PDF-"


@app.get("/api/search")
def api_search(term: str = "asthma", max_results: int = 20) -> Dict[str, Any]:
    if max_results < 1 or max_results > 200:
        raise HTTPException(status_code=400, detail="max_results must be 1~200")
    pmids = esearch_pubmed(term=term, max_results=max_results)
    papers = fetch_pubmed_metadata(pmids)
    return {"count": len(papers), "items": papers}


@app.post("/api/download")
def api_download(term: str = "asthma", max_results: int = 20, category: str = "asthma") -> Dict[str, Any]:
    if max_results < 1 or max_results > 200:
        raise HTTPException(status_code=400, detail="max_results must be 1~200")

    category_safe = safe_name(category)
    category_dir = DOWNLOAD_DIR / category_safe
    category_dir.mkdir(parents=True, exist_ok=True)

    # Use PMC as the download source to ensure OA PDF resolution is feasible.
    # We still preserve pmid when available in returned metadata.
    pmc_numeric_ids = esearch_pmc(term=term, max_results=max_results * 3)
    papers = fetch_pmc_metadata(pmc_numeric_ids)[: max_results * 3]

    downloaded: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    deduped: List[Dict[str, Any]] = []
    index = bootstrap_index_from_files(load_download_index())

    for p in papers:
        if len(downloaded) >= max_results:
            break
        pmcid = p.get("pmcid")
        if not pmcid:
            skipped.append({**p, "reason": "No PMCID (not OA in PMC)"})
            continue

        existing = find_existing_record(index, p)
        if existing:
            deduped.append({**p, "reason": "Already downloaded", "existing_file_path": existing.get("file_path", "")})
            continue

        try:
            pdf_url = resolve_pdf_link_aws(pmcid) or resolve_oa_pdf_link(pmcid)
            if not pdf_url:
                skipped.append({**p, "reason": "No OA PDF link found in AWS/OA API"})
                continue

            filename = f"{p['year']}_{safe_name(p['pmid'])}_{safe_name(p['title'], 60)}.pdf"
            target_path = category_dir / filename

            ok = download_pdf(pdf_url, target_path)
            if not ok:
                if target_path.exists():
                    target_path.unlink()
                failed.append({**p, "reason": "Downloaded file is not a valid PDF", "pdf_url": pdf_url})
                continue

            downloaded.append(
                {
                    **p,
                    "pdf_url": pdf_url,
                    "file_path": str(target_path.relative_to(BASE_DIR)),
                    "sha256": pdf_sha256(target_path),
                }
            )
            upsert_record(index, p, str(target_path.relative_to(BASE_DIR)), downloaded[-1]["sha256"])
        except requests.RequestException as e:
            failed.append({**p, "reason": f"Request error: {str(e)}"})
        except ET.ParseError as e:
            failed.append({**p, "reason": f"XML parse error: {str(e)}"})
        except Exception as e:
            failed.append({**p, "reason": f"Unknown error: {str(e)}"})

    save_download_index(index)
    return {
        "query": term,
        "category": category_safe,
        "total_candidates": len(papers),
        "downloaded_count": len(downloaded),
        "deduped_count": len(deduped),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "downloaded": downloaded,
        "deduped": deduped,
        "skipped": skipped,
        "failed": failed,
    }


@app.post("/api/batch_download")
def api_batch_download(payload: BatchDownloadRequest) -> Dict[str, Any]:
    task_results: List[Dict[str, Any]] = []
    total_downloaded = 0
    total_deduped = 0
    total_skipped = 0
    total_failed = 0

    for idx, item in enumerate(payload.items):
        category = item.category.strip() if item.category else item.term
        result = api_download(term=item.term, max_results=item.max_results, category=category)
        task_results.append(
            {
                "task_index": idx,
                "term": item.term,
                "max_results": item.max_results,
                "category": category,
                "result": result,
            }
        )
        total_downloaded += result["downloaded_count"]
        total_deduped += result.get("deduped_count", 0)
        total_skipped += result["skipped_count"]
        total_failed += result["failed_count"]

        # Slow down by design to reduce pressure on upstream services.
        if idx < len(payload.items) - 1 and payload.interval_seconds > 0:
            time.sleep(payload.interval_seconds)

    return {
        "task_count": len(payload.items),
        "interval_seconds": payload.interval_seconds,
        "total_downloaded": total_downloaded,
        "total_deduped": total_deduped,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "tasks": task_results,
    }


@app.get("/api/files")
def api_files() -> Dict[str, Any]:
    items: List[Dict[str, str]] = []
    for pdf in DOWNLOAD_DIR.glob("**/*.pdf"):
        items.append(
            {
                "relative_path": str(pdf.relative_to(BASE_DIR)),
                "category": str(pdf.relative_to(DOWNLOAD_DIR).parent),
                "filename": pdf.name,
            }
        )
    items.sort(key=lambda x: x["relative_path"])
    return {"count": len(items), "items": items}


@app.get("/files/{file_path:path}")
def get_file(file_path: str):
    full_path = BASE_DIR / file_path
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full_path)


frontend_dir = BASE_DIR / "frontend"
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)

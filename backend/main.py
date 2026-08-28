from __future__ import annotations

import os
import re
import html
import secrets
import io
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, HttpUrl
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


app = FastAPI(title="SiteScore API", version="0.1.0")
raw_database_url = os.getenv("DATABASE_URL", "").strip().strip('"').strip("'") or None
database_url = raw_database_url
if database_url and database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
if database_url:
    parsed_database_url = make_url(database_url)
    if ".pooler.supabase.com" in (parsed_database_url.host or ""):
        supabase_host = urlparse(os.getenv("SUPABASE_URL", "")).hostname or ""
        project_ref = supabase_host.split(".")[0]
        if project_ref:
            parsed_database_url = parsed_database_url.set(username=f"postgres.{project_ref}")
        elif parsed_database_url.username == "postgres":
            raise RuntimeError("SUPABASE_URL must be set when DATABASE_URL uses a Supabase pooler hostname")
    if "sslmode" not in parsed_database_url.query:
        parsed_database_url = parsed_database_url.update_query_dict({"sslmode": "require"})
    database_url = str(parsed_database_url)
logger = logging.getLogger("uvicorn.error")
engine_options = {"pool_pre_ping": True}
if database_url and ".pooler.supabase.com" in database_url:
    # Supabase transaction pooler connections must not be held by SQLAlchemy.
    engine_options.update({"poolclass": NullPool, "connect_args": {"prepare_threshold": 0}})
engine = create_engine(database_url, **engine_options) if database_url else None
if database_url:
    safe_database_url = make_url(database_url).set(password="***")
    logger.info("Database configured: %s", safe_database_url.render_as_string(hide_password=False))

allowed_origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_URL", "https://site-score-sable.vercel.app").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class AuditRequest(BaseModel):
    url: HttpUrl


class AuditResponse(BaseModel):
    url: str
    final_url: str
    status_code: int
    title: str | None
    meta_description: str | None
    h1_tags: list[str]
    robots_txt: dict[str, Any]
    sitemap_xml: dict[str, Any]
    viewport_meta: dict[str, Any]
    is_https: bool
    images_missing_alt: int
    images_total: int
    json_ld: list[Any]
    page_weight_bytes: int
    has_sitewide_noindex: bool
    mixed_content_count: int
    scores: dict[str, int]
    overall_score: int
    grade: str
    issues: list[dict[str, Any]]
    share_id: str | None = None
    share_url: str | None = None
    og_image_url: str | None = None


class LeadCaptureRequest(BaseModel):
    email: EmailStr


class LeadCaptureResponse(BaseModel):
    success: bool
    message: str


def public_api_url() -> str:
    return os.getenv("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")


def frontend_url() -> str:
    return os.getenv("FRONTEND_URL", "http://localhost:5173").split(",")[0].strip().rstrip("/")


def persist_report(report: dict[str, Any]) -> str:
    if engine is None:
        raise HTTPException(status_code=503, detail="Report storage is not configured on the API.")
    from json import dumps

    for _ in range(5):
        short_id = secrets.token_urlsafe(7).replace("-", "_")[:10]
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("""
                        insert into public.audit_reports
                          (short_id, url, final_url, domain, overall_score, grade, report)
                        values (:short_id, :url, :final_url, :domain, :overall_score, :grade, cast(:report as jsonb))
                    """),
                    {
                        "short_id": short_id,
                        "url": report["url"],
                        "final_url": report["final_url"],
                        "domain": urlparse(report["final_url"]).netloc,
                        "overall_score": report["overall_score"],
                        "grade": report["grade"],
                        "report": dumps(report),
                    },
                )
            return short_id
        except Exception as exc:
            if "duplicate key" not in str(exc).lower():
                logger.exception("Could not persist audit report")
                raise HTTPException(status_code=503, detail="The audit was completed but could not be saved.") from exc
    raise HTTPException(status_code=503, detail="Could not create a unique share link. Please try again.")


def load_report(short_id: str) -> dict[str, Any]:
    if engine is None:
        raise HTTPException(status_code=503, detail="Report storage is not configured on the API.")
    with engine.connect() as connection:
        row = connection.execute(text("select report from public.audit_reports where short_id = :short_id"), {"short_id": short_id}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="That shared report does not exist.")
    return row["report"]


def capture_lead(short_id: str, email: str) -> None:
    if engine is None:
        raise HTTPException(status_code=503, detail="Report storage is not configured on the API.")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    insert into public.lead_captures (report_short_id, email)
                    values (:short_id, :email)
                    on conflict (report_short_id, email) do nothing
                """),
                {"short_id": short_id, "email": email.lower()},
            )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Your email could not be saved. Please try again.") from exc


def lead_has_access(short_id: str, email: str) -> bool:
    if engine is None:
        return False
    with engine.connect() as connection:
        return connection.execute(
            text("select 1 from public.lead_captures where report_short_id = :short_id and email = :email"),
            {"short_id": short_id, "email": email.lower()},
        ).first() is not None


def build_pdf(report: dict[str, Any]) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="PDF generation is not available on the API.") from exc

    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.7 * inch, leftMargin=0.7 * inch, topMargin=0.65 * inch, bottomMargin=0.65 * inch)
    styles = getSampleStyleSheet()
    story = [Paragraph("SiteScore report", styles["Title"]), Paragraph(f"{report['final_url']} · Grade {report['grade']} · {report['overall_score']}/100", styles["Heading2"]), Spacer(1, 0.2 * inch)]
    story.append(Paragraph("Priority fixes", styles["Heading2"]))
    for index, issue in enumerate(report.get("issues", []), 1):
        story.extend([Paragraph(f"{index}. {issue['title']} ({issue['category']})", styles["Heading3"]), Paragraph(issue["explanation"], styles["BodyText"])])
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Raw crawl signals", styles["Heading2"]))
    for label, value in (("Title", report.get("title") or "Not found"), ("Meta description", report.get("meta_description") or "Not found"), ("H1 count", str(len(report.get("h1_tags", [])))), ("Page weight", f"{report.get('page_weight_bytes', 0):,} bytes"), ("Images missing alt", str(report.get("images_missing_alt", 0)))):
        story.append(Paragraph(f"<b>{html.escape(label)}:</b> {html.escape(value)}", styles["BodyText"]))
    document.build(story)
    return buffer.getvalue()


def report_urls(short_id: str) -> dict[str, str]:
    base = public_api_url()
    return {"share_id": short_id, "share_url": f"{base}/r/{short_id}", "og_image_url": f"{base}/api/reports/{short_id}/og.svg"}


@dataclass
class ScoreIssue:
    category: str
    lost_points: int
    title: str
    explanation: str


def score_audit(
    *,
    title: str | None,
    meta_description: str | None,
    h1_tags: list[str],
    robots_exists: bool,
    robots_blocks_all: bool,
    sitemap_exists: bool,
    has_sitewide_noindex: bool,
    json_ld: list[Any],
    page_weight_bytes: int,
    images_missing_alt: int,
    images_total: int,
    is_https: bool,
    viewport_exists: bool,
    mixed_content_count: int,
) -> tuple[dict[str, int], int, str, list[dict[str, Any]]]:
    issues: list[ScoreIssue] = []

    indexability_checks = [robots_exists and not robots_blocks_all, sitemap_exists, not has_sitewide_noindex]
    indexability = round(sum(indexability_checks) / len(indexability_checks) * 100)
    if not indexability_checks[0]:
        issues.append(ScoreIssue("Indexability", 20, "Review crawler access", "Make sure robots.txt exists and does not disallow all crawlers so search engines can access your site."))
    if not indexability_checks[1]:
        issues.append(ScoreIssue("Indexability", 20, "Add an XML sitemap", "Publish sitemap.xml and reference your important pages so search engines can discover them efficiently."))
    if not indexability_checks[2]:
        issues.append(ScoreIssue("Indexability", 20, "Remove sitewide noindex", "Remove the blanket noindex directive unless you intentionally want the whole site kept out of search results."))

    on_page_checks = [title is not None and 30 <= len(title) <= 60, meta_description is not None and 120 <= len(meta_description) <= 160, len(h1_tags) == 1]
    on_page = round(sum(on_page_checks) / len(on_page_checks) * 100)
    if not on_page_checks[0]:
        issues.append(ScoreIssue("On-page", 20, "Tune the title tag", "Add a descriptive title between 30 and 60 characters so searchers and crawlers understand the page quickly."))
    if not on_page_checks[1]:
        issues.append(ScoreIssue("On-page", 20, "Write a meta description", "Add a useful meta description between 120 and 160 characters to give the search result a clear preview."))
    if not on_page_checks[2]:
        issues.append(ScoreIssue("On-page", 20, "Use one clear H1", "Keep exactly one H1 that describes the page's primary topic and use lower-level headings for sections."))

    valid_json_ld = bool(json_ld) and all(not (isinstance(item, dict) and "_invalid" in item) for item in json_ld)
    structured_data = 100 if valid_json_ld else 0
    if not valid_json_ld:
        issues.append(ScoreIssue("Structured data", 20, "Add valid JSON-LD", "Add valid schema.org JSON-LD that describes the page so eligible search features can understand its content."))

    image_ratio = images_missing_alt / images_total if images_total else 0
    performance_checks = [page_weight_bytes < 2 * 1024 * 1024, image_ratio < 0.2]
    performance = round(sum(performance_checks) / len(performance_checks) * 100)
    if not performance_checks[0]:
        issues.append(ScoreIssue("Performance signals", 10, "Reduce page weight", "Keep the initial HTML response under 2 MB by compressing content and removing unnecessary payload."))
    if not performance_checks[1]:
        issues.append(ScoreIssue("Performance signals", 10, "Fill in image alt text", "Add concise alt text to images so their meaning is available to screen readers and crawlers."))

    security_checks = [is_https, viewport_exists, mixed_content_count == 0]
    security = round(sum(security_checks) / len(security_checks) * 100)
    if not security_checks[0]:
        issues.append(ScoreIssue("Security/mobile", 20, "Enable HTTPS", "Serve the site over HTTPS to protect visitors and preserve trust in the browser and search results."))
    if not security_checks[1]:
        issues.append(ScoreIssue("Security/mobile", 20, "Add a viewport meta tag", "Add a responsive viewport declaration so the page can size correctly on phones and tablets."))
    if not security_checks[2]:
        issues.append(ScoreIssue("Security/mobile", 20, "Fix mixed content", "Update HTTP assets to HTTPS so browsers do not block insecure resources on the secure page."))

    scores = {"indexability": indexability, "on_page": on_page, "structured_data": structured_data, "performance_signals": performance, "security_mobile": security}
    overall_score = round(sum(scores.values()) / len(scores))
    grade = "A" if overall_score >= 90 else "B" if overall_score >= 80 else "C" if overall_score >= 70 else "D" if overall_score >= 60 else "F"
    issues.sort(key=lambda issue: issue.lost_points, reverse=True)
    return scores, overall_score, grade, [issue.__dict__ for issue in issues[:5]]


def robots_blocks_all(content: str | None) -> bool:
    if not content:
        return False
    applies_to_all = False
    for line in content.splitlines():
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            applies_to_all = value == "*"
        elif directive == "disallow" and applies_to_all and value == "/":
            return True
    return False


async def fetch_optional(client: httpx.AsyncClient, url: str) -> tuple[bool, str | None, int | None]:
    try:
        response = await client.get(url)
        return response.is_success, response.text, response.status_code
    except httpx.HTTPError:
        return False, None, None


def extract_json_ld(soup: BeautifulSoup) -> list[Any]:
    values: list[Any] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            import json

            values.append(json.loads(raw))
        except json.JSONDecodeError:
            values.append({"_invalid": raw.strip()})
    return values


def meta_value(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.IGNORECASE)})
    return tag.get("content", "").strip() if tag else None


@app.get("/api/health")
async def health() -> dict[str, str]:
    if engine is None:
        return {"status": "ok", "database": "not_configured"}
    database = make_url(database_url) if database_url else None
    raw_database = make_url(raw_database_url) if raw_database_url else None
    connection_info = {
        "database_host": database.host or "unknown",
        "database_port": str(database.port or "default"),
        "database_user": database.username or "unknown",
        "raw_database_user": raw_database.username if raw_database else "unknown",
    }
    try:
        import psycopg

        direct_url = database.set(drivername="postgresql").render_as_string(hide_password=False)
        with psycopg.connect(direct_url, connect_timeout=8) as direct_connection:
            direct_connection.execute("select 1")
        connection_info["psycopg"] = "connected"
    except ImportError:
        logger.exception("psycopg is not installed")
        connection_info["psycopg"] = "not_installed"
        connection_info["psycopg_error"] = "The psycopg package is not installed."
    except Exception as exc:
        logger.exception("Direct psycopg database health check failed")
        connection_info["psycopg"] = "unavailable"
        connection_info["psycopg_error"] = str(exc).split("\n", 1)[0][:240]
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        connection_info["sqlalchemy"] = "connected"
        if connection_info["psycopg"] != "connected":
            return {"status": "degraded", "database": "unavailable", **connection_info}
        return {"status": "ok", "database": "connected", **connection_info}
    except Exception:
        logger.exception("Database health check failed")
        return {"status": "degraded", "database": "unavailable", "sqlalchemy": "unavailable", **connection_info}


@app.get("/api/reports/{short_id}", response_model=AuditResponse)
async def get_report(short_id: str) -> AuditResponse:
    report = load_report(short_id)
    return AuditResponse(**{**report, **report_urls(short_id)})


@app.get("/api/reports/{short_id}/og.svg")
async def report_og_image(short_id: str) -> Response:
    report = load_report(short_id)
    domain = html.escape(urlparse(report["final_url"]).netloc)
    grade = html.escape(report["grade"])
    score = report["overall_score"]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
            <rect width="1200" height="630" fill="#07162e"/>
            <circle cx="1010" cy="-40" r="360" fill="#0b2347"/>
            <text x="76" y="100" fill="#77e1b5" font-family="Arial,sans-serif" font-size="28" font-weight="700">SiteScore</text>
            <text x="76" y="200" fill="#9db1cd" font-family="Arial,sans-serif" font-size="24">SEO READ FOR</text>
            <text x="76" y="260" fill="#f8fbff" font-family="Arial,sans-serif" font-size="44" font-weight="700">{domain}</text>
            <text x="76" y="480" fill="#77e1b5" font-family="Arial,sans-serif" font-size="170" font-weight="700">{grade}</text>
            <text x="310" y="455" fill="#f8fbff" font-family="Arial,sans-serif" font-size="88" font-weight="700">{score}</text>
            <text x="315" y="500" fill="#9db1cd" font-family="Arial,sans-serif" font-size="22">/ 100 overall score</text>
            <rect x="76" y="550" width="1048" height="2" fill="#24436d"/>
            <text x="76" y="590" fill="#9db1cd" font-family="Arial,sans-serif" font-size="18">Technical signals, made legible.</text>
    </svg>'''
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/reports/{short_id}/lead", response_model=LeadCaptureResponse)
async def create_lead(short_id: str, request: LeadCaptureRequest) -> LeadCaptureResponse:
    load_report(short_id)
    capture_lead(short_id, str(request.email))
    return LeadCaptureResponse(success=True, message="Your detailed fixes are unlocked.")


@app.post("/api/reports/{short_id}/pdf")
async def download_pdf(short_id: str, request: LeadCaptureRequest) -> Response:
    report = load_report(short_id)
    if not lead_has_access(short_id, str(request.email)):
        raise HTTPException(status_code=403, detail="Submit your email before downloading the report.")
    return Response(content=build_pdf(report), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="sitescore-{short_id}.pdf"'})


@app.get("/r/{short_id}", response_class=Response)
async def shared_report(short_id: str) -> Response:
    report = load_report(short_id)
    urls = report_urls(short_id)
    title = html.escape(f"{report['grade']} grade for {urlparse(report['final_url']).netloc} | SiteScore")
    description = html.escape(f"SiteScore found an overall SEO score of {report['overall_score']}/100 for {urlparse(report['final_url']).netloc}.")
    destination = f"{frontend_url()}/r/{short_id}"
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
            <meta name="description" content="{description}"><meta property="og:title" content="{title}">
            <meta property="og:description" content="{description}"><meta property="og:type" content="website">
            <meta property="og:image" content="{urls['og_image_url']}"><meta property="og:url" content="{urls['share_url']}">
            <meta http-equiv="refresh" content="0;url={destination}"></head>
            <body style="font-family:Arial,sans-serif;background:#07162e;color:white;padding:40px">Opening your SiteScore report...</body></html>'''
    return Response(content=page, media_type="text/html")


@app.post("/api/audit", response_model=AuditResponse)
async def audit_site(request: AuditRequest) -> AuditResponse:
    target_url = str(request.url)
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Enter a complete URL starting with http:// or https://.")

    headers = {"User-Agent": "SiteScoreBot/0.1 (+https://sitescore.app)"}
    timeout = httpx.Timeout(15.0, connect=8.0)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            response = await client.get(target_url)
            response.raise_for_status()
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            final_parsed = urlparse(str(response.url))
            origin = f"{final_parsed.scheme}://{final_parsed.netloc}"
            robots_ok, robots_body, robots_status = await fetch_optional(client, urljoin(origin + "/", "robots.txt"))
            sitemap_ok, _, sitemap_status = await fetch_optional(client, urljoin(origin + "/", "sitemap.xml"))
    except httpx.InvalidURL as exc:
        raise HTTPException(status_code=422, detail="That URL is not valid. Check it and try again.") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"The site returned HTTP {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=504, detail="The site could not be reached within 15 seconds.") from exc

    images = soup.find_all("img")
    missing_alt = sum(1 for image in images if not image.get("alt", "").strip())
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.IGNORECASE)})
    final_url = str(response.url)
    title = soup.title.get_text(strip=True) if soup.title else None
    meta_description = meta_value(soup, "description")
    h1_tags = [heading.get_text(" ", strip=True) for heading in soup.find_all("h1")]
    json_ld = extract_json_ld(soup)
    has_sitewide_noindex = any(
        directive in {"noindex", "none"}
        for tag in soup.find_all("meta", attrs={"name": re.compile("^robots$", re.IGNORECASE)})
        for directive in re.split(r"[,\s]+", tag.get("content", "").lower())
    )
    mixed_content_count = sum(
        1
        for tag in soup.find_all(src=True)
        if str(tag.get("src", "")).lower().startswith("http://")
    ) + sum(
        1
        for tag in soup.find_all(href=True)
        if str(tag.get("href", "")).lower().startswith("http://")
    )
    scores, overall_score, grade, issues = score_audit(
        title=title,
        meta_description=meta_description,
        h1_tags=h1_tags,
        robots_exists=robots_ok,
        robots_blocks_all=robots_blocks_all(robots_body),
        sitemap_exists=sitemap_ok,
        has_sitewide_noindex=has_sitewide_noindex,
        json_ld=json_ld,
        page_weight_bytes=len(response.content),
        images_missing_alt=missing_alt,
        images_total=len(images),
        is_https=urlparse(final_url).scheme.lower() == "https",
        viewport_exists=viewport is not None,
        mixed_content_count=mixed_content_count,
    )

    report = AuditResponse(
        url=target_url,
        final_url=final_url,
        status_code=response.status_code,
        title=title,
        meta_description=meta_description,
        h1_tags=h1_tags,
        robots_txt={"exists": robots_ok, "status_code": robots_status, "content": robots_body},
        sitemap_xml={"exists": sitemap_ok, "status_code": sitemap_status},
        viewport_meta={"exists": viewport is not None, "content": viewport.get("content") if viewport else None},
        is_https=urlparse(final_url).scheme.lower() == "https",
        images_missing_alt=missing_alt,
        images_total=len(images),
        json_ld=json_ld,
        page_weight_bytes=len(response.content),
        has_sitewide_noindex=has_sitewide_noindex,
        mixed_content_count=mixed_content_count,
        scores=scores,
        overall_score=overall_score,
        grade=grade,
        issues=issues,
    )
    share_id = persist_report(report.model_dump())
    return AuditResponse(**{**report.model_dump(), **report_urls(share_id)})

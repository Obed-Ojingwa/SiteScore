from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl


app = FastAPI(title="SiteScore API", version="0.1.0")

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
    return {"status": "ok"}


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
            origin = f"{parsed.scheme}://{parsed.netloc}"
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

    return AuditResponse(
        url=target_url,
        final_url=final_url,
        status_code=response.status_code,
        title=soup.title.get_text(strip=True) if soup.title else None,
        meta_description=meta_value(soup, "description"),
        h1_tags=[heading.get_text(" ", strip=True) for heading in soup.find_all("h1")],
        robots_txt={"exists": robots_ok, "status_code": robots_status, "content": robots_body},
        sitemap_xml={"exists": sitemap_ok, "status_code": sitemap_status},
        viewport_meta={"exists": viewport is not None, "content": viewport.get("content") if viewport else None},
        is_https=urlparse(final_url).scheme.lower() == "https",
        images_missing_alt=missing_alt,
        images_total=len(images),
        json_ld=extract_json_ld(soup),
        page_weight_bytes=len(response.content),
    )

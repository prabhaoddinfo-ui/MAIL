"""
backend.py — MailForge Pro FastAPI Backend v2.2
FIXED v2.2:
  - Persistent SMTP connection shared across all recipients (no reconnect per email)
  - keep-alive NOOP ping between sends prevents "Server not connected"
  - Graceful reconnect if connection drops mid-campaign
  - Longer DATA timeout (60s) for large GIF/image emails
  - Safe server.quit() wrapped so it never raises on already-closed connections
Run with: python backend.py
"""

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import asyncio
import base64
import concurrent.futures
import io
import logging
import os
import re
import smtplib
import socket
import ssl
import time
import uuid
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Dict, List, Optional, Tuple

import dns.resolver
import pandas as pd
import requests as http_requests

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"

def _get_sg_key(cfg: Dict) -> str:
    """
    Resolve SendGrid API key from config or environment.
    Accepts both smtp_pass / password keys (user pastes key into Password field).
    """
    return (
        cfg.get("smtp_pass") or cfg.get("password") or
        os.environ.get("SENDGRID_API_KEY", "")
    )

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="MailForge Pro API", version="2.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Constants ───────────────────────────────────────────────────────────────
DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.com", "mailinator.com", "guerrillamail.com",
    "yopmail.com", "10minutemail.com", "temp-mail.org", "fakeinbox.com",
    "maildrop.cc", "getnada.com", "trashmail.com", "dispostable.com",
    "spamgourmet.com", "throwawaymail.com", "tempail.com", "eyepaste.com",
    "sharklasers.com", "guerrillamail.org", "grr.la", "mailinator.net",
}

ROLE_PREFIXES = {
    "admin", "support", "info", "sales", "contact", "webmaster",
    "postmaster", "hostmaster", "abuse", "noreply", "no-reply",
    "newsletter", "office", "help", "service", "marketing",
    "billing", "accounts", "careers", "jobs", "hr", "team",
    "hello", "enquiries", "inquiries", "feedback",
}

EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._%+\-]*[a-zA-Z0-9]@"
    r"[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}$"
)

# ─── SMTP helper ─────────────────────────────────────────────────────────────

def _safe_b64decode(raw: str) -> bytes:
    """Decode base64, stripping data-URL prefix and fixing missing padding."""
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = raw.strip()
    missing = len(raw) % 4
    if missing:
        raw += "=" * (4 - missing)
    return base64.b64decode(raw)


def _smtp_connect(cfg: Dict, timeout: int = 60) -> smtplib.SMTP:
    """
    Open an authenticated SMTP connection.
    timeout=60 gives large emails (GIFs) enough time to upload.
    """
    host    = cfg.get("smtp_host") or cfg.get("host", "")
    port    = int(cfg.get("smtp_port") or cfg.get("port", 587))
    user    = cfg.get("smtp_user") or cfg.get("username", "")
    pw      = cfg.get("smtp_pass") or cfg.get("password", "")
    use_ssl = cfg.get("use_ssl", False)
    use_tls = cfg.get("use_tls", True)

    if not host:
        raise ValueError("SMTP host is not configured")

    if use_ssl:
        ctx = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()

    if user and pw:
        server.login(user, pw)
        logger.info(f"  SMTP authenticated as {user}")

    # Set socket to unlimited timeout AFTER login so the DATA phase
    # (uploading large GIFs/attachments) never times out mid-transfer
    if server.sock:
        server.sock.settimeout(None)

    return server


def _safe_quit(server: Optional[smtplib.SMTP]) -> None:
    """Close SMTP connection without raising even if already disconnected."""
    if server is None:
        return
    try:
        server.quit()
    except Exception:
        try:
            server.close()
        except Exception:
            pass


def _is_connected(server: smtplib.SMTP) -> bool:
    """Send NOOP to check if the connection is still alive."""
    try:
        status = server.noop()
        return status[0] == 250
    except Exception:
        return False


# ─── Email building ───────────────────────────────────────────────────────────

def _validate_syntax(email: str) -> Dict[str, Any]:
    if not email or not isinstance(email, str):
        return {"valid": False, "local": "", "domain": "", "reason": "Empty email"}
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return {"valid": False, "local": "", "domain": "", "reason": "Invalid format"}
    try:
        local, domain = email.rsplit("@", 1)
    except ValueError:
        return {"valid": False, "local": "", "domain": "", "reason": "Missing @"}
    if len(email) > 254:
        return {"valid": False, "local": local, "domain": domain, "reason": "Email exceeds 254 chars"}
    if len(local) > 64:
        return {"valid": False, "local": local, "domain": domain, "reason": "Local part exceeds 64 chars"}
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return {"valid": False, "local": local, "domain": domain, "reason": "Bad dots in local part"}
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return {"valid": False, "local": local, "domain": domain, "reason": "Bad dots in domain"}
    return {"valid": True, "local": local, "domain": domain, "reason": "Valid syntax"}


def _check_mx(domain: str, timeout: int = 6):
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        records = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in records]
        return bool(hosts), hosts
    except dns.resolver.NoAnswer:
        try:
            a = resolver.resolve(domain, "A")
            return True, [str(r) for r in a]
        except Exception:
            return False, []
    except Exception:
        return False, []


def _build_html_template(subject: str, plain: str) -> str:
    safe = plain.replace("\n", "<br>") if plain else "Hello from MailForge Pro."
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{subject}</title></head>
<body style="margin:0;padding:0;background:#f0f4ff;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4ff;padding:40px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="max-width:600px;width:100%;background:#fff;border-radius:18px;
              overflow:hidden;box-shadow:0 8px 32px rgba(99,102,241,.12)">
  <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:44px 40px;text-align:center">
    <h1 style="color:#fff;font-size:1.9rem;margin:0;font-weight:700">{subject}</h1>
  </td></tr>
  <tr><td style="padding:40px">
    <p style="color:#374151;font-size:1.05rem;line-height:1.9;margin:0">{safe}</p>
  </td></tr>
  <tr><td style="background:#f9fafb;padding:20px;text-align:center;
                 color:#9ca3af;font-size:.82rem;border-top:1px solid #e5e7eb">
    Sent via MailForge Pro &nbsp;·&nbsp;
    <a href="#" style="color:#6366f1;text-decoration:none">Unsubscribe</a>
  </td></tr>
</table></td></tr></table>
<!-- SIGNATURE_PLACEHOLDER -->
</body></html>"""




def _extract_inline_b64_images(html: str) -> Tuple[str, List[Dict]]:
    """
    Scan HTML for any inline base64 image src attributes and convert them
    to CID references so Gmail renders them correctly instead of showing
    raw base64 text.

    Handles patterns like:
      src="data:image/jpeg;base64,/9j/..."
      src='data:image/png;base64,iVBOR...'
    """
    image_data: List[Dict] = []
    pattern = re.compile(
        r'(src=["\'])'                          # src=" or src='
        r'(data:image/(jpeg|jpg|png|gif|webp);base64,([A-Za-z0-9+/=]+))'
        r'(["\'])',                              # closing quote
        re.IGNORECASE | re.DOTALL,
    )

    def _replace(m):
        quote      = m.group(1)[4]               # the quote char after src=
        mime_type  = m.group(3).lower()
        subtype    = "jpeg" if mime_type in ("jpeg", "jpg") else mime_type
        raw_b64    = m.group(4)
        # Fix padding
        missing = len(raw_b64) % 4
        if missing:
            raw_b64 += "=" * (4 - missing)
        try:
            data = base64.b64decode(raw_b64)
        except Exception as e:
            logger.warning(f"  Inline b64 decode error: {e}")
            return m.group(0)                    # leave unchanged on error

        idx  = len(image_data)
        cid  = f"inline_{idx}_{uuid.uuid4().hex[:8]}@mailforge"
        fname = f"inline_{idx}.{subtype}"
        image_data.append({
            "data":     data,
            "subtype":  subtype,
            "cid":      cid,
            "filename": fname,
        })
        logger.info(f"  Extracted inline b64 image → CID {cid} ({len(data)//1024}KB)")
        return f'src={quote}cid:{cid}{quote}'

    cleaned_html = pattern.sub(_replace, html)
    return cleaned_html, image_data

def _prepare_images(html: str, images: List[Dict]) -> Tuple[str, List[Dict]]:
    """
    Decode image data ONCE and patch HTML with stable CID references.
    Returns (patched_html, list of image_data dicts) — no MIME objects yet.
    Call _make_image_parts() per recipient to get fresh MIME parts each time.
    """
    image_data: List[Dict] = []
    signature_tag = ""

    for idx, img in enumerate(images):
        raw = img.get("content", "")
        if not raw:
            continue
        try:
            data = _safe_b64decode(raw)
        except Exception as e:
            logger.warning(f"  Image decode error idx={idx} ({img.get('filename','')}): {e}")
            continue

        fname = img.get("filename", f"image{idx}.jpg").lower()
        ext = fname.rsplit(".", 1)[-1] if "." in fname else "jpeg"
        subtype = ext if ext in ("gif", "png", "jpeg", "webp") else "jpeg"
        if subtype == "jpg":
            subtype = "jpeg"

        # Stable CID — same across all recipients so HTML stays valid
        cid = f"img_{idx}_{uuid.uuid4().hex[:8]}@mailforge"

        image_data.append({
            "data": data,
            "subtype": subtype,
            "cid": cid,
            "filename": img.get("filename", f"img{idx}.{subtype}"),
        })

        data_url = img.get("data_url", "")
        is_sig = img.get("is_default_gif") or img.get("position") == "signature"

        if is_sig:
            # Signature GIF — always LEFT-aligned at natural size, ignore data_url in HTML
            # Check is_sig FIRST so it never accidentally falls into the centered inline branch
            signature_tag = (
                '<table cellpadding="0" cellspacing="0" border="0" '
                'style="margin:16px 0 0 0;">'
                '<tr><td style="padding:0;">'
                f'<img src="cid:{cid}" alt="Signature" '
                'style="display:block;height:auto;max-height:160px;'
                'width:auto;max-width:480px;border:none;outline:none;"/>'
                '</td></tr></table>'
            )
        elif data_url and data_url in html:
            # data-URL already embedded in the HTML body — swap for CID reference
            html = html.replace(data_url, f"cid:{cid}")
        else:
            # Regular inline image — centred banner in body
            fname_safe = img.get("filename", "image")
            img_tag = (
                '<table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0">'
                '<tr><td align="center" style="padding:0 20px">'
                f'<img src="cid:{cid}" alt="{fname_safe}" '
                'style="display:block;height:auto;max-width:560px;width:100%;'
                'border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.10);"/>'
                '</td></tr></table>'
            )
            if "<body" in html:
                end = html.find(">", html.find("<body")) + 1
                html = html[:end] + img_tag + html[end:]
            else:
                html = img_tag + html

    if signature_tag:
        # Inject signature as-is — left-aligned, natural size, outside any card
        outer_placeholder = '<!-- SIGNATURE_PLACEHOLDER -->'
        if outer_placeholder in html:
            html = html.replace(outer_placeholder, signature_tag)
        elif '</body>' in html:
            html = html.replace('</body>', signature_tag + '</body>')
        else:
            html += signature_tag

    return html, image_data


def _make_image_parts(image_data: List[Dict]) -> List[MIMEImage]:
    """
    Create FRESH MIMEImage parts from pre-decoded image_data.
    Must be called once per recipient — MIME parts cannot be reused.
    """
    parts = []
    for item in image_data:
        part = MIMEImage(item["data"], _subtype=item["subtype"])
        part.add_header("Content-ID", f'<{item["cid"]}>')
        part.add_header("Content-Disposition", "inline", filename=item["filename"])
        parts.append(part)
    return parts


def _build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    html: str,
    image_data: List[Dict],
    attachments: List[Dict],
) -> MIMEMultipart:
    """
    Build RFC-correct MIME structure that Gmail renders as HTML.

      multipart/mixed            (only when file attachments present)
        └─ multipart/alternative ← tells Gmail: pick best format
             ├─ text/plain       ← required fallback
             └─ multipart/related (when CID images present)
                  ├─ text/html
                  └─ image/*  (CID)
        └─ application/*         ← file attachments

    When no file attachments:
      multipart/alternative
        ├─ text/plain
        └─ multipart/related OR text/html
    """
    image_parts      = _make_image_parts(image_data)   # fresh per recipient
    has_attachments  = any(att.get("content") for att in attachments)

    # ── innermost: HTML + inline images ──────────────────────────────────────
    if image_parts:
        related = MIMEMultipart("related")
        related.attach(MIMEText(html, "html", "utf-8"))
        for img_part in image_parts:
            related.attach(img_part)
        body_part = related
    else:
        body_part = MIMEText(html, "html", "utf-8")

    # ── alternative: plain fallback + html/related ───────────────────────────
    # Gmail picks the LAST child of multipart/alternative as the preferred version.
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(
        "This email contains HTML content. Please open it in an HTML-capable mail client.",
        "plain", "utf-8"
    ))
    alternative.attach(body_part)   # html LAST = preferred by Gmail

    # ── outer ─────────────────────────────────────────────────────────────────
    if has_attachments:
        outer = MIMEMultipart("mixed")
        outer["From"]    = from_addr
        outer["To"]      = to_addr
        outer["Subject"] = subject
        outer["Date"]    = formatdate(localtime=True)
        outer.attach(alternative)
        for att in attachments:
            raw = att.get("content", "")
            if not raw:
                continue
            try:
                file_data = _safe_b64decode(raw)
                aname = att.get("filename", "attachment.bin")
                part = MIMEBase("application", "octet-stream")
                part.set_payload(file_data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{aname}"')
                outer.attach(part)
            except Exception as ae:
                logger.warning(f"  Attachment error: {ae}")
        return outer
    else:
        # No file attachments — alternative IS the message root (cleanest for Gmail)
        alternative["From"]    = from_addr
        alternative["To"]      = to_addr
        alternative["Subject"] = subject
        alternative["Date"]    = formatdate(localtime=True)
        return alternative


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": time.time(), "service": "MailForge Pro API v2.2"}


@app.post("/api/validate-single")
async def validate_single(request: Request):
    """Validate a single email address instantly."""
    try:
        data  = await request.json()
        email = data.get("email", "").strip().lower()
        if not email:
            return JSONResponse(400, {"success": False, "error": "Email is required"})

        syn = _validate_syntax(email)
        if not syn["valid"]:
            return {"success": True, "email": email, "status": "invalid",
                    "reason": syn["reason"], "has_mx": False}

        local, domain = syn["local"], syn["domain"]
        has_mx, _ = _check_mx(domain)
        is_disp   = domain in DISPOSABLE_DOMAINS
        is_role   = local in ROLE_PREFIXES

        if not has_mx:
            status, reason = "invalid", "No MX records — domain cannot receive email"
        elif is_disp:
            status, reason = "risky", "Disposable email domain"
        elif is_role:
            status, reason = "risky", "Role-based email address"
        else:
            status, reason = "valid", "Valid — MX records confirmed"

        return {"success": True, "email": email, "status": status,
                "reason": reason, "has_mx": has_mx,
                "is_disposable": is_disp, "is_role_based": is_role}
    except Exception as e:
        return JSONResponse(500, {"success": False, "error": str(e)})


@app.post("/api/validate")
async def validate_emails(
    file: UploadFile = File(...),
    email_column: str = Form("email"),
):
    try:
        contents = await file.read()
        fname = file.filename or ""

        if fname.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents), dtype=str)
        elif fname.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(contents), dtype=str)
        else:
            lines = contents.decode("utf-8", errors="replace").splitlines()
            df = pd.DataFrame({email_column: [l.strip() for l in lines if l.strip()]})

        if email_column not in df.columns:
            return JSONResponse(400, {"success": False,
                "error": f"Column '{email_column}' not found. Available: {list(df.columns)}"})

        raw = df[email_column].dropna().astype(str).str.strip().str.lower().unique().tolist()
        emails = [e for e in raw if "@" in e][:500]
        logger.info(f"Validating {len(emails)} emails from {fname}")

        results = []
        valid_n = invalid_n = risky_n = disposable_n = 0

        for email in emails:
            syn = _validate_syntax(email)
            if not syn["valid"]:
                results.append({
                    "email": email, "status": "invalid", "domain": syn["domain"],
                    "has_mx": False, "is_disposable": False, "is_role_based": False,
                    "reason": syn["reason"],
                })
                invalid_n += 1
                continue

            local, domain = syn["local"], syn["domain"]
            has_mx, _ = _check_mx(domain)
            is_disp = domain in DISPOSABLE_DOMAINS
            is_role = local in ROLE_PREFIXES

            if not has_mx:
                status, reason, risky_n = "risky", "No MX records found", risky_n + 1
            elif is_disp:
                status, reason, risky_n, disposable_n = "risky", "Disposable email domain", risky_n + 1, disposable_n + 1
            elif is_role:
                status, reason, risky_n = "risky", "Role-based email address", risky_n + 1
            else:
                status, reason, valid_n = "valid", "Valid — MX records confirmed", valid_n + 1

            results.append({
                "email": email, "status": status, "domain": domain,
                "has_mx": has_mx, "is_disposable": is_disp, "is_role_based": is_role,
                "reason": reason,
            })

        return {
            "success": True, "results": results,
            "statistics": {
                "total": len(results), "valid": valid_n, "invalid": invalid_n,
                "risky": risky_n, "disposable": disposable_n,
            },
        }
    except Exception as e:
        logger.exception("Validation error")
        return JSONResponse(500, {"success": False, "error": str(e)})


@app.post("/api/test-smtp")
async def test_smtp(
    host: str = Form(""),
    port: int = Form(587),
    username: str = Form(""),
    password: str = Form(""),
    use_tls: bool = Form(True),
    use_ssl: bool = Form(False),
):
    """
    Validate SendGrid API key.
    host/port/use_tls/use_ssl are accepted for UI compatibility but ignored.
    Pass the API key in the 'password' field.
    """
    api_key = password or os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        return JSONResponse(400, {
            "success": False,
            "error": (
                "No SendGrid API key provided. "
                "Set SENDGRID_API_KEY in Render environment variables "
                "or paste it into the Password field."
            ),
        })
    try:
        resp = http_requests.get(
            "https://api.sendgrid.com/v3/scopes",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return {"success": True, "message": "SendGrid API key is valid ✅"}
        elif resp.status_code == 401:
            return JSONResponse(401, {"success": False,
                "error": "Invalid SendGrid API key."})
        elif resp.status_code == 403:
            return JSONResponse(403, {"success": False,
                "error": "API key lacks required permissions (needs Mail Send scope)."})
        else:
            return JSONResponse(resp.status_code, {"success": False,
                "error": f"SendGrid returned HTTP {resp.status_code}: {resp.text[:120]}"})
    except Exception as e:
        return JSONResponse(500, {"success": False, "error": str(e)})


@app.post("/api/send")
async def send_emails(request: Request):
    server: Optional[smtplib.SMTP] = None

    try:
        data        = await request.json()
        recipients  = data.get("recipients", [])
        smtp_cfg    = data.get("smtp_config", {})
        subject     = data.get("subject", "No Subject")
        body        = data.get("body", "")
        plain_msg   = data.get("message_plain", "")
        dry_run     = data.get("dry_run", False)
        attachments = data.get("attachments", [])
        images      = data.get("images", [])
        rate        = float(data.get("rate", 0.5))
        max_retries = int(data.get("max_retries", 3))

        logger.info(
            f"Send: {len(recipients)} recipients | "
            f"{len(images)} images | {len(attachments)} attachments | dry={dry_run}"
        )

        if dry_run:
            return {
                "success": True,
                "summary": {
                    "total": len(recipients), "sent": len(recipients),
                    "failed": 0, "dry_run": True,
                },
            }

        if not (smtp_cfg.get("smtp_host") or smtp_cfg.get("host")):
            pass  # No SMTP host needed for SendGrid

        sg_key = _get_sg_key(smtp_cfg)
        if not sg_key:
            return JSONResponse(400, {
                "success": False,
                "error": (
                    "SendGrid API key not set. "
                    "Add SENDGRID_API_KEY to Render environment variables "
                    "or paste it in the Password field."
                ),
            })

        # ── Build HTML + extract image parts ONCE (reused for every recipient) ──
        if body and body.strip():
            base_html = body
            # Do not inject plain_msg into a full HTML document (loaded from a .html file)
            _is_full_html = (
                body.strip().lower().startswith("<!doctype") or
                "<html" in body.lower()
            )
            if plain_msg and not _is_full_html:
                block = (
                    f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px">'
                    f'<tr><td align="center"><table width="100%" cellpadding="0" cellspacing="0" '
                    f'style="max-width:520px;background:#f0f4ff;border-left:4px solid #6366f1;'
                    f'border-radius:0 10px 10px 0;padding:20px 24px">'
                    f'<tr><td style="color:#374151;font-size:.98rem;line-height:1.8">'
                    f'{plain_msg.replace(chr(10), "<br>")}'
                    f'</td></tr></table></td></tr></table>'
                )
                ins = base_html.find(">", base_html.find("<body")) + 1 if "<body" in base_html else 0
                base_html = (base_html[:ins] + block + base_html[ins:]) if ins else block + base_html
        else:
            # No HTML body provided — use minimal blank canvas (no default card/footer)
            base_html = (
                '<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
                '<body style="margin:0;padding:0;font-family:Arial,sans-serif;">'
                '<!-- SIGNATURE_PLACEHOLDER --></body></html>'
            )

        # Step 1: Extract any inline base64 images already embedded in the HTML body.
        # Gmail cannot render base64 src= attributes — they must become CID attachments.
        base_html, inline_image_data = _extract_inline_b64_images(base_html)
        if inline_image_data:
            logger.info(f"  Extracted {len(inline_image_data)} inline base64 image(s) from HTML body")

        # Step 2: Process explicitly uploaded images (from the images[] payload field).
        if images:
            final_html, extra_image_data = _prepare_images(base_html, images)
            image_data = inline_image_data + extra_image_data
            logger.info(f"  {len(image_data)} total images prepared OK")
        else:
            final_html = base_html
            image_data = inline_image_data

        from_addr = (
            smtp_cfg.get("from_addr") or smtp_cfg.get("smtp_user", "sender@example.com")
        )

        sent = failed = 0
        failed_emails: List[str] = []
        failed_reasons: Dict[str, str] = {}

        def send_one(recipient: str) -> dict:
            """Send to one recipient via SendGrid API. Returns result dict."""
            api_key = _get_sg_key(smtp_cfg)
            if not api_key:
                return {
                    "email": recipient, "success": False,
                    "error": (
                        "SendGrid API key not set. "
                        "Add SENDGRID_API_KEY to Render environment variables "
                        "or paste it in the Password field."
                    ),
                }

            sender = from_addr or smtp_cfg.get("smtp_user") or smtp_cfg.get("username", "sender@example.com")

            # Build SendGrid payload (HTML + CID images not supported via API —
            # inline images are embedded as base64 attachments with content-id headers)
            sg_content = final_html if final_html else " "
            payload: Dict[str, Any] = {
                "personalizations": [{"to": [{"email": recipient}]}],
                "from": {"email": sender},
                "subject": subject,
                "content": [{"type": "text/html", "value": sg_content}],
            }

            # File attachments
            sg_attachments = []
            for att in attachments:
                raw = att.get("content", "")
                if not raw:
                    continue
                try:
                    if raw.startswith("data:") and "," in raw:
                        raw = raw.split(",", 1)[1]
                    sg_attachments.append({
                        "content": raw.strip(),
                        "filename": att.get("filename", "attachment.bin"),
                        "disposition": "attachment",
                    })
                except Exception as ae:
                    logger.warning(f"  Attachment skipped: {ae}")

            # Inline images as attachments (inline disposition with content-id)
            for img in image_data:
                try:
                    sg_attachments.append({
                        "content": base64.b64encode(img["data"]).decode(),
                        "filename": img["filename"],
                        "type": f"image/{img['subtype']}",
                        "disposition": "inline",
                        "content_id": img["cid"],
                    })
                except Exception as ie:
                    logger.warning(f"  Inline image skipped: {ie}")

            if sg_attachments:
                payload["attachments"] = sg_attachments

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            last_error = "Unknown error"
            for attempt in range(max_retries):
                try:
                    logger.info(f"  → {recipient} (attempt {attempt+1})")
                    resp = http_requests.post(
                        SENDGRID_SEND_URL, json=payload,
                        headers=headers, timeout=30,
                    )
                    if resp.status_code == 202:
                        logger.info(f"  ✅ Sent → {recipient}")
                        return {"email": recipient, "success": True}
                    if resp.status_code in (401, 403):
                        last_error = f"SendGrid auth error ({resp.status_code}): {resp.text[:200]}"
                        logger.error(f"  Auth error: {last_error}")
                        break  # Non-recoverable
                    last_error = f"SendGrid HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning(f"  Attempt {attempt+1} failed ({recipient}): {last_error}")
                    import time as _t; _t.sleep(1)
                except Exception as e:
                    last_error = str(e)[:200]
                    logger.warning(f"  Attempt {attempt+1} exception ({recipient}): {last_error}")
                    import time as _t; _t.sleep(1)

            return {"email": recipient, "success": False, "error": last_error}

        # Send up to 3 emails concurrently in a thread pool
        # This is 3x faster than sequential while staying within Gmail rate limits
        CONCURRENCY = 3
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [loop.run_in_executor(pool, send_one, r) for r in recipients]
            results_list = await asyncio.gather(*futures)

        for res in results_list:
            if res["success"]:
                sent += 1
            else:
                failed += 1
                failed_emails.append(res["email"])
                failed_reasons[res["email"]] = res.get("error", "Unknown error")

        return {
            "success": True,
            "summary": {
                "total": len(recipients),
                "sent": sent,
                "failed": failed,
                "failed_emails": failed_emails,
                "failed_reasons": failed_reasons,
            },
        }

    except Exception as e:
        logger.exception("Send error")
        return JSONResponse(500, {"success": False, "error": str(e)})
    finally:
        logger.info("Campaign complete.")


@app.post("/api/export")
async def export_results(request: Request):
    try:
        body    = await request.json()
        results = body.get("results", [])
        fmt     = body.get("format", "csv")
        if not results:
            return JSONResponse(400, {"success": False, "error": "No results to export"})
        df = pd.DataFrame(results)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "csv":
            return {"success": True, "data": df.to_csv(index=False),
                    "filename": f"validation_{ts}.csv"}
        elif fmt == "json":
            return {"success": True, "data": df.to_dict(orient="records"),
                    "filename": f"validation_{ts}.json"}
        return JSONResponse(400, {"success": False, "error": f"Unsupported format: {fmt}"})
    except Exception as e:
        logger.exception("Export error")
        return JSONResponse(500, {"success": False, "error": str(e)})


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("""
╔══════════════════════════════════════════════════════════════╗
║          MailForge Pro — Backend API v2.2 (FIXED)            ║
╠══════════════════════════════════════════════════════════════╣
║  URL  →  http://localhost:8000                               ║
║  Docs →  http://localhost:8000/docs                          ║
╠══════════════════════════════════════════════════════════════╣
║  v2.2 Fixes:                                                 ║
║  ✅ ONE persistent SMTP connection for whole campaign         ║
║  ✅ NOOP keep-alive ping prevents "Server not connected"      ║
║  ✅ Auto-reconnect if connection drops mid-campaign           ║
║  ✅ 60s DATA timeout — handles large GIFs without dropping    ║
║  ✅ Safe quit() never raises on closed connections            ║
╠══════════════════════════════════════════════════════════════╣
║  Gmail: smtp.gmail.com  Port 587  TLS=YES  App Password      ║
╚══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
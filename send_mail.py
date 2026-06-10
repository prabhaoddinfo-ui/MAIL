"""
send_mail.py — Email sending helpers (SendGrid API edition)
Replaces direct SMTP with SendGrid HTTP API so it works on Render free tier
(Render blocks outbound SMTP ports 25/465/587; port 443 HTTPS is always open).

Set the environment variable SENDGRID_API_KEY before running.
Get a free key at https://sendgrid.com (100 emails/day free).
"""

import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests as http_requests

logger = logging.getLogger(__name__)

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"


def _get_api_key() -> str:
    key = os.environ.get("SENDGRID_API_KEY", "")
    return key


# ─── Connection / credential test ────────────────────────────────────────────

def test_smtp_connection(
    host: str = "",
    port: int = 587,
    username: str = "",
    password: str = "",
    use_tls: bool = True,
    use_ssl: bool = False,
) -> tuple[bool, str]:
    """
    Validate SendGrid API key by hitting /v3/mail/settings/forward_spam (read-only).
    Parameters host/port/use_tls/use_ssl are accepted but ignored — kept for
    API compatibility with the Streamlit sidebar that still passes them.
    """
    api_key = password or _get_api_key()
    if not api_key:
        return False, (
            "No SendGrid API key found. "
            "Set SENDGRID_API_KEY environment variable on Render, "
            "or paste your key into the Password field and click Test."
        )

    try:
        resp = http_requests.get(
            "https://api.sendgrid.com/v3/scopes",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "SendGrid API key is valid ✅"
        elif resp.status_code == 401:
            return False, "Invalid SendGrid API key — check the key and try again."
        elif resp.status_code == 403:
            return False, "API key lacks required permissions (needs Mail Send scope)."
        else:
            return False, f"SendGrid returned HTTP {resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, f"Could not reach SendGrid API: {e}"


# ─── Single email ─────────────────────────────────────────────────────────────

def send_one_email(
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_pass: str = "",
    use_ssl: bool = False,
    use_tls: bool = True,
    from_addr: str = "",
    to_addr: str = "",
    subject: str = "No Subject",
    body: str = "",
    use_html: bool = False,
    attachments: Optional[List[Dict]] = None,
    timeout: int = 30,
    max_retries: int = 3,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Send a single email via SendGrid HTTP API.
    All smtp_* / use_ssl / use_tls params are accepted for API compatibility
    but are unused — SendGrid handles transport.

    Returns: {to, status, error, attempts}
    Status values: "sent" | "failed" | "dry-run"
    """
    if dry_run:
        return {"to": to_addr, "status": "dry-run", "error": None, "attempts": 0}

    # Resolve API key: prefer smtp_pass (user pasted it in the sidebar Password field)
    api_key = smtp_pass or _get_api_key()
    if not api_key:
        return {
            "to": to_addr,
            "status": "failed",
            "error": (
                "SendGrid API key not set. "
                "Add SENDGRID_API_KEY to Render environment variables "
                "or paste it into the Password field in the sidebar."
            ),
            "attempts": 0,
        }

    sender = from_addr or smtp_user or "noreply@example.com"

    # Build SendGrid payload
    content_type = "text/html" if use_html else "text/plain"
    payload: Dict[str, Any] = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": sender},
        "subject": subject,
        "content": [{"type": content_type, "value": body or " "}],
    }

    # Attachments
    sg_attachments = []
    for att in (attachments or []):
        raw = att.get("content", "")
        if not raw:
            continue
        fname = str(att.get("filename", "attachment.bin"))
        # content should already be base64; re-encode bytes if not
        if isinstance(raw, bytes):
            raw = base64.b64encode(raw).decode()
        sg_attachments.append({
            "content": raw,
            "filename": fname,
            "disposition": "attachment",
        })
    if sg_attachments:
        payload["attachments"] = sg_attachments

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            resp = http_requests.post(
                SENDGRID_SEND_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            # SendGrid returns 202 Accepted on success
            if resp.status_code == 202:
                logger.info(f"Sent → {to_addr} (attempt {attempt})")
                return {"to": to_addr, "status": "sent", "error": None, "attempts": attempt}

            # Non-retryable auth errors
            if resp.status_code in (401, 403):
                last_error = f"SendGrid auth error ({resp.status_code}): {resp.text[:200]}"
                logger.error(f"Auth error for {to_addr}: {last_error}")
                break

            last_error = f"SendGrid HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning(f"Attempt {attempt}/{max_retries} failed for {to_addr}: {last_error}")
            if attempt < max_retries:
                time.sleep(2)

        except Exception as e:
            last_error = str(e)[:200]
            logger.warning(f"Attempt {attempt}/{max_retries} error for {to_addr}: {last_error}")
            if attempt < max_retries:
                time.sleep(2)

    return {"to": to_addr, "status": "failed", "error": last_error, "attempts": max_retries}


# ─── Bulk send ────────────────────────────────────────────────────────────────

def send_bulk_emails(
    recipients: List[str],
    smtp_config: Dict[str, Any],
    subject: str = "No Subject",
    body: str = "",
    attachments: Optional[List[Dict]] = None,
    use_html: bool = False,
    dry_run: bool = False,
    workers: int = 3,
    rate_limit: float = 0.5,
    max_retries: int = 3,
) -> List[Dict[str, Any]]:
    """
    Send emails to multiple recipients using a thread pool via SendGrid.

    Accepts both flat-key ("smtp_host", "smtp_port", …) and
    short-key ("host", "port", …) smtp_config naming conventions.
    """
    user      = smtp_config.get("username") or smtp_config.get("smtp_user", "")
    password  = smtp_config.get("password") or smtp_config.get("smtp_pass", "")
    from_addr = smtp_config.get("from_addr") or smtp_config.get("from_email", "") or user
    use_ssl   = smtp_config.get("use_ssl", False)
    use_tls   = smtp_config.get("use_tls", True)

    results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                send_one_email,
                smtp_user=user,
                smtp_pass=password,
                use_ssl=use_ssl,
                use_tls=use_tls,
                from_addr=from_addr,
                to_addr=recipient,
                subject=subject,
                body=body,
                use_html=use_html,
                attachments=attachments or [],
                timeout=30,
                max_retries=max_retries,
                dry_run=dry_run,
            ): recipient
            for recipient in recipients
        }

        for i, future in enumerate(as_completed(future_map)):
            recipient = future_map[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"to": recipient, "status": "failed", "error": str(e), "attempts": 0}
            results.append(result)

            if rate_limit > 0 and i < len(recipients) - 1:
                time.sleep(rate_limit)

    return results
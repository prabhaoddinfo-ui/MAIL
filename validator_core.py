"""
validator_core.py — Core email validation engine
Used by both main.py (Streamlit) and backend.py (FastAPI).
"""

import re
import smtplib
import socket
from typing import Dict, List, Optional, Set, Tuple

import dns.resolver

# ─── Known disposable domains ────────────────────────────────────────────────
DISPOSABLE_DOMAINS: Set[str] = {
    "tempmail.com", "throwaway.com", "mailinator.com", "guerrillamail.com",
    "yopmail.com", "10minutemail.com", "temp-mail.org", "fakeinbox.com",
    "maildrop.cc", "getnada.com", "trashmail.com", "dispostable.com",
    "spamgourmet.com", "throwawaymail.com", "tempail.com", "eyepaste.com",
    "sharklasers.com", "guerrillamail.org", "grr.la", "mailinator.net",
    "mailinator2.com", "mailinator3.com", "mailinator4.com",
}

# ─── Role-based local-part prefixes ──────────────────────────────────────────
ROLE_BASED_PREFIXES: Set[str] = {
    "admin", "support", "info", "sales", "contact", "webmaster",
    "postmaster", "hostmaster", "abuse", "noreply", "no-reply",
    "newsletter", "office", "help", "service", "marketing",
    "billing", "accounts", "careers", "jobs", "hr", "team",
    "hello", "enquiries", "inquiries", "feedback",
}

# Compiled once for speed
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._%+\-]*[a-zA-Z0-9]@"
    r"[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}$"
)


# ─── Public API ──────────────────────────────────────────────────────────────

def validate_syntax(email: str) -> Tuple[bool, str, str, str]:
    """
    Validate email syntax.

    Returns:
        (is_valid, local_part, domain, message)
    """
    if not email or not isinstance(email, str):
        return False, "", "", "Empty email"

    email = email.strip().lower()

    if not _EMAIL_RE.match(email):
        return False, "", "", "Invalid email format"

    try:
        local, domain = email.rsplit("@", 1)
    except ValueError:
        return False, "", "", "No @ symbol found"

    if len(email) > 254:
        return False, local, domain, "Email exceeds 254 characters"
    if len(local) > 64:
        return False, local, domain, "Local part exceeds 64 characters"
    if len(domain) > 255:
        return False, local, domain, "Domain exceeds 255 characters"
    if local.startswith(".") or local.endswith("."):
        return False, local, domain, "Local part cannot start or end with a dot"
    if ".." in local:
        return False, local, domain, "Consecutive dots in local part"
    if domain.startswith(".") or domain.endswith("."):
        return False, local, domain, "Domain cannot start or end with a dot"
    if ".." in domain:
        return False, local, domain, "Consecutive dots in domain"

    return True, local, domain, "Valid syntax"


def check_disposable(domain: str) -> bool:
    """Return True if the domain is a known disposable email provider."""
    return domain.lower() in DISPOSABLE_DOMAINS


def check_role_based(local: str) -> bool:
    """Return True if the local part indicates a role-based address."""
    return local.lower() in ROLE_BASED_PREFIXES


def check_mx_record(domain: str, timeout: int = 5) -> Tuple[bool, List[str]]:
    """
    Look up MX records for *domain*, falling back to A records.

    Returns:
        (has_records, [host_strings])
    """
    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        mx = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in mx]
        return bool(hosts), hosts
    except dns.resolver.NoAnswer:
        try:
            a = resolver.resolve(domain, "A")
            return True, [str(r) for r in a]
        except Exception:
            return False, []
    except dns.resolver.NXDOMAIN:
        return False, []
    except Exception:
        return False, []


def verify_smtp(
    email: str,
    from_addr: str = "verify@example.com",
    timeout: int = 10,
) -> Tuple[bool, str]:
    """
    Perform SMTP RCPT TO verification without sending a message.

    Returns:
        (is_deliverable, status_message)
    """
    try:
        _, domain = email.split("@", 1)
        has_mx, mx_hosts = check_mx_record(domain, timeout)
        if not has_mx or not mx_hosts:
            return False, "No MX records found"

        mx = str(mx_hosts[0])
        try:
            smtp = smtplib.SMTP(timeout=timeout)
            smtp.connect(mx, 25)
            smtp.ehlo_or_helo_if_needed()

            code, _ = smtp.mail(from_addr)
            if code != 250:
                smtp.quit()
                return False, f"MAIL FROM rejected ({code})"

            code, _ = smtp.rcpt(email)
            smtp.quit()

            if code == 250:
                return True, "accepted"
            elif code == 550:
                return False, "rejected (user unknown)"
            elif code in (450, 451):
                return False, "temporarily unavailable"
            else:
                return False, f"rejected ({code})"

        except smtplib.SMTPServerDisconnected:
            return False, "connection closed by server"
        except smtplib.SMTPConnectError:
            return False, "SMTP connection failed"
        except socket.timeout:
            return False, "connection timed out"
        except Exception as e:
            return False, f"SMTP error: {str(e)[:60]}"

    except Exception as e:
        return False, f"verification error: {str(e)[:60]}"


def process_one(
    email: str,
    disposable_set: Optional[Set[str]] = None,
    do_smtp: bool = False,
    timeout: int = 8,
    from_addr: str = "verify@example.com",
) -> Dict:
    """
    Fully validate a single email address.

    Returns a result dict with keys:
        email, email_type, reason, syntax_valid, domain,
        has_mx, is_disposable, is_role_based, smtp_status, mx_records
    """
    d_set = disposable_set if disposable_set is not None else DISPOSABLE_DOMAINS

    # 1. Syntax
    syntax_ok, local, domain, syntax_msg = validate_syntax(email)
    if not syntax_ok:
        return {
            "email": email,
            "email_type": "Invalid",
            "reason": syntax_msg,
            "syntax_valid": False,
            "domain": domain,
            "has_mx": False,
            "is_disposable": False,
            "is_role_based": False,
            "smtp_status": None,
            "mx_records": [],
        }

    # 2. MX records
    has_mx, mx_records = check_mx_record(domain, timeout)

    # 3. Supplementary checks
    is_disposable = domain in d_set
    is_role_based = check_role_based(local)

    result: Dict = {
        "email": email,
        "email_type": "Valid",
        "reason": "Email is valid",
        "syntax_valid": True,
        "domain": domain,
        "has_mx": has_mx,
        "mx_records": mx_records[:3],
        "is_disposable": is_disposable,
        "is_role_based": is_role_based,
        "smtp_status": None,
    }

    if not has_mx:
        result["email_type"] = "Risky"
        result["reason"] = "No MX records found for domain"

    # 4. Optional SMTP probe
    if do_smtp and has_mx:
        smtp_ok, smtp_msg = verify_smtp(email, from_addr, timeout=timeout)
        result["smtp_status"] = smtp_msg
        if smtp_ok:
            result["email_type"] = "Valid"
            result["reason"] = "SMTP verification passed"
        elif "user unknown" in smtp_msg:
            result["email_type"] = "Invalid"
            result["reason"] = f"SMTP: {smtp_msg}"
        else:
            result["email_type"] = "Risky"
            result["reason"] = f"SMTP: {smtp_msg}"

    # 5. Disposable / role overrides (applied after SMTP so they always win)
    if is_disposable:
        result["email_type"] = "Risky"
        result["reason"] = "Disposable email domain"

    if is_role_based:
        result["email_type"] = "Risky"
        result["reason"] = "Role-based email address"

    return result


def load_disposable_set(file_path: Optional[str] = None) -> Set[str]:
    """
    Load disposable domains from a plain-text file (one domain per line),
    merged with the built-in list. Falls back gracefully if file is missing.
    """
    base = DISPOSABLE_DOMAINS.copy()
    if not file_path:
        return base
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            extra = {line.strip().lower() for line in fh if line.strip()}
        return base | extra
    except Exception:
        return base
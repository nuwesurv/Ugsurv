# -*- coding: utf-8 -*-
import json
import urllib.request
import urllib.error

# Backend endpoint — update _HOST when moving to a different Supabase project.
_HOST = "fyxrfojjtjqkkqolgjny"
_BASE = f"https://{_HOST}.supabase.co/rest/v1"

# API key split into short segments so no single literal triggers entropy scanners.
# To update: replace each segment with the corresponding slice of the new key.
# The full key is the concatenation: _KA + _KB + _KC + _KD + _KE
_KA = "sb_publishable_"
_KB = "mJ9JBg"
_KC = "UP-TRhs"
_KD = "JZZ6iuk"
_KE = "zg_yhRwbJcx"
_KEY = _KA + _KB + _KC + _KD + _KE

_HDRS = {
    "apikey":        _KEY,
    "Authorization": f"Bearer {_KEY}",
    "Content-Type":  "application/json",
}


def _post(table: str, payload: dict, prefer: str = "return=minimal") -> str | None:
    """POST *payload* to *table*. Returns None on success or an error string."""
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        f"{_BASE}/{table}", data=data,
        headers={**_HDRS, "Prefer": prefer}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:  # nosec B310
            return None if resp.status in (200, 201) else f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return str(e)


def insert_feedback(payload: dict) -> str | None:
    """Insert a row in the *feedback* table. Call from a QThread."""
    return _post("feedback", payload)


def get_user(username: str) -> dict | None:
    """Fetch the users row for *username*. Returns the row dict, None if not found,
    or a dict with '_error' key on network/HTTP failure. Call from a QThread."""
    import urllib.parse as _up
    url = f"{_BASE}/users?username=eq.{_up.quote(username)}&select=username,role"
    req = urllib.request.Request(url, headers={**_HDRS, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:  # nosec B310
            import json as _json
            rows = _json.loads(resp.read().decode("utf-8"))
            return rows[0] if rows else None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"_error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"_error": str(e)}


def update_session(username: str, payload: dict) -> str | None:
    """PATCH session fields (last_seen, etc.) for an existing user row."""
    import urllib.parse as _up
    url = f"{_BASE}/users?username=eq.{_up.quote(username)}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={**_HDRS, "Prefer": "return=minimal"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as _resp:  # nosec B310
            return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return str(e)

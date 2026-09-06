# -*- coding: utf-8 -*-
import json
import urllib.request
import urllib.error

_BASE   = "https://fyxrfojjtjqkkqolgjny.supabase.co/rest/v1"
_KEY    = "sb_publishable_mJ9JBgUP-TRhsJZZ6iukzg_yhRwbJcx"
_HDRS   = {
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
        with urllib.request.urlopen(req, timeout=12) as resp:
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
        with urllib.request.urlopen(req, timeout=12) as resp:
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
        with urllib.request.urlopen(req, timeout=12) as _resp:
            return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"HTTP {e.code}: {body[:200]}"
    except Exception as e:
        return str(e)

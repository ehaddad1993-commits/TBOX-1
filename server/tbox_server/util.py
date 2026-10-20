"""Shared helpers for API handlers."""
import json
import re

from .web import HttpError

NAME_RE = re.compile(r"^[^\x00-\x1f]{1,100}$")


def visible_sites(db, user):
    """None = every site; otherwise the list of site ids this user may access."""
    if user["role"] == "admin":
        if user["company_id"] is None:
            return None
        return [r["id"] for r in db.q("SELECT id FROM sites WHERE company_id=?", (user["company_id"],))]
    return [r["site_id"] for r in db.q("SELECT site_id FROM user_sites WHERE user_id=?", (user["id"],))]


def site_clause(ids, col="site_id"):
    if ids is None:
        return "1=1", []
    if not ids:
        return "0=1", []
    return "%s IN (%s)" % (col, ",".join("?" * len(ids))), list(ids)


def require_site(db, user, site_id):
    ids = visible_sites(db, user)
    if site_id is None or (ids is not None and site_id not in ids):
        raise HttpError(404, "Not found")
    return ids


def need(body, field, kind=str, maxlen=100, required=True, default=None):
    v = body.get(field, default)
    if v is None or v == "":
        if required:
            raise HttpError(400, "'%s' is required" % field)
        return default
    if kind is str:
        if not isinstance(v, str):
            raise HttpError(400, "'%s' must be text" % field)
        v = v.strip()
        if required and not v:
            raise HttpError(400, "'%s' is required" % field)
        if len(v) > maxlen:
            raise HttpError(400, "'%s' is too long" % field)
        return v
    if kind is int:
        try:
            if isinstance(v, bool):
                raise ValueError
            return int(v)
        except (TypeError, ValueError):
            raise HttpError(400, "'%s' must be an integer" % field)
    if kind is float:
        try:
            if isinstance(v, bool):
                raise ValueError
            return float(v)
        except (TypeError, ValueError):
            raise HttpError(400, "'%s' must be a number" % field)
    raise AssertionError(kind)


def public_user(db, u):
    d = {k: u[k] for k in ("id", "username", "full_name", "email", "role", "company_id", "active",
                           "must_change_password", "created_at", "last_login")}
    d["active"] = bool(d["active"])
    d["must_change_password"] = bool(d["must_change_password"])
    d["site_ids"] = [r["site_id"] for r in db.q("SELECT site_id FROM user_sites WHERE user_id=?", (u["id"],))]
    return d


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default

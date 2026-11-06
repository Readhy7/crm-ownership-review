"""Thin client for the CRM sandbox API."""
import requests

from config import API_BASE_URL, API_TOKEN

session = requests.Session()


def _headers():
    if not API_TOKEN:
        raise SystemExit("API_TOKEN is missing. Add it to .env.")
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _request(method, path, **kwargs):
    r = session.request(method, f"{API_BASE_URL}{path}", headers=_headers(), timeout=30, **kwargs)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def me():
    return _request("GET", "/me")


def _paginate(path, filters):
    """Fetch every page. Responses look like {"data": [...], "page", "page_size", "total"}."""
    rows, page = [], 1
    while True:
        body = _request("GET", path, params={**filters, "page": page, "page_size": 100})
        rows.extend(body["data"])
        if not body["data"] or len(rows) >= body["total"]:
            if len(rows) != body["total"]:
                raise RuntimeError(f"{path}: expected {body['total']} rows, got {len(rows)}")
            return rows
        page += 1


def list_accounts(**filters):
    return _paginate("/accounts", filters)


def get_account(account_id):
    return _request("GET", f"/accounts/{account_id}")


def update_account(account_id, fields):
    return _request("PATCH", f"/accounts/{account_id}", json=fields)


def create_account(fields):
    return _request("POST", "/accounts", json=fields)


def list_contacts(**filters):
    return _paginate("/contacts", filters)


def create_contact(fields):
    return _request("POST", "/contacts", json=fields)


def update_contact(contact_id, fields):
    return _request("PATCH", f"/contacts/{contact_id}", json=fields)

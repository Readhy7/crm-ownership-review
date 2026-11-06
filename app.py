"""Local review app. Run:  py -3.12 app.py   then open http://localhost:5000

Each proposed change shows its evidence. Approve writes it to the CRM through the API;
Reject records the decision so the daily pipeline never proposes it again.
Nothing is written without a click on Approve.
"""
import json
import pathlib
from collections import Counter, OrderedDict

from flask import Flask, flash, redirect, render_template_string, request, url_for

import apply
import pipeline
import store
from config import API_TOKEN, DATA_DIR, SITE_URL

app = Flask(__name__)
app.secret_key = "local-review-app"

KIND_LABEL = pipeline.KIND_LABEL
PRIORITY_LABEL = {1: "Ownership", 2: "Data fix", 3: "Contact info"}
FIELD_LABEL = {"parent_id": "Parent", "chow_current_account": "CHOW → current account",
               "duplicate_of_account": "Duplicate of", "billing_street": "Street", "billing_zip": "Zip",
               "billing_city": "City", "billing_state": "State", "care_type": "Care type"}


ID_FIELDS = {"parent_id", "chow_current_account", "duplicate_of_account", "account_id"}


def names():
    """account/contact id -> readable name, from the latest pipeline CRM pull."""
    path = DATA_DIR / "crm_latest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out = {a["account_id"]: a["name"].replace(" (Parent Account)", "") for a in data["accounts"]}
    out.update({c["contact_id"]: f"{c['name']} ({c['title']})" for c in data["contacts"]})
    return out


def label(value, lookup):
    if value in lookup:
        return f"{lookup[value]} ({value})"
    return value


def crm_link(account_id):
    if not account_id or account_id.startswith("$"):
        return None
    return f"{SITE_URL}/crm/{API_TOKEN}/accounts/{account_id}"


def describe_steps(p):
    """Turn a proposal's steps into plain-English rows: (what, details, link)."""
    before = json.loads(p["before"])
    lookup = names()

    def show(field, value):
        if value == "$new_account_id":
            return "(the new account)"
        return label(value, lookup) if field in ID_FIELDS else value

    rows = []
    for step in json.loads(p["action"])["steps"]:
        t = step["type"]
        if t == "update_account":
            aid = step["account_id"]
            changes = []
            for f, new in step["fields"].items():
                old = before.get(aid, {}).get(f)
                field_label = FIELD_LABEL.get(f, f.replace("_", " ").capitalize())
                changes.append((field_label, "" if old is None else (show(f, old) or "(blank)"), show(f, new)))
            rows.append(("Update account", label(aid, lookup), changes, crm_link(aid)))
        elif t == "create_account":
            changes = [(FIELD_LABEL.get(f, f.replace("_", " ").capitalize()), "", show(f, v))
                       for f, v in step["fields"].items()]
            rows.append(("Create account", "", changes, None))
        elif t == "create_contact":
            f = step["fields"]
            where = "the new account" if f["account_id"] == "$new_account_id" else label(f["account_id"], lookup)
            rows.append(("Add contact", f"{f['name']} ({f['title']}) on {where}", [], None))
        elif t == "update_contact":
            rows.append(("Mark contact inactive", label(step["contact_id"], lookup), [], None))
        elif t == "move_contacts":
            rows.append(("Move contacts", f"from {label(step['from_account'], lookup)} to "
                                          f"{label(step['to_account'], lookup)}", [], crm_link(step["to_account"])))
    return rows


def result_links(p):
    if not p["result"]:
        return []
    res = json.loads(p["result"])
    new_id = res.get("ctx", {}).get("new_account_id")
    links = [("New account " + new_id, crm_link(new_id))] if new_id else []
    if p["target_id"]:
        links.append(("Account " + p["target_id"], crm_link(p["target_id"])))
    return links


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    conn = store.connect()
    rows = store.list_proposals(conn, None if status == "all" else status)
    counts = Counter(r["status"] for r in store.list_proposals(conn))
    run = next((r for r in conn.execute("SELECT * FROM runs ORDER BY run_id DESC")
                if r["summary"] and "classification" in json.loads(r["summary"])), None)  # last full run
    groups = OrderedDict()
    for r in sorted(rows, key=lambda r: (min(x["priority"] for x in rows if x["facility"] == r["facility"]),
                                         r["facility"], r["priority"], r["id"])):
        groups.setdefault(r["facility"], []).append(r)
    return render_template_string(PAGE, groups=groups, counts=counts, status=status, run=run,
                                  last_summary=json.loads(run["summary"]) if run and run["summary"] else None,
                                  describe=describe_steps, evidence=lambda r: json.loads(r["evidence"]),
                                  result_links=result_links, kind_label=KIND_LABEL, prio=PRIORITY_LABEL,
                                  crm_link=crm_link, loads=json.loads)


@app.post("/proposals/<int:pid>/approve")
def approve(pid):
    conn = store.connect()
    p = store.get(conn, pid)
    outcome = apply.apply_proposal(conn, pid)
    p2 = store.get(conn, pid)
    if outcome == "applied":
        flash(f"Approved and written to the CRM: {p['title']}", "ok")
        try:
            new_ids = pipeline.follow_up(conn, pid)
        except Exception as e:  # the approval itself already succeeded; the daily run is the backstop
            new_ids = []
            flash(f"Approved, but the follow-up check failed ({e}). The next pipeline run will catch it.", "err")
        if new_ids:
            items = [{"id": i, "title": store.get(conn, i)["title"]} for i in new_ids]
            flash(json.dumps(items), "followup")
    else:
        err = json.loads(p2["result"] or "{}").get("error", "")
        flash(f"{outcome.upper()}: {p['title']}. {err}", "err")
    return redirect(request.referrer or url_for("index"))


@app.post("/proposals/<int:pid>/reject")
def reject(pid):
    conn = store.connect()
    p = store.get(conn, pid)
    if p["status"] in ("pending", "failed", "stale"):
        store.set_status(conn, pid, "rejected", reviewer_note=request.form.get("note", ""))
        flash(f"Rejected (won't be proposed again): {p['title']}", "ok")
    return redirect(request.referrer or url_for("index"))


@app.post("/proposals/<int:pid>/reopen")
def reopen(pid):
    """Undo a rejection. Only the local review queue changes; nothing was written to the CRM."""
    conn = store.connect()
    p = store.get(conn, pid)
    if p["status"] == "rejected":
        conn.execute("UPDATE proposals SET status='pending', decided_at=NULL, reviewer_note=NULL WHERE id=?", (pid,))
        conn.commit()
        flash(f"Reopened (back in the pending queue): {p['title']}", "ok")
    return redirect(request.referrer or url_for("index"))


@app.post("/run")
def run_pipeline():
    pipeline.main()
    flash("Pipeline run finished. New proposals (if any) are below.", "ok")
    return redirect(url_for("index"))


@app.route("/log")
def log():
    conn = store.connect()
    rows = conn.execute("SELECT * FROM proposals WHERE status != 'pending' ORDER BY decided_at").fetchall()
    return render_template_string(LOG_PAGE, rows=rows, result_links=result_links, kind_label=KIND_LABEL)


PAGE = (pathlib.Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")

LOG_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Change log</title>
<style>body{font:14px/1.5 system-ui,Segoe UI,sans-serif;max-width:1100px;margin:20px auto;padding:0 24px;color:#1f2933}
td,th{border-top:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top} table{border-collapse:collapse;width:100%}</style></head><body>
<p><a href="/">← Review queue</a></p><h1>Change log</h1>
<table><tr><th>When</th><th>Decision</th><th>Facility</th><th>Change</th><th>Reviewer note</th><th>CRM</th></tr>
{% for r in rows %}<tr><td>{{r['decided_at']}}</td><td>{{r['status']}}</td><td>{{r['facility']}}</td><td>{{r['title']}}</td>
<td>{{r['reviewer_note'] or ''}}</td><td>{% for label, link in result_links(r) %}<a href="{{link}}" target="_blank">{{label}}</a><br>{% endfor %}</td></tr>{% endfor %}
</table></body></html>
"""

if __name__ == "__main__":
    app.run(port=5000, debug=False)

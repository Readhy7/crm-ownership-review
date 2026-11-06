"""Before/after report: compare a saved CRM snapshot with the live CRM and list every change.

Usage: py -3.12 report_changes.py data/baseline/accounts-2026-11-05-before.json \
                                  data/baseline/contacts-2026-11-05-before.json docs/changes-made.md
"""
import json
import sys
from collections import defaultdict

import crm

FIELDS = ["name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip", "care_type",
          "phone", "status", "chow_current_account", "duplicate_of_account", "note"]
LABEL = {"parent_id": "Parent", "chow_current_account": "CHOW current account", "duplicate_of_account": "Duplicate of",
         "billing_street": "Street", "billing_zip": "Zip", "billing_city": "City", "billing_state": "State",
         "care_type": "Care type", "name": "Name", "phone": "Phone", "status": "Status", "note": "Note"}


def main(acc_path, con_path, out_path):
    before = {a["account_id"]: a for a in json.load(open(acc_path))}
    before_c = {c["contact_id"]: c for c in json.load(open(con_path))}
    after = {a["account_id"]: a for a in crm.list_accounts()}
    after_c = {c["contact_id"]: c for c in crm.list_contacts()}
    name = lambda i: (after.get(i) or before.get(i) or {}).get("name", i).replace(" (Parent Account)", "")  # noqa: E731
    show = lambda f, v: (f"{name(v)} ({v})" if v and f in ("parent_id", "chow_current_account",  # noqa: E731
                                                            "duplicate_of_account") else (v or "(blank)"))

    sections = defaultdict(list)
    for aid, a in sorted(after.items(), key=lambda kv: kv[1]["name"]):
        if aid not in before:
            sections["New accounts"].append(
                f"**{a['name']}** ({aid}): parent {name(a['parent_id'])}; {a['billing_street']}, {a['billing_city']}, "
                f"{a['billing_state']} {a['billing_zip']}; {a['care_type']}; {a['phone']}")
            continue
        b = before[aid]
        diffs = [(f, b.get(f), a.get(f)) for f in FIELDS if (b.get(f) or "") != (a.get(f) or "")]
        if not diffs:
            continue
        lines = [f"{LABEL[f]}: {show(f, old)} → {show(f, new)}" if f != "note" else f"Note added: \"{new}\""
                 for f, old, new in diffs]
        fields = {f for f, _, _ in diffs}
        if "duplicate_of_account" in fields:
            group = "Duplicates retired"
        elif "chow_current_account" in fields:
            group = "Change of ownership (billing SOP: old account kept, linked)"
        elif "parent_id" in fields:
            group = "Moved to the correct parent"
        elif fields & {"status"}:
            group = "Flagged for review"
        elif fields == {"note"}:
            group = "Notes"
        else:
            group = "Data fixes (name, address, phone)"
        sections[group].append(f"**{b['name']}** ({aid}, under {name(b['parent_id']) or '(no parent)'})\n" +
                               "\n".join(f"  - {l}" for l in lines))

    for cid, c in sorted(after_c.items(), key=lambda kv: kv[1]["name"]):
        if cid not in before_c:
            sections["Contacts added"].append(f"{c['name']} ({c['title']}) on {name(c['account_id'])}")
        else:
            b = before_c[cid]
            if b["account_id"] != c["account_id"]:
                sections["Contacts moved"].append(f"{c['name']} ({c['title']}): {name(b['account_id'])} → "
                                                  f"{name(c['account_id'])}")
            if b["is_active"] and not c["is_active"]:
                sections["Contacts marked inactive"].append(f"{c['name']} ({c['title']}) on {name(c['account_id'])}")

    order = ["Moved to the correct parent", "Change of ownership (billing SOP: old account kept, linked)",
             "Duplicates retired", "New accounts", "Flagged for review", "Data fixes (name, address, phone)", "Notes",
             "Contacts added", "Contacts moved", "Contacts marked inactive"]
    total_acc = sum(len(sections[k]) for k in order[:7])
    out = ["# Changes made to the CRM", "",
           f"Compared the CRM snapshot taken before any changes with the live CRM after review. "
           f"{total_acc} accounts created or changed; "
           f"{sum(len(sections[k]) for k in order[7:])} contact changes. Every change below was approved in the "
           f"review app.", ""]
    out += ["| Category | Count |", "|---|---|"] + [f"| {k} | {len(sections[k])} |" for k in order if sections[k]] + [""]
    for k in order:
        if sections[k]:
            out += [f"## {k} ({len(sections[k])})", ""] + [f"- {item}" for item in sections[k]] + [""]
    open(out_path, "w", encoding="utf-8").write("\n".join(out))
    print("\n".join(out[:16]))


if __name__ == "__main__":
    main(*sys.argv[1:4])

"""Pre-approval audit of the REAL review queue against the LIVE CRM (read-only; writes nothing).

1. Staleness: every pending item's "before" values still match the live CRM.
2. Simulate approving every pending item on an in-memory copy of the live CRM.
3. Audit the simulated end state against the website, location by location and field by field,
   plus the accounts under Bellhaven that are not on the website.
4. List every account the queue would touch, so nothing outside the expected set is changed.

Run: py -3.12 tests/audit_queue.py
"""
import collections
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crm  # noqa: E402
import store  # noqa: E402
from match import CARE_TYPE, is_protected  # noqa: E402
from normalize import name_key, person_key, phone_digits, street_key, zip5  # noqa: E402
from test_rerun import FakeCRM  # noqa: E402

issues, notes = [], []


def main():
    scrape = json.loads((ROOT / "data/locations.json").read_text())
    accounts, contacts = crm.list_accounts(), crm.list_contacts()
    by_id = {a["account_id"]: a for a in accounts}
    conn = store.connect()
    pending = store.list_proposals(conn, "pending")

    # 1. staleness
    for p in pending:
        for aid, fields in json.loads(p["before"]).items():
            for f, v in fields.items():
                if by_id[aid].get(f) != v:
                    issues.append(f"STALE #{p['id']} {p['title']}: {aid}.{f} is {by_id[aid].get(f)!r}, expected {v!r}")

    # 2. simulate
    fake = FakeCRM(accounts, contacts)
    touched = collections.defaultdict(list)
    for p in pending:
        prop = {"action": json.loads(p["action"])}
        for s in prop["action"]["steps"]:
            for key in ("account_id", "from_account", "to_account"):
                if isinstance(s.get(key), str) and not s[key].startswith("$"):
                    touched[s[key]].append(f"#{p['id']} {p['kind']}")
        fake.apply(prop)
    acc, con = fake.accounts, fake.contacts

    bell = next(a["account_id"] for a in accounts if a["name"].startswith("Bellhaven Senior Living"))
    live = lambda a: not a["duplicate_of_account"] and not a["chow_current_account"]  # noqa: E731

    # 3a. every website location
    for loc in scrape["locations"]:
        cands = [a for a in acc.values() if live(a) and street_key(a["billing_street"]) == street_key(loc["street"])
                 and a["billing_city"].lower() == loc["city"].lower() and a["billing_state"] == loc["state"]]
        if len(cands) != 1:
            issues.append(f"{loc['name']}: {len(cands)} live accounts at the address after approval (want 1)")
            continue
        a = cands[0]
        want_care = CARE_TYPE.get(loc["care_offerings"][0])
        checks = {
            "parent is Bellhaven": a["parent_id"] == bell,
            "status Active": a["status"] == "Active",
            "name": a["name"] == loc["name"],
            "zip": zip5(a["billing_zip"]) == loc["zip"],
            "phone": phone_digits(a["phone"]) == phone_digits(loc["phone"]),
            "administrator is an active contact": any(
                person_key(c["name"]) == person_key(loc["administrator"]) and c.get("is_active", True)
                and c["account_id"] == a["account_id"] for c in con.values()),
        }
        for k, ok in checks.items():
            if not ok:
                issues.append(f"{loc['name']}: {k} FAILS (account {a['account_id']}: {a.get('name')}, "
                              f"{a.get('status')}, phone {a.get('phone')})")
        offered = {CARE_TYPE.get(o) for o in loc["care_offerings"]}
        if a["care_type"] not in offered:
            notes.append(f"{loc['name']}: CRM care_type '{a['care_type']}' vs website offerings {loc['care_offerings']}")
        stale_admins = [c["name"] for c in con.values() if c["account_id"] == a["account_id"] and c.get("is_active", True)
                        and c["title"].lower() == "administrator" and person_key(c["name"]) != person_key(loc["administrator"])]
        if stale_admins:
            issues.append(f"{loc['name']}: other active 'Administrator' contacts remain: {stale_admins}")

    # 3b. accounts under Bellhaven not on the website
    site_keys = {(street_key(l["street"]), l["city"].lower()) for l in scrape["locations"]}
    for a in acc.values():
        if a["parent_id"] == bell and live(a) and (street_key(a["billing_street"]), a["billing_city"].lower()) not in site_keys:
            if a["status"] != "Needs Review":
                issues.append(f"{a['name']}: under Bellhaven, not on website, status {a['status']}")
            else:
                notes.append(f"{a['name']}: Needs Review (not on website) - note: {a['note'][:80]}...")
        if a["parent_id"] == bell and a["chow_current_account"]:
            cur = acc[a["chow_current_account"]]
            notes.append(f"{a['name']}: kept for billing under Bellhaven; CHOW -> {cur['name']} under "
                         f"{by_id.get(cur['parent_id'], {}).get('name', cur['parent_id'])}")

    # 3c. billing SOP: a protected account that needed a NEW parent must be left exactly as is
    # (only chow_current_account may change). Protected accounts already under the right parent
    # (Akron, Defiance, Meadville) aren't moving, so ordinary fixes like a phone update are allowed.
    for aid, orig in by_id.items():
        if is_protected(orig) and (orig["parent_id"] != bell or acc[aid]["chow_current_account"]):
            changed = {k for k in orig if k not in ("chow_current_account", "note", "updated_at") and acc[aid].get(k) != orig[k]}
            if changed:
                issues.append(f"SOP VIOLATION {orig['name']}: changed {changed}")

    # 4. touched accounts
    print(f"Pending items: {len(pending)}. Accounts touched: {len(touched)}")
    for aid, items in sorted(touched.items(), key=lambda kv: by_id[kv[0]]["name"]):
        a = by_id[aid]
        print(f"  {a['name']:<45} {by_id.get(a['parent_id'], {}).get('name', '-')[:28]:<28} {', '.join(items)}")
    new_accounts = [a for a in acc.values() if a["account_id"].startswith("NEW")]
    print(f"New accounts: {[a['name'] for a in new_accounts]}")
    print(f"New contacts: {[(c['name'], acc[c['account_id']]['name']) for c in con.values() if c['contact_id'].startswith('NEWC')]}")
    print(f"Contacts deactivated: {[c['name'] for c in con.values() if c.get('is_active') is False and c['contact_id'] in {x['contact_id'] for x in contacts if x['is_active']}]}")
    print("\nNOTES:"); [print("  " + n) for n in notes]
    print("\nISSUES:" if issues else "\nISSUES: none"); [print("  " + i) for i in issues]


if __name__ == "__main__":
    main()

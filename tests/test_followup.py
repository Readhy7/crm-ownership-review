"""Offline test of approve -> write -> instant follow-up, plus note appending.

Uses a fake CRM and a temporary review queue; the real CRM and data/review.db are untouched.
Run: py -3.12 tests/test_followup.py
"""
import copy
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import apply  # noqa: E402
import crm  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from match import Matcher  # noqa: E402


class FakeAPI:
    def __init__(self, accounts, contacts):
        self.a = {x["account_id"]: copy.deepcopy(x) for x in accounts}
        self.c = {x["contact_id"]: copy.deepcopy(x) for x in contacts}
        self.n = 0

    def install(self):
        crm.list_accounts = lambda **f: [copy.deepcopy(v) for v in self.a.values()]
        crm.list_contacts = lambda **f: [copy.deepcopy(v) for v in self.c.values()
                                         if not f.get("account_id") or v["account_id"] == f["account_id"]]
        crm.get_account = lambda i: copy.deepcopy(self.a[i])
        crm.update_account = lambda i, f: self.a[i].update(f) or self.a[i]
        crm.update_contact = lambda i, f: self.c[i].update(f) or self.c[i]

        def create_account(f):
            self.n += 1
            new = {"account_id": f"NEW{self.n:03d}", "lifetime_revenue": 0, "outstanding_ar": 0,
                   "chow_current_account": "", "duplicate_of_account": "", "note": "", **f}
            self.a[new["account_id"]] = new
            return new

        def create_contact(f):
            self.n += 1
            new = {"contact_id": f"NEWC{self.n:03d}", **f}
            self.c[new["contact_id"]] = new
            return new
        crm.create_account, crm.create_contact = create_account, create_contact


def main():
    accounts = json.loads((ROOT / "tests/fixtures/crm_accounts_before.json").read_text())
    contacts = json.loads((ROOT / "tests/fixtures/crm_contacts_before.json").read_text())
    scrape = json.loads((ROOT / "tests/fixtures/website_snapshot.json").read_text())

    # Give one duplicate an existing rep note, to prove notes are appended, not overwritten.
    erie_dup = next(a for a in accounts if a["name"] == "Harborview Shores of Erie")
    erie_dup["note"] = "Rep: spoke with front desk in March."

    api = FakeAPI(accounts, contacts)
    api.install()
    tmp = pathlib.Path(tempfile.mkdtemp())
    store.DB_PATH = tmp / "test.db"
    pipeline.DATA_DIR = tmp  # follow-ups read the latest website snapshot from here
    (tmp / "locations.json").write_text(json.dumps(scrape))
    conn = store.connect()

    ids, _ = store.add_proposals(conn, store.start_run(conn), Matcher(scrape, accounts, contacts).run())
    rows = {i: store.get(conn, i) for i in ids}
    cedar = next(a["account_id"] for a in accounts if a["name"].startswith("Cedar Trail Communities"))
    cedar_kids = {a["account_id"] for a in accounts if a["parent_id"] == cedar}
    related = [i for i, r in rows.items() if r["target_id"] in cedar_kids]

    followups = []
    for i in related:  # approve the Cedar Trail moves one by one, like a reviewer would
        assert apply.apply_proposal(conn, i) == "applied", (i, store.get(conn, i)["result"])
        new = pipeline.follow_up(conn, i)
        followups.append((i, [store.get(conn, n)["title"] for n in new]))

    erie_ids = [i for i, r in rows.items() if r["target_id"] == erie_dup["account_id"]]
    apply.apply_proposal(conn, erie_ids[0])

    triggered = [(i, t) for i, t in followups if t]
    # Ownership changes are what empty the parent; renames/phone updates don't matter to it.
    structural = [i for i in related if rows[i]["kind"] in ("reparent", "chow_new_account", "mark_duplicate")]
    last_structural = structural[-1]
    before_last = [t for i, t in followups[:related.index(last_structural)]]
    checks = {
        "no follow-up before the last Cedar Trail ownership change": all(not t for t in before_last),
        "that approval triggers exactly one follow-up: the Cedar Trail note":
            len(triggered) == 1 and triggered[0][0] == last_structural
            and len(triggered[0][1]) == 1 and "Cedar Trail" in triggered[0][1][0],
        "follow-up is tagged with the approval that caused it":
            conn.execute("SELECT origin FROM proposals WHERE kind='parent_note'").fetchone()[0] == last_structural,
        "existing note kept, ours appended below it":
            api.a[erie_dup["account_id"]]["note"].startswith("Rep: spoke with front desk in March.\n["),
    }
    print(f"Approved {len(related)} Cedar Trail items; follow-ups: {triggered}")
    print("Erie duplicate note now:", api.a[erie_dup["account_id"]]["note"].replace("\n", " / "))
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()

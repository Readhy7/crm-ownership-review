"""Re-run safety test, fully offline (no API calls).

1. Build proposals from the saved website snapshot and CRM pull.
2. Approve ALL of them against an in-memory copy of the CRM.
3. Run matching again on the updated copy: nothing new should be proposed.
4. Check the end state: SOP accounts untouched apart from the pointer, duplicates retired, etc.

Run: py -3.12 tests/test_rerun.py
"""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from match import Matcher  # noqa: E402


class FakeCRM:
    def __init__(self, accounts, contacts):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = {c["contact_id"]: copy.deepcopy(c) for c in contacts}
        self.n = 0

    def apply(self, proposal):
        ctx = {}
        res = lambda v: ctx[v[1:]] if isinstance(v, str) and v.startswith("$") else v  # noqa: E731
        for step in proposal["action"]["steps"]:
            t = step["type"]
            if t == "update_account":
                self.accounts[step["account_id"]].update({k: res(v) for k, v in step["fields"].items()})
            elif t == "create_account":
                self.n += 1
                new_id = f"NEW{self.n:03d}"
                self.accounts[new_id] = {"account_id": new_id, "lifetime_revenue": 0, "outstanding_ar": 0,
                                         "chow_current_account": "", "duplicate_of_account": "", "note": "",
                                         "parent_name": "", **step["fields"]}
                ctx["new_account_id"] = new_id
            elif t == "create_contact":
                self.n += 1
                cid = f"NEWC{self.n:03d}"
                self.contacts[cid] = {"contact_id": cid, **{k: res(v) for k, v in step["fields"].items()}}
            elif t == "update_contact":
                self.contacts[step["contact_id"]].update(step["fields"])
            elif t == "move_contacts":
                for c in self.contacts.values():
                    if c["account_id"] == step["from_account"]:
                        c["account_id"] = res(step["to_account"])
            else:
                raise AssertionError(f"unknown step {t}")


def main():
    scrape = json.loads((ROOT / "tests/fixtures/website_snapshot.json").read_text())
    accounts = json.loads((ROOT / "tests/fixtures/crm_accounts_before.json").read_text())
    contacts = json.loads((ROOT / "tests/fixtures/crm_contacts_before.json").read_text())

    first = Matcher(scrape, accounts, contacts).run()
    fake = FakeCRM(accounts, contacts)
    for p in first:
        fake.apply(p)

    # The 2nd run may only propose parent notes (they describe the CRM once the moves have landed).
    second_all = Matcher(scrape, list(fake.accounts.values()), list(fake.contacts.values())).run()
    notes = [p for p in second_all if p["kind"] == "parent_note"]
    print(f"Second run parent notes: {[p['title'] for p in notes]}")
    for p in notes:
        print("   ", p["action"]["steps"][0]["fields"]["note"])
        fake.apply(p)
    second = [p for p in second_all if p["kind"] != "parent_note"]
    second += Matcher(scrape, list(fake.accounts.values()), list(fake.contacts.values())).run()  # 3rd run
    acc = fake.accounts
    by_name = lambda n: [a for a in acc.values() if a["name"] == n]  # noqa: E731
    bell = next(a["account_id"] for a in acc.values() if a["name"].startswith("Bellhaven Senior Living"))
    original = {a["account_id"]: a for a in accounts}

    checks = {
        "second run proposes nothing": not second,
        "Tiffin old account: only chow_current_account changed": all(
            acc[a["account_id"]][k] == v for a in by_name("Bellhaven of Tiffin") if a["account_id"] in original
            for k, v in original[a["account_id"]].items() if k != "chow_current_account"),
        "Tiffin: one new account under Bellhaven": sum(
            a["parent_id"] == bell for a in by_name("Bellhaven of Tiffin")) == 1,
        "Lima re-parented directly": by_name("Bellhaven Crossings of Lima")[0]["parent_id"] == bell,
        "Sandusky old account points to Millstone": acc[next(a["account_id"] for a in accounts
                                                             if a["name"] == "Bellhaven of Sandusky")]
        ["chow_current_account"] == next(a["account_id"] for a in accounts if a["name"] == "Millstone Care of Sandusky"),
        "Kettering survivor renamed + under Bellhaven": any(
            a["name"] == "Bellhaven of Kettering" and a["parent_id"] == bell for a in acc.values()),
        "every website location has exactly one live Bellhaven account": all(
            sum(1 for a in acc.values() if a["parent_id"] == bell and a["name"] == loc["name"]
                and not a["duplicate_of_account"] and not a["chow_current_account"]) == 1
            for loc in scrape["locations"]),
        "no billing-protected account retired by the pipeline": not any(
            a["lifetime_revenue"] > 0 and a["outstanding_ar"] > 0 and a["status"] == "Inactive"
            and original.get(a["account_id"], {}).get("status") != "Inactive" for a in acc.values()),
        "Owosso contact moved to survivor": all(
            acc[c["account_id"]]["name"] == "Bellhaven of Owosso" and not acc[c["account_id"]]["duplicate_of_account"]
            for c in fake.contacts.values() if c["name"] == "Tricia Lindqvist"),
    }
    print(f"First run: {len(first)} proposals. Second run after approving all: {len(second)}.")
    for p in second:
        print("  unexpected:", p["title"])
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()

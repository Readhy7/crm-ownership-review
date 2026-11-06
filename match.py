"""Match website locations to CRM accounts, classify each one, and build review proposals.

Nothing here writes to the CRM. The output is a list of proposals for the review app.

Order of operations for each website location:
  1. Find candidates by ADDRESS (normalized street + city + state). The building is the stable
     identifier; names change with every rebrand. Fallback, only when the CRM address is
     unusable (PO box or blank): same city + very similar name. Never match on name alone
     across cities.
  2. If several accounts share the address, run the BILLING CHECK on every one of them FIRST,
     then pick one survivor. A billing-protected account (lifetime_revenue > 0 AND
     outstanding_ar > 0) is never marked duplicate/Inactive.
  3. Compare the survivor field by field (parent, name, address, zip, phone, administrator)
     and propose one review item per real difference. Cosmetic formatting is not proposed.
  4. Any parent change goes through the billing SOP: protected -> leave the old account as is,
     create a new account under the right parent, point chow_current_account at it;
     otherwise re-parent directly.
Then:
  5. Website locations with no CRM account -> propose creating one.
  6. CRM accounts under the operator that are not on the website -> look for evidence of a new
     owner at the same address; otherwise flag Needs Review (absence alone is weak evidence).
  7. Parent accounts left with no current facilities -> propose a note.

Classification per location (as named in the task): confident match, match needing a fix,
no CRM account yet; plus CRM accounts under the operator that are no longer on the website.
"""
import datetime as dt
from collections import defaultdict

from rapidfuzz import fuzz

from normalize import (is_po_box, name_key, person_key, phone_digits, place_key, street_key,
                       zip5)

OPERATOR_PARENT = "Bellhaven Senior Living"
# Acquired in full (About page: "welcomed the Harborview Care Group family of communities").
# Used to break ties between otherwise-equal duplicates, and to flag the absorbed parent.
ACQUIRED_IN_FULL = {"Harborview Care Group"}
MAX_NOT_ON_SITE = 5          # more than this many "missing" accounts in one run looks like a bad scrape
NAME_FALLBACK_SCORE = 90     # name similarity needed when the CRM address is unusable
LOOKALIKE_SCORE = 70         # similar-name accounts worth mentioning as evidence (not matched)

CARE_TYPE = {
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Independent Living": "Independent Living",
}

OWNERSHIP, DATA_FIX, CONTACT_INFO = 1, 2, 3


def money(x):
    return f"${x:,.0f}"


def is_protected(acct):
    """The billing SOP's test: revenue history AND an outstanding balance."""
    return acct["lifetime_revenue"] > 0 and acct["outstanding_ar"] > 0


def parent_label(name):
    return name.replace(" (Parent Account)", "")


class Matcher:
    def __init__(self, scrape_result, accounts, contacts, today=None):
        self.today = today or dt.date.today().isoformat()
        self.scrape = scrape_result
        self.locations = scrape_result["locations"]
        self.by_id = {a["account_id"]: a for a in accounts}
        parent_ids = {a["parent_id"] for a in accounts if a["parent_id"]}
        self.parents = {a["account_id"]: a for a in accounts
                        if a["account_id"] in parent_ids or "(Parent Account)" in a["name"]}
        self.op = next(a for a in self.parents.values()
                       if parent_label(a["name"]) == OPERATOR_PARENT)
        self.facilities = [a for a in accounts if a["account_id"] not in self.parents]
        # Historical records (already resolved as a duplicate or a change of ownership) are
        # never matched again. This is also what keeps re-runs quiet after approvals.
        self.live = [a for a in self.facilities
                     if not a["duplicate_of_account"] and not a["chow_current_account"]]
        self.contacts = defaultdict(list)
        for c in contacts:
            self.contacts[c["account_id"]].append(c)
        self.by_address = defaultdict(list)
        for a in self.live:
            self.by_address[self.addr_key(a["billing_street"], a["billing_city"], a["billing_state"])].append(a)
        self.proposals = []
        self.classification = []
        self.leaving = set()  # accounts proposed to leave their current parent (for parent notes)
        self.warnings = []

    # ---------- helpers ----------
    @staticmethod
    def addr_key(street, city, state):
        return (street_key(street),) + place_key(city, state)

    def pname(self, acct):
        p = self.by_id.get(acct["parent_id"])
        return parent_label(p["name"]) if p else "(no parent)"

    def admin_on(self, acct, loc):
        admin = person_key(loc["administrator"])
        return admin and any(person_key(c["name"]) == admin for c in self.contacts[acct["account_id"]])

    def describe(self, acct):
        return (f"CRM: {acct['name']} | {acct['billing_street']}, {acct['billing_city']}, "
                f"{acct['billing_state']} {acct['billing_zip']} | parent: {self.pname(acct)} | "
                f"status {acct['status']} | id {acct['account_id']}")

    @staticmethod
    def site_line(loc):
        return (f"Website: {loc['name']} | {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']} | "
                f"administrator {loc['administrator'] or 'n/a'} | {loc['url']}")

    def billing_line(self, acct):
        return (f"Billing: lifetime revenue {money(acct['lifetime_revenue'])}, "
                f"outstanding AR {money(acct['outstanding_ar'])}")

    def propose(self, *, facility, kind, priority, confidence, title, summary, steps,
                evidence, target_id=None, before=None):
        before = before or {}
        for step in steps:
            # Notes are appended, never overwritten: a rep's existing note is kept above ours.
            # The existing note is also recorded as a "before" value, so if someone edits it
            # between proposal and approval, the write is stopped as stale instead of clobbering it.
            if step["type"] == "update_account" and "note" in step["fields"]:
                existing = (self.by_id.get(step["account_id"], {}).get("note") or "").strip()
                if existing:
                    step["fields"]["note"] = f"{existing}\n{step['fields']['note']}"
                before.setdefault(step["account_id"], {})["note"] = self.by_id.get(step["account_id"], {}).get("note", "")
        self.proposals.append({
            "facility": facility, "kind": kind, "priority": priority, "confidence": confidence,
            "title": title, "summary": summary, "target_id": target_id,
            "action": {"steps": steps}, "before": before, "evidence": evidence,
        })

    def new_account_fields(self, loc, note):
        return {
            "name": loc["name"],
            "parent_id": self.op["account_id"],
            "billing_street": loc["street"],
            "billing_city": loc["city"],
            "billing_state": loc["state"],
            "billing_zip": loc["zip"],
            "care_type": CARE_TYPE.get((loc["care_offerings"] or [""])[0], ""),
            "phone": loc["phone"],
            "status": "Active",
            "note": note,
        }

    def admin_contact_step(self, loc, account_ref):
        if not loc["administrator"]:
            return []
        return [{"type": "create_contact", "fields": {
            "account_id": account_ref, "name": loc["administrator"], "title": "Administrator",
            "phone": loc["phone"], "email": "", "is_active": True}}]

    # ---------- step 1: candidates ----------
    def candidates(self, loc):
        exact = self.by_address.get(self.addr_key(loc["street"], loc["city"], loc["state"]), [])
        if exact:
            return exact, "address"
        same_place = [a for a in self.live
                      if place_key(a["billing_city"], a["billing_state"]) == place_key(loc["city"], loc["state"])]
        unusable = [a for a in same_place if is_po_box(a["billing_street"]) or not a["billing_street"].strip()]
        fallback = [a for a in unusable
                    if fuzz.token_set_ratio(name_key(a["name"]), name_key(loc["name"])) >= NAME_FALLBACK_SCORE
                    or self.admin_on(a, loc)]
        return fallback, "name+city (CRM address unusable)"

    def lookalikes(self, loc):
        out = []
        for a in self.live:
            score = fuzz.token_set_ratio(name_key(a["name"]), name_key(loc["name"]))
            same_place = place_key(a["billing_city"], a["billing_state"]) == place_key(loc["city"], loc["state"])
            if (same_place and score >= LOOKALIKE_SCORE) or name_key(a["name"]) == name_key(loc["name"]):
                out.append(f"Similar name, NOT matched (different address): {a['name']} | "
                           f"{a['billing_street']}, {a['billing_city']}, {a['billing_state']} | "
                           f"parent {self.pname(a)} | id {a['account_id']}")
        return out

    # ---------- step 2: duplicates ----------
    def survivor_rank(self, acct, loc):
        return (
            acct["parent_id"] == self.op["account_id"],
            acct["lifetime_revenue"] > 0,
            bool(self.admin_on(acct, loc)),
            phone_digits(acct["phone"]) == phone_digits(loc["phone"]),
            len(self.contacts[acct["account_id"]]),
            self.pname(acct) in ACQUIRED_IN_FULL,
            acct["status"] == "Active",
        )

    def resolve_duplicates(self, loc, group):
        ranked = sorted(group, key=lambda a: self.survivor_rank(a, loc), reverse=True)
        survivor, losers = ranked[0], ranked[1:]
        for loser in losers:
            ev = [self.site_line(loc), "Survivor → " + self.describe(survivor), "Duplicate → " + self.describe(loser),
                  "Same normalized address: " + " ".join(self.addr_key(loc["street"], loc["city"], loc["state"])),
                  self.billing_line(loser),
                  "Survivor chosen by: correct parent > billing history > website administrator is a contact "
                  "> phone matches website > number of contacts > fully acquired chain"]
            moved = [c["name"] for c in self.contacts[loser["account_id"]]]
            self.leaving.add(loser["account_id"])
            if is_protected(loser) and loser["parent_id"] != survivor["parent_id"]:
                # Billing needs this account preserved; the survivor is already the account under
                # the new owner, so the SOP pointer goes to it instead of creating another one.
                self.propose(
                    facility=loc["name"], kind="chow_pointer", priority=OWNERSHIP, confidence="high",
                    title=f"Change of ownership: point “{loser['name']}” to the current account",
                    summary=(f"“{loser['name']}” has billing history and an open balance, so it stays exactly as it is "
                             f"(billing SOP). Its chow_current_account is set to the current account "
                             f"“{survivor['name']}” ({survivor['account_id']})."),
                    steps=[{"type": "update_account", "account_id": loser["account_id"],
                            "fields": {"chow_current_account": survivor["account_id"]}}],
                    evidence=ev + ["Billing SOP: revenue > 0 AND AR > 0 → keep the old account; link it, don't retire it."],
                    target_id=loser["account_id"], before={loser["account_id"]: {"chow_current_account": ""}})
            elif is_protected(loser):
                self.propose(
                    facility=loc["name"], kind="flag_review", priority=OWNERSHIP, confidence="medium",
                    title=f"Possible duplicate with open billing: “{loser['name']}”",
                    summary=("Same address and same parent as another account, but it has billing history and an "
                             "open balance, so it is not retired automatically. Flagged for a person."),
                    steps=[{"type": "update_account", "account_id": loser["account_id"], "fields": {
                        "status": "Needs Review",
                        "note": f"[{self.today}] Possible duplicate of {survivor['account_id']} "
                                f"({survivor['name']}); not retired because of open billing."}}],
                    evidence=ev, target_id=loser["account_id"],
                    before={loser["account_id"]: {"status": loser["status"]}})
            else:
                steps = [{"type": "update_account", "account_id": loser["account_id"], "fields": {
                    "duplicate_of_account": survivor["account_id"], "status": "Inactive",
                    "note": f"[{self.today}] Duplicate of {survivor['account_id']} ({loc['name']}, "
                            f"{loc['street']}, {loc['city']}). Same facility; kept the record under the current owner."}}]
                if moved:
                    steps.append({"type": "move_contacts", "from_account": loser["account_id"],
                                  "to_account": survivor["account_id"]})
                self.propose(
                    facility=loc["name"], kind="mark_duplicate", priority=OWNERSHIP, confidence="high",
                    title=f"Mark “{loser['name']}” ({self.pname(loser)}) as a duplicate"
                          + (f" and move {len(moved)} contact(s)" if moved else ""),
                    summary=(f"Same facility as “{survivor['name']}”. Set duplicate_of_account → "
                             f"{survivor['account_id']}, status Inactive"
                             + (f"; move contacts to the surviving account: {', '.join(moved)}." if moved else ".")),
                    steps=steps, evidence=ev, target_id=loser["account_id"],
                    before={loser["account_id"]: {"duplicate_of_account": "", "status": loser["status"]}})
        return survivor

    # ---------- steps 3-4: compare the survivor ----------
    def compare(self, loc, acct, basis):
        start = len(self.proposals)
        aid = acct["account_id"]
        ev_base = [self.site_line(loc), self.describe(acct), f"Matched on: {basis}"]
        admin_match = self.admin_on(acct, loc)
        phone_match = phone_digits(acct["phone"]) == phone_digits(loc["phone"])
        ev_base.append("Website administrator is a contact on this account" if admin_match else
                       f"Website administrator ({loc['administrator']}) is not a contact on this account")
        ev_base.append("Phone matches website" if phone_match else
                       f"Phone differs: CRM {acct['phone']} vs website {loc['phone']}")
        conf = "high" if basis == "address" else "medium"

        # Parent first, because the billing SOP can mean the old account must not be touched at all.
        if acct["parent_id"] != self.op["account_id"]:
            self.leaving.add(aid)
            ev = ev_base + [self.billing_line(acct)]
            if self.pname(acct) in ACQUIRED_IN_FULL or "Cedar Trail" in self.pname(acct):
                ev.append("About page: Bellhaven welcomed Harborview Care Group (2025) and select "
                          "communities from Cedar Trail (2026)")
            if is_protected(acct):
                note = (f"[{self.today}] Created for change of ownership to {OPERATOR_PARENT}. Previous account "
                        f"{aid} ({acct['name']}, under {self.pname(acct)}) kept for billing.")
                self.propose(
                    facility=loc["name"], kind="chow_new_account", priority=OWNERSHIP, confidence=conf,
                    title=f"Change of ownership: new “{loc['name']}” under Bellhaven; keep old account for billing",
                    summary=(f"The website shows {loc['name']} is a Bellhaven community, but the CRM account sits "
                             f"under {self.pname(acct)}. It has revenue history ({money(acct['lifetime_revenue'])}) "
                             f"AND an open balance ({money(acct['outstanding_ar'])}), so per the billing SOP the old "
                             f"account is left exactly as it is. A new account is created under Bellhaven from the "
                             f"website data (plus its administrator as a contact), and the old account's "
                             f"chow_current_account is set to the new account."),
                    steps=[{"type": "create_account", "fields": self.new_account_fields(loc, note)},
                           *self.admin_contact_step(loc, "$new_account_id"),
                           {"type": "update_account", "account_id": aid,
                            "fields": {"chow_current_account": "$new_account_id"}}],
                    evidence=ev + ["Billing SOP: revenue > 0 AND AR > 0 → do NOT re-parent; create new + link"],
                    target_id=aid, before={aid: {"parent_id": acct["parent_id"], "chow_current_account": ""}})
                return "match needing a fix"  # old account stays untouched: no renames/phone fixes on it
            reason = ("no revenue history" if acct["lifetime_revenue"] <= 0 else
                      f"revenue {money(acct['lifetime_revenue'])} but no outstanding AR")
            self.propose(
                facility=loc["name"], kind="reparent", priority=OWNERSHIP, confidence=conf,
                title=f"Move “{acct['name']}” from {self.pname(acct)} to Bellhaven",
                summary=(f"The website lists this facility as a Bellhaven community; the CRM has it under "
                         f"{self.pname(acct)}. Billing SOP: {reason}, so the existing account is re-parented directly."),
                steps=[{"type": "update_account", "account_id": aid, "fields": {"parent_id": self.op["account_id"]}}],
                evidence=ev + [f"Billing SOP: {reason} → re-parent directly"],
                target_id=aid, before={aid: {"parent_id": acct["parent_id"]}})

        if name_key(acct["name"]) != name_key(loc["name"]):
            self.propose(
                facility=loc["name"], kind="rename", priority=DATA_FIX, confidence=conf,
                title=f"Rename “{acct['name']}” → “{loc['name']}”",
                summary="The CRM name doesn't match the operator's current name for this facility.",
                steps=[{"type": "update_account", "account_id": aid, "fields": {"name": loc["name"]}}],
                evidence=ev_base, target_id=aid, before={aid: {"name": acct["name"]}})

        if street_key(acct["billing_street"]) != street_key(loc["street"]):
            self.propose(
                facility=loc["name"], kind="fix_address", priority=DATA_FIX, confidence=conf,
                title=f"Fix street: “{acct['billing_street']}” → “{loc['street']}”",
                summary=("The CRM street is not the facility's physical address"
                         + (" (it's a PO box)." if is_po_box(acct["billing_street"]) else ".")),
                steps=[{"type": "update_account", "account_id": aid, "fields": {"billing_street": loc["street"]}}],
                evidence=ev_base + ["Every other account uses billing_street for the physical facility address, "
                                    "and matching depends on it, so the website's street address replaces it."],target_id=aid, before={aid: {"billing_street": acct["billing_street"]}})

        if zip5(acct["billing_zip"]) != zip5(loc["zip"]):
            self.propose(
                facility=loc["name"], kind="fix_zip", priority=DATA_FIX, confidence="medium",
                title=f"Fix zip: {acct['billing_zip']} → {loc['zip']}",
                summary="Street, city and state match the website; only the zip differs.",
                steps=[{"type": "update_account", "account_id": aid, "fields": {"billing_zip": loc["zip"]}}],
                evidence=ev_base, target_id=aid, before={aid: {"billing_zip": acct["billing_zip"]}})

        if not phone_match and loc["phone"]:
            self.propose(
                facility=loc["name"], kind="update_phone", priority=CONTACT_INFO, confidence="medium",
                title=f"Update phone: {acct['phone'] or '(blank)'} → {loc['phone']}",
                summary="The operator's website lists a different number; the CRM number looks stale.",
                steps=[{"type": "update_account", "account_id": aid, "fields": {"phone": loc["phone"]}}],
                evidence=ev_base, target_id=aid, before={aid: {"phone": acct["phone"]}})

        if loc["administrator"] and not admin_match:
            # The website lists one administrator per community, so an active contact titled
            # "Administrator" with a different name is stale (staff turnover). It is marked
            # inactive, never deleted; other roles (nursing, billing, admissions) are untouched.
            stale = [c for c in self.contacts[aid] if c["is_active"]
                     and c["title"].strip().lower() == "administrator"]
            steps = self.admin_contact_step(loc, aid) + [
                {"type": "update_contact", "contact_id": c["contact_id"], "fields": {"is_active": False}}
                for c in stale]
            self.propose(
                facility=loc["name"], kind="add_contact", priority=CONTACT_INFO, confidence="medium",
                title=f"Administrator is now {loc['administrator']}: add contact"
                      + (f", mark {', '.join(c['name'] for c in stale)} inactive" if stale else ""),
                summary=("The website names this administrator, who isn't a contact on the account."
                         + (f" The current 'Administrator' contact ({', '.join(c['name'] for c in stale)}) "
                            f"is marked inactive, not deleted." if stale else "")
                         + " Other contacts are left as they are."),
                steps=steps,
                evidence=ev_base + ["Current contacts: " + (", ".join(
                    f"{c['name']} ({c['title']})" for c in self.contacts[aid]) or "none")],
                target_id=aid, before={})

        return "confident match" if len(self.proposals) == start else "match needing a fix"

    # ---------- step 5: no CRM account ----------
    def propose_create(self, loc):
        look = self.lookalikes(loc)
        note = f"[{self.today}] Created from the operator website ({loc['url']})."
        if look:
            note += (" Not the same facility as similarly named account(s) "
                     + ", ".join(l.split("| id ")[-1] for l in look) + " (different address).")
        self.propose(
            facility=loc["name"], kind="create_account", priority=OWNERSHIP,
            confidence="medium" if look else "high",
            title=f"Create “{loc['name']}” under Bellhaven (+ administrator contact)",
            summary=(f"{loc['name']} ({loc['street']}, {loc['city']}, {loc['state']}) is on the website but no CRM "
                     f"account exists at that address." + (" Similar-name accounts exist elsewhere; see evidence "
                                                            "for why they are different facilities." if look else "")),
            steps=[{"type": "create_account", "fields": self.new_account_fields(loc, note)},
                   *self.admin_contact_step(loc, "$new_account_id")],
            evidence=[self.site_line(loc), "No live CRM account at this normalized address"] + look)

    # ---------- step 6: on the CRM under the operator, not on the website ----------
    def not_on_site(self, matched_ids):
        # Accounts already set to Needs Review are with a person; don't flag them again every day.
        missing = [a for a in self.live
                   if a["parent_id"] == self.op["account_id"] and a["account_id"] not in matched_ids
                   and a["status"] != "Needs Review"]
        if self.scrape.get("removal_guard_tripped") or len(missing) > MAX_NOT_ON_SITE:
            self.warnings.append(f"{len(missing)} operator accounts are missing from the website; that looks like a bad "
                                 f"scrape, so no removal proposals were made this run.")
            return
        removed_404 = {r["name"]: r for r in self.scrape.get("removed_since_last_run", [])}
        for acct in missing:
            aid = acct["account_id"]
            key = self.addr_key(acct["billing_street"], acct["billing_city"], acct["billing_state"])
            successors = [a for a in self.by_address.get(key, []) if a["account_id"] != aid
                          and a["parent_id"] != self.op["account_id"]]
            ev = [self.describe(acct), self.billing_line(acct),
                  f"Not listed on the operator website ({len(self.locations)} locations checked on {self.today})"]
            if acct["name"] in removed_404:
                ev.append(f"Its website page now returns 404 (last seen {removed_404[acct['name']]['last_seen']})")
            self.leaving.add(aid)
            self.classification.append((acct["name"], "CRM account under Bellhaven, no longer on the website"))
            if successors:
                succ = successors[0]
                ev.append("Same address now exists under another owner → " + self.describe(succ))
                if is_protected(acct):
                    self.propose(
                        facility=acct["name"], kind="chow_pointer", priority=OWNERSHIP, confidence="medium",
                        title=f"Sold to {self.pname(succ)}: point “{acct['name']}” to “{succ['name']}”",
                        summary=(f"No longer on Bellhaven's website, and the same address is in the CRM as "
                                 f"“{succ['name']}” under {self.pname(succ)}. The old account has revenue history AND "
                                 f"an open balance, so per the billing SOP it stays exactly as it is and its "
                                 f"chow_current_account points to the existing {self.pname(succ)} account (an account "
                                 f"under the new owner already exists, so none is created)."),
                        steps=[{"type": "update_account", "account_id": aid,
                                "fields": {"chow_current_account": succ["account_id"]}},
                               {"type": "update_account", "account_id": succ["account_id"], "fields": {
                                   "note": f"[{self.today}] Change of ownership from {OPERATOR_PARENT}: previous "
                                           f"account {aid} ({acct['name']}) kept for billing and points here."}}],
                        evidence=ev + ["Billing SOP: revenue > 0 AND AR > 0 → keep the old account; link it",
                                       "Deviation from the SOP's literal wording: no NEW account is created, because "
                                       "an account under the new owner already exists; creating one would only add "
                                       "a duplicate. The note goes on the new owner's account, since the old one "
                                       "must stay exactly as it is."],
                        target_id=aid, before={aid: {"chow_current_account": "", "parent_id": acct["parent_id"]}})
                else:
                    self.propose(
                        facility=acct["name"], kind="mark_duplicate", priority=OWNERSHIP, confidence="medium",
                        title=f"Sold to {self.pname(succ)}: mark “{acct['name']}” as a duplicate of “{succ['name']}”",
                        summary="No billing to preserve, and the facility already exists under its new owner.",
                        steps=[{"type": "update_account", "account_id": aid, "fields": {
                            "duplicate_of_account": succ["account_id"], "status": "Inactive",
                            "note": f"[{self.today}] No longer on Bellhaven's website; facility now under "
                                    f"{self.pname(succ)} as {succ['account_id']}."}},
                               {"type": "move_contacts", "from_account": aid, "to_account": succ["account_id"]}],
                        evidence=ev, target_id=aid,
                        before={aid: {"duplicate_of_account": "", "status": acct["status"]}})
            else:
                self.propose(
                    facility=acct["name"], kind="flag_review", priority=OWNERSHIP, confidence="medium",
                    title=f"Not on Bellhaven's website: flag “{acct['name']}” for review",
                    summary=("Under Bellhaven in the CRM but not on the website, and there's no evidence of a new owner "
                             "(sold? closed? website omission?). Status → Needs Review with a note. The parent is "
                             "left unchanged because removing it would claim the facility is independent."),
                    steps=[{"type": "update_account", "account_id": aid, "fields": {
                        "status": "Needs Review",
                        "note": f"[{self.today}] Not listed on {OPERATOR_PARENT}'s website. No new owner found in the "
                                f"CRM. Confirm whether it was sold or closed."}}],
                    evidence=ev + ["No other account at this address"], target_id=aid,
                    before={aid: {"status": acct["status"]}})

    # ---------- step 7: parents left empty ----------
    def parent_notes(self):
        """Describe what is ACTUALLY in the CRM, never a predicted future state.

        A parent gets a note only when every account still linked to it is history (a retired
        duplicate, or an old account kept for billing after a change of ownership). Until the
        related moves are approved and land in the CRM, nothing is proposed here; the next run
        picks it up.
        """
        for pid, parent in self.parents.items():
            if pid == self.op["account_id"] or "No current facilities" in (parent["note"] or ""):
                continue
            children = [a for a in self.facilities if a["parent_id"] == pid]
            live_ids = {a["account_id"] for a in self.live}
            if not children or any(a["account_id"] in live_ids for a in children):
                continue

            def why(a):
                if a["duplicate_of_account"]:
                    other = self.by_id.get(a["duplicate_of_account"], {}).get("name", a["duplicate_of_account"])
                    return f"{a['name']}: inactive duplicate of {other}"
                cur = self.by_id.get(a["chow_current_account"])
                where = f"{cur['name']} under {self.pname(cur)}" if cur else a["chow_current_account"]
                return (f"{a['name']} ({a['account_id']}): kept for billing after change of ownership; "
                        f"current account is {where} ({a['chow_current_account']})")

            label = parent_label(parent["name"])
            absorbed = label in ACQUIRED_IN_FULL
            remaining = [why(a) for a in children]
            fields = {"note": (f"[{self.today}] No current facilities under this parent. The accounts still linked "
                               f"here are history: " + "; ".join(remaining) + ". "
                               + ("Acquired in full by Bellhaven in 2025 (per Bellhaven's About page)."
                                  if absorbed else "Bellhaven took only select communities, so the company may "
                                                   "still operate facilities not in this CRM."))}
            if absorbed:
                fields["status"] = "Needs Review"
            self.propose(
                facility=f"{label} (parent account)", kind="parent_note", priority=OWNERSHIP, confidence="medium",
                title=f"Note on parent “{label}”: no current facilities" + (" (absorbed by Bellhaven)" if absorbed else ""),
                summary=("Every account still linked to this parent is history, so a rep opening it would otherwise "
                         "see 'Active' children that aren't current. "
                         + ("Bellhaven acquired it in full, so status → Needs Review to confirm the company no longer "
                            "exists separately." if absorbed else "Note only; status unchanged.")),
                steps=[{"type": "update_account", "account_id": pid, "fields": fields}],
                evidence=["Accounts linked to this parent: " + " | ".join(remaining)],
                target_id=pid, before={pid: {"status": parent["status"], "note": parent["note"]}})

    # ---------- run ----------
    def run(self):
        matched = set()
        for loc in self.locations:
            group, basis = self.candidates(loc)
            if not group:
                self.propose_create(loc)
                self.classification.append((loc["name"], "no CRM account yet"))
                continue
            for a in group:
                matched.add(a["account_id"])
            survivor = self.resolve_duplicates(loc, group) if len(group) > 1 else group[0]
            result = self.compare(loc, survivor, basis)
            if len(group) > 1:
                result = "match needing a fix"
            self.classification.append((loc["name"], result))
        self.not_on_site(matched)
        self.parent_notes()
        return self.proposals

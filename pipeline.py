"""Daily pipeline: scrape -> pull CRM -> match -> queue NEW proposals -> write a plain-English report.

Usage:
  py -3.12 pipeline.py              full run (scrapes the site, reads the CRM, updates the review queue)
  py -3.12 pipeline.py --dry-run    same, but doesn't touch the review queue; prints the report only

Never writes to the CRM. Writes happen only from the review app when a person approves.
Safe to re-run: proposals already in the queue (pending, approved or rejected) are not
re-proposed, and approved changes no longer show up as differences in the fresh CRM pull.
"""
import datetime as dt
import json
import sys
from collections import Counter

import crm
import scrape
import store
from config import DATA_DIR
from match import Matcher

REPORT_DIR = DATA_DIR / "reports"
KIND_LABEL = {
    "reparent": "Move to Bellhaven", "chow_new_account": "Change of ownership (billing SOP)",
    "chow_pointer": "Change of ownership pointer (billing SOP)", "mark_duplicate": "Duplicate",
    "create_account": "New account", "flag_review": "Flag for review", "parent_note": "Parent account note",
    "rename": "Rename", "fix_address": "Fix street", "fix_zip": "Fix zip",
    "update_phone": "Update phone", "add_contact": "Add contact",
}


def write_report(matcher, proposals, path):
    lines = [f"# Proposed CRM changes ({matcher.today})", "",
             f"{len(matcher.locations)} website locations checked against "
             f"{len(matcher.facilities)} CRM facility accounts.", ""]
    counts = Counter(c for _, c in matcher.classification)
    lines += ["## Classification", ""] + [f"- **{k}:** {v}" for k, v in counts.most_common()] + [""]
    for k in ("confident match", "match needing a fix", "no CRM account yet",
              "CRM account under Bellhaven, no longer on the website"):
        names = [n for n, c in matcher.classification if c == k]
        if names:
            lines.append(f"**{k.capitalize()}:** " + ", ".join(sorted(names)))
            lines.append("")
    if matcher.warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in matcher.warnings] + [""]
    lines += [f"## Every proposed change ({len(proposals)})", ""]
    by_kind = Counter(p["kind"] for p in proposals)
    lines += ["| Type | Count |", "|---|---|"] + [f"| {KIND_LABEL[k]} | {v} |" for k, v in by_kind.most_common()] + [""]
    current = None
    for p in sorted(proposals, key=lambda p: (p["facility"], p["priority"])):
        if p["facility"] != current:
            current = p["facility"]
            lines += [f"### {current}", ""]
        lines.append(f"- **{p['title']}** ({p['confidence']} confidence). {p['summary']}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(dry_run=False):
    started = dt.datetime.now()
    print("1/4 Scraping the website...")
    scrape_result = scrape.main()
    print("2/4 Pulling the CRM...")
    accounts, contacts = crm.list_accounts(), crm.list_contacts()
    print(f"    {len(accounts)} accounts, {len(contacts)} contacts")
    # Latest CRM pull, used by the review app to show account names next to ids.
    (DATA_DIR / "crm_latest.json").write_text(json.dumps({"accounts": accounts, "contacts": contacts}))
    print("3/4 Matching...")
    matcher = Matcher(scrape_result, accounts, contacts)
    proposals = matcher.run()
    for w in matcher.warnings:
        print("    WARNING:", w)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"proposed-changes-{matcher.today}.md"
    write_report(matcher, proposals, report)
    print(f"    {len(proposals)} differences found; report: {report}")

    if dry_run:
        print("4/4 Dry run: review queue not updated.")
        return
    print("4/4 Updating the review queue...")
    conn = store.connect()
    run_id = store.start_run(conn)
    new_ids, skipped = store.add_proposals(conn, run_id, proposals)
    added = len(new_ids)
    summary = {"locations": len(matcher.locations), "differences": len(proposals), "new_proposals": added,
               "already_known": skipped, "warnings": matcher.warnings,
               "seconds": round((dt.datetime.now() - started).total_seconds(), 1),
               "classification": dict(Counter(c for _, c in matcher.classification))}
    store.finish_run(conn, run_id, summary)
    print(f"    {added} new proposals queued, {skipped} already known (not re-proposed).")
    print(json.dumps(summary, indent=2))


def follow_up(conn, approved_id):
    """Re-check right after an approval so follow-ups appear while the reviewer has context.

    Uses the latest website snapshot (no re-scrape) and a fresh CRM pull. Anything new that
    the approval made true (e.g. a parent account now has no current facilities) is queued
    and tagged with the approval that triggered it. Already-known items are skipped as usual.
    """
    scrape_result = json.loads((DATA_DIR / "locations.json").read_text())
    accounts, contacts = crm.list_accounts(), crm.list_contacts()
    (DATA_DIR / "crm_latest.json").write_text(json.dumps({"accounts": accounts, "contacts": contacts}))
    proposals = Matcher(scrape_result, accounts, contacts).run()
    run_id = store.start_run(conn)
    new_ids, skipped = store.add_proposals(conn, run_id, proposals, origin=approved_id)
    store.finish_run(conn, run_id, {"follow_up_of": approved_id, "new_proposals": len(new_ids),
                                    "already_known": skipped, "differences": len(proposals)})
    return new_ids


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)

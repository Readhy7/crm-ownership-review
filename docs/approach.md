# Approach, decisions and edge cases

## 1. Sources and what I trust

- **The website is the source for what Bellhaven says it operates today.** It's the operator's marketing site, though, not a legal record. It lags (one new community is linked only from the homepage), so I trust **positive claims** ("we run a community at this address") more than **absences** ("it's not on our site").
- **Detail pages over directory cards.** The cards show one care offering; detail pages list them all.
- **The About page explains the history:** Bellhaven acquired *all* of Harborview Care Group (2025) and *select* communities from Cedar Trail (2026). That's why Harborview and Cedar Trail accounts sit at Bellhaven addresses, and why Cedar Trail is still treated as an active company.

## 2. Matching

**The address is the identifier.** A building doesn't move; names change with every rebrand and acquisition. Each field plays a different role:

| Field | Role |
|---|---|
| Street + city + state | Identify the facility (normalized) |
| Name, parent, street, zip | Compare and fix |
| Administrator (vs. CRM contacts), phone | Supporting evidence. A mismatch lowers confidence but never rules out a match (staff and numbers change) |
| Care type | Distinguishes two facilities on one campus |

- **Normalization (for comparison only):** lowercase, no punctuation, and USPS standard abbreviations ("1120 West Main Street" = "1120 W Main St", "Wilmington Pk" = "Wilmington Pike"). Only long forms are shortened, so "St" (Saint) in "45 St Lawrence Dr" stays consistent. Cosmetic formatting differences are never written back to the CRM.
- **Street, city *and* state must all match.** City names repeat across states (Portsmouth OH/VA/NH). A zip difference within the same state is still a match, with the zip fix proposed separately.
- **Fallback, only when the CRM address is unusable** (a PO box or blank): same city plus a very similar name. Medium confidence.
- **Never match on name alone across cities.** Similar names elsewhere are listed as evidence on new-account proposals, but never merged.

## 3. Classification

| Class | Count (first run) | Action |
|---|---|---|
| Confident match | 6 | Nothing to change |
| Match needing a fix | 25 | One review item per real difference (parent, name, street, zip, phone, administrator) |
| No CRM account yet | 4 | Create the account under Bellhaven, with the administrator as a contact |
| Under Bellhaven in the CRM, not on the website | 3 | Look for a new owner at the same address; otherwise Needs Review plus a note |

## 4. The billing SOP

Applied **only when an account would move to a different parent**, and only when **both** conditions hold (lifetime revenue > 0 **and** outstanding AR > 0).

| Account | Revenue / AR | Result |
|---|---|---|
| Bellhaven of Tiffin (under Cedar Trail) | $84,000 / $12,400 | Old account untouched; new account under Bellhaven; `chow_current_account` → new |
| Bellhaven of Marietta (under Cedar Trail) | $51,250 / $3,800 | Same |
| Bellhaven Crossings of Lima (under Harborview) | $47,000 / **$0** | Re-parented directly (no balance, so the SOP doesn't apply) |
| Bellhaven Meadows of Findlay (no parent) | $22,000 / **$0** | Re-parented directly |
| Akron, Defiance, Meadville | revenue + AR | Already under Bellhaven: not moving, so the SOP doesn't apply (ordinary fixes like a phone update are fine) |
| **Bellhaven of Sandusky** | $130,000 / $5,200 | Sold to Millstone (see below) |

**Sandusky, a deliberate deviation from the SOP's literal wording.** Sandusky is no longer on Bellhaven's site, and the same address already exists in the CRM as *Millstone Care of Sandusky* under Millstone Health Partners. The SOP says to *create* a new account under the correct parent, but one already exists, and creating another would only add a duplicate. So the old account stays exactly as it is, and its `chow_current_account` points to the existing Millstone account. The explanation note goes on the Millstone account, since the old one must not be touched.

**Order of operations:** the billing check runs **before** duplicate handling. An account with revenue and an open balance is never marked as a duplicate or Inactive. If it would lose a duplicate contest under a different parent, it gets the CHOW pointer instead; under the same parent, it's flagged for a person.

## 5. Duplicates

Five addresses had several accounts, mostly leftovers from the acquisitions (Bellhaven, Harborview and Cedar Trail copies of the same building). The survivor is chosen by: correct parent > billing history > the website's administrator is a contact > phone matches > number of contacts > the fully acquired chain (Harborview). Losers get `duplicate_of_account` = survivor, status Inactive and a note, and **their contacts move to the survivor**.

- **Kettering** had three empty accounts, none under Bellhaven. I kept the Harborview record, since Harborview was acquired in full and is the most likely predecessor, then re-parented and renamed it.
- **Owosso** had two identically named accounts. The survivor is the one whose phone and administrator match the website. The losing record's Admissions Director was moved over.

## 6. Accounts no longer on the website

Absence is weak evidence (sold? closed? missing from the website?), so these are never deactivated on absence alone:
- **Sandusky:** evidence of a new owner (above).
- **Coldwater and Alliance:** no other account at those addresses. Status Needs Review with a note; the parent is left unchanged, because removing it would claim they're independent.

## 7. Contact information

- **Phones:** 17 of the 30 matched facilities had a different phone from the website, clustered on stale records (renamed, acquired, duplicates). These are proposed as separate, low-priority items.
- **Administrators:** if the website's administrator isn't a contact on the account, they're added. A different active contact titled "Administrator" is marked inactive, never deleted. Other roles are left alone.

## 8. Parent accounts and follow-ups

Retired duplicates and billing-preserved accounts stay linked to their old parents. Cedar Trail, for example, still lists old Tiffin and Marietta as Active (per the SOP). So once every account under a parent is history, the system proposes a **note on the parent** naming each leftover account and why it's there.

Two design iterations:
1. **Proposing the note up front** described a future state the CRM didn't show yet. Replaced.
2. **Proposing it on the next daily run** describes reality, but a reviewer handling many parents would have lost the context by then. Replaced.

**Final:** after every approval, the app re-checks the CRM (about 1 second, no re-scrape) and queues any follow-up the approval just made true. It's tagged "Follow-up from your approval of #N", and the confirmation message links to it even if a search filter would hide it.

In review I **approved** the Harborview note (acquired in full, so status → Needs Review) and **rejected** the Cedar Trail note (still an active company; the leftover accounts already explain themselves through their CHOW and duplicate fields).

## 9. Daily-run safety

| Risk | Handling |
|---|---|
| A community announced only on the homepage disappears from every link when the notice comes down | Previously seen pages are re-checked directly; "removed" only on a real 404, otherwise "unlisted" |
| Partial page load looks like mass closures | The directory count must match the site's stated count; more than 3 removals in one run pauses removal proposals |
| Transient errors | 3 attempts with backoff, then the run fails loudly |
| Re-proposing decided items | Fingerprint (change type + account + new values, excluding dated notes) |
| Records left "wrong" on purpose (billing-preserved, retired duplicates) | Excluded from matching via `chow_current_account` / `duplicate_of_account`; without this, day 2 would propose a second new Tiffin |
| CRM changed between proposal and approval | Stale check against the proposal's recorded "before" values |
| A crash midway through a multi-step write | Progress saved per step; a retry resumes, never repeating a create |

## 10. Traps in the data (summary)

- **Website:** a community missing from the directory (Findlay); cards hiding care offerings; "Amberly Manor" has no Bellhaven branding; "Bellhaven Willow Creek" is in Portage, MI; Carlisle (PA) vs. New Carlisle (OH); phone area codes that don't match the states.
- **CRM:**
  - Outdated names: Riverbend Manor, Sunny Acres, Chesterton Senior Commons, Cedar Trail of Zanesville.
  - Small name variants: "of" vs "at", Centre vs Center.
  - Address formatting variants; a PO box as the street (Ashtabula); transposed zip digits (Portsmouth).
  - The wrong parent after acquisitions; a facility with no parent (Findlay).
  - Five duplicate groups.
  - Same-name, different-facility look-alikes: Amberly Manor in Colorado; Union Square Senior Living at a different address with a different administrator.
  - Bellhaven accounts no longer on the site.

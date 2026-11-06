"""Write an APPROVED proposal to the CRM. This is the only module that writes.

A proposal's action is a list of steps:
  update_account   {"account_id", "fields"}
  create_account   {"fields"}                -> remembered as $new_account_id
  create_contact   {"fields"}                (account_id may be "$new_account_id")
  update_contact   {"contact_id", "fields"}
  move_contacts    {"from_account", "to_account"}
  deactivate_contacts {"account_id"}

Safety:
  - Stale check: before writing, every account the proposal read is fetched again. If any
    field it relied on has changed since the proposal was made, nothing is written and the
    proposal is marked "stale" so it can be re-evaluated on the next run.
  - Progress is saved after each step, so a retry after a failure never repeats a step
    that already succeeded (e.g. never creates the same account twice).
"""
import json

import crm
import store


class StaleProposal(Exception):
    pass


def _resolve(value, ctx):
    if isinstance(value, str) and value.startswith("$"):
        return ctx[value[1:]]
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    return value


def check_not_stale(before):
    for account_id, fields in before.items():
        current = crm.get_account(account_id)
        changed = {f: (v, current.get(f)) for f, v in fields.items() if current.get(f) != v}
        if changed:
            detail = "; ".join(f"{f}: was {old!r}, now {new!r}" for f, (old, new) in changed.items())
            raise StaleProposal(f"Account {account_id} changed since this was proposed ({detail})")


def apply_proposal(conn, proposal_id):
    p = store.get(conn, proposal_id)
    if p["status"] not in ("pending", "failed"):
        return p["status"]
    action = json.loads(p["action"])
    progress = json.loads(p["result"]) if p["result"] else {"done": []}
    ctx = progress.setdefault("ctx", {})

    try:
        if not progress["done"]:
            check_not_stale(json.loads(p["before"]))
        for i, step in enumerate(action["steps"]):
            if i in progress["done"]:
                continue
            kind = step["type"]
            if kind == "update_account":
                out = crm.update_account(step["account_id"], _resolve(step["fields"], ctx))
            elif kind == "create_account":
                out = crm.create_account(_resolve(step["fields"], ctx))
                ctx["new_account_id"] = out["account_id"]
            elif kind == "create_contact":
                out = crm.create_contact(_resolve(step["fields"], ctx))
            elif kind == "update_contact":
                out = crm.update_contact(step["contact_id"], step["fields"])
            elif kind == "move_contacts":
                moved = []
                for c in crm.list_contacts(account_id=step["from_account"]):
                    crm.update_contact(c["contact_id"], {"account_id": _resolve(step["to_account"], ctx)})
                    moved.append(c["name"])
                out = {"moved": moved}
            elif kind == "deactivate_contacts":
                done = []
                for c in crm.list_contacts(account_id=step["account_id"]):
                    if c["is_active"]:
                        crm.update_contact(c["contact_id"], {"is_active": False})
                        done.append(c["name"])
                out = {"deactivated": done}
            else:
                raise ValueError(f"Unknown step type {kind}")
            progress["done"].append(i)
            progress.setdefault("outputs", {})[str(i)] = out
            store.save_partial_result(conn, proposal_id, progress)
    except StaleProposal as e:
        progress["error"] = str(e)
        store.set_status(conn, proposal_id, "stale", result=progress)
        return "stale"
    except Exception as e:  # report the failure in the app; nothing is retried automatically
        progress["error"] = str(e)
        store.set_status(conn, proposal_id, "failed", result=progress)
        return "failed"

    store.set_status(conn, proposal_id, "applied", result=progress)
    return "applied"

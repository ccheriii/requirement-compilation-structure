"""Deterministic extraction of machine-readable contracts embedded in requirement text.

Benchmark requirements annotate interface elements as ``"NAME"(role=ROLE)`` and list
scenario data as ``Seed prerequisites ...: - label: {json}``. Black-box acceptance tests
locate elements with exactly these role/name pairs and rely on exactly these records, so
the compiler parses them instead of leaving them to model interpretation.
"""

import json
import re

ROLE_PATTERN = re.compile(r'"([^"\n]{1,200})"\s*\(role=([a-z]+)[^)\n]{0,60}\)')
TEMPLATE_PATTERN = re.compile(r"\{[^{}]+\}")
SEED_HEADER = re.compile(r"seed prerequisites", re.IGNORECASE)
SEED_LINE = re.compile(r"^\s*-\s*([^:\n{}]{1,120}):\s*(\{.*\})\s*$")
CLOCK_PATTERN = re.compile(r"clock[^0-9\n]{0,40}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)", re.IGNORECASE)

# Roles whose elements repeat once per record; their names are usually record values.
RECORD_ROLES = {"listitem", "row", "article", "option", "cell", "gridcell"}


def _contract(name, role):
    name = " ".join(name.split())
    template = bool(TEMPLATE_PATTERN.search(name))
    contract = {"name": name, "role": role}
    if template:
        contract["template"] = True
        # Placeholders stand for record values (e.g. "{item name}" or "More options {title}").
        pieces = TEMPLATE_PATTERN.split(name)
        contract["pattern"] = "^" + ".+".join(re.escape(piece) for piece in pieces) + "$"
    return contract


def role_contracts(text):
    """Return unique {name, role[, template, pattern]} contracts in order of appearance."""
    found, seen = [], set()
    for name, role in ROLE_PATTERN.findall(text or ""):
        contract = _contract(name, role)
        key = (contract["name"], contract["role"])
        if key not in seen:
            seen.add(key)
            found.append(contract)
    return found


def requirement_text(tree, rid):
    """A requirement's own description plus every Given/When/Then of its own steps."""
    parts = [tree.requirements[rid].get("description") or ""]
    for sid in tree.own_steps[rid]:
        step = tree.steps[sid]
        parts.extend(str(step.get(key) or "") for key in ("Given", "When", "Then"))
    return "\n".join(parts)


def requirement_contracts(tree, rid):
    """Role contracts declared by one requirement (description and its own scenario steps)."""
    return role_contracts(requirement_text(tree, rid))


def all_contracts(tree):
    """Map each leaf requirement to its role contracts (leaves without contracts omitted)."""
    result = {}
    for rid in tree.leaves:
        contracts = requirement_contracts(tree, rid)
        if contracts:
            result[rid] = contracts
    return result


def seed_records(text):
    """Parse '- label: {json}' lines that follow a 'Seed prerequisites' header."""
    records, active = [], False
    for line in (text or "").splitlines():
        if SEED_HEADER.search(line):
            active = True
            continue
        if not active:
            continue
        match = SEED_LINE.match(line)
        if not match:
            if line.strip() and not line.lstrip().startswith("-"):
                active = False
            continue
        try:
            data = json.loads(match.group(2))
        except ValueError:
            continue
        records.append({"label": " ".join(match.group(1).split()), "data": data})
    return records


def scenario_seeds(tree):
    """All distinct seed prerequisite records, with the scenarios/requirements that need them."""
    merged = {}
    for rid in tree.requirements:
        for sid in tree.own_steps[rid]:
            step = tree.steps[sid]
            for record in seed_records(str(step.get("Given") or "")):
                key = record["label"] + "\n" + json.dumps(record["data"], sort_keys=True, ensure_ascii=False)
                entry = merged.setdefault(key, {**record, "requirements": [], "scenarios": []})
                if rid not in entry["requirements"]:
                    entry["requirements"].append(rid)
                scenario = step["scenario"]["id"]
                if scenario not in entry["scenarios"]:
                    entry["scenarios"].append(scenario)
    return list(merged.values())


def shared_clock(tree):
    """The ISO timestamp of the declared application/evaluator clock, if any."""
    texts = [requirement.get("description") or "" for requirement in tree.requirements.values()]
    texts.extend(str(step.get("Given") or "") for step in tree.steps.values())
    for text in texts:
        match = CLOCK_PATTERN.search(text)
        if match:
            value = match.group(1)
            return value if value.endswith("Z") else value + "Z"
    return None


SEED_IDENTITY_KEYS = {"name", "title", "email", "username", "nickname", "label", "code", "sku", "login",
    "account", "mobile", "phone"}


def seed_identity_values(data, limit=200):
    """Identity-like string values (names, titles, emails...) nested anywhere in a seed record."""
    values = []

    def visit(value, key=None):
        if len(values) >= limit:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in SEED_IDENTITY_KEYS and 2 < len(value) <= 200:
            values.append(value)

    visit(data)
    return list(dict.fromkeys(values))


CONTRACT_RULES = """ROLE CONTRACTS: requirement.role_contracts lists every "NAME"(role=ROLE) pair declared by the
requirement text. Black-box acceptance tests locate these elements with
getByRole(ROLE, {name: NAME, exact: true}), so each contract is an exact interface obligation:
- Render an element whose computed ARIA role is exactly ROLE and whose accessible name is exactly NAME.
  Where native semantics differ, set the role explicitly. A labelled <input type="password"> already counts
  as role=textbox. A named section declared role=region is <section aria-label="NAME">; dialog -> role="dialog" aria-label="NAME"; status ->
  role="status" aria-label="NAME" containing that message text (text alone gives a status NO name); tab -> role="tab"; menuitem -> role="menuitem";
  toggle buttons that report pressed state use aria-pressed).
  Other roles and how they get their exact name: navigation -> <nav aria-label="NAME">; searchbox ->
  <input type="search"> with a <label>/aria-label NAME; combobox -> labelled <select> (or role="combobox");
  spinbutton -> labelled <input type="number">; radio/checkbox -> labelled <input type="radio|checkbox">;
  group -> <fieldset> with <legend>NAME</legend> or role="group" aria-label="NAME"; timer -> role="timer"
  aria-label="NAME" (content alone gives no name); table -> <table aria-label="NAME"> or <caption>NAME</caption>;
  record containers -> set aria-label="NAME" on the element itself: <tr aria-label="NAME"> for row,
  <li aria-label="NAME"> for listitem, <article aria-label="NAME"> for article. A name computed from content
  would concatenate every cell/child text and fail exact matching; controls stay inside the container.
  link -> <a href> (a <button> is NOT a link); button -> <button> (an <a href> is NOT a button).
- An action the requirements name WITHOUT a declared role (Create ..., New ..., Edit, Save ..., Delete,
  Cancel, Confirm ...) is a <button type="button">, even when it opens another page: graders use
  getByRole('button', {name}). Navigate in its click handler (design prop.navigates_to); never render it
  as <a>. Only "(role=link)" contracts and plain navigation are links.
- template contracts contain {placeholders} standing for record values: "{item name}"(role=button) means one
  element per record named exactly by that record's value; "More options {title}" means that literal prefix
  followed by the record title. Match the contract's pattern exactly.
- Keep names unique per role on a page (strict-mode locators) and never add decorative elements reusing them.
- The external grader gives each whole scenario 10 seconds. Show results immediately after an action:
  no artificial delays, debounce longer than ~300 ms, polling, or animations that hide the result; tests
  never use waitForTimeout or networkidle and wait only for the visible outcome.
- A record listed in a list or named region (a page, draft, book, item, order...) that the requirements let
  the user open or resume is rendered as a link or button whose accessible name is exactly the record's title,
  even when no role is declared for that entry. Never show an openable listed record as plain text only;
  keep icons/metadata outside that accessible name (aria-hidden or a sibling element).
"""

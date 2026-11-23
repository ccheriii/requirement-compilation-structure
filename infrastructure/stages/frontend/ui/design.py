"""Canonical UI/data records and incremental design updates."""

from copy import deepcopy
import re



INSTRUCTIONS = """Design the UI elements and frontend data declarations for the current requirement.
Use requirement scenarios and visual references; reuse supplied UI elements where appropriate.
This stage declares the interface only. Return no code, tests, API definitions, triggers,
production initial values, example runtime objects or display fixtures.

OUTPUT CONTRACT
Return one JSON object with four sibling keys: elements, pages, requirement_ui, step_ui.
elements maps ELEMENT IDs to {type, parent, mode, data, prop}. A page root has type
"page" and parent=null. Other elements reference a parent ELEMENT ID. Use ordinary types:
container, form, input, textarea, checkbox, select, radiogroup, button, text, heading, link,
list, item, image, alert or table. mode is an array containing user_read and/or user_write, or [].
A single choice among a few fixed options is type "radiogroup" with prop.label = the group
label and prop.options = the option captions; this is the default for single choices. Use
"select" (prop.label, prop.options) only when the requirement or reference shows a dropdown,
when a default must be picked from a long list, or when there are many options.

data is an array of variable declarations: {var, dtype, constraints}.
var is the frontend variable name; dtype is string, integer, number, boolean, array,
object or null. constraints describes requirement-based rules and any array/object shape.
Declare all dynamic data consumed by the element, including visibility dependencies.
Repeat the same declaration consistently when several elements share a variable.
Do not produce a separate bindings registry or initial/source fields.
Fixed headings, labels, hints and button captions belong in prop, not dynamic data.
prop contains static text, label, placeholder, input_type, options, style, href, alt,
decorative or heading level. Optional prop.visible is a boolean or {var:VARIABLE_NAME, equals:VALUE}.
A status dependency controls visibility without requiring its raw value as display text.
constraints contain ONLY rules stated in the requirement (or array/object shape). Do not
invent length, pattern or character rules from reference hint text or placeholders.

ACCESSIBLE SEMANTICS (generated browser tests locate controls by role and exact name)
prop.input_type is one of text, email, tel, url, password (default text). Every field whose
label, requirement or reference indicates a password or secret, including confirmation
fields, MUST be input_type "password". Date and time fields (e.g. a start date or a date of
birth) are input_type "text" with prop.placeholder "YYYY-MM-DD"; never use date,
datetime-local, time, number or search, which change how tests locate and type into the field.
Every input, textarea, select, radiogroup, checkbox, button and link sets prop.label to its
exact accessible name: the visible label or caption text without required-asterisks,
trailing colons or icons. Within one page, no two simultaneously visible elements of the
same role (link, button, text field, dropdown, checkbox, radio group) share a label; if
content repeats a functional control's name, reuse that one functional element.
Error and feedback messages are type "alert" with a prop.visible condition; at most one
alert is visible at a time. Static hints and notes are type "text", never "alert".
A displayed dynamic value (e.g. the current user's name or a record title) is its own
"text" element whose rendered text is exactly that value, with no prefix such as
"Welcome, " in the same element. It is rendered once per page and removed, not blanked,
when its visibility condition is false.

Treat explicit UI names and roles in the original requirement as interface contracts.
Preserve required labels, action names, region names and placeholders verbatim, including
language, spelling and case. Do not paraphrase, add object-type prefixes, translate or
concatenate aliases: a required "Name" field must not become "Shelf name". Record the
wording in the existing element.prop; do not create a separate naming registry.
Explicit requirement wording takes precedence over visual-reference examples. Only choose
wording freely when it is unspecified or alternatives are explicitly allowed. Business
record names and user-entered values remain dynamic; example data is not fixed UI wording.

pages maps PAGE IDs to {name, route, root, images}. route is a URL path, root is an
ELEMENT ID and images lists supplied image IDs. Preserve explicit image.page hints.
Do not add fixtures or sample objects. Reference images describe appearance, not actual
loaded records. Requirement-defined defaults remain requirement evidence for implementation;
do not turn them into a separate initialization model in this design.
DECORATIVE REFERENCE CONTENT: reference-image items that no requirement step or
description makes functional (brand marks, utility menus, navigation menus, carousels,
unrelated search or booking widgets, promotional panels, footers) are decoration. Declare
them only as type text, image or container with prop.decorative=true, or omit them. Never
declare decoration as link, button, input, textarea, select, radiogroup, checkbox or alert,
never give it href or data, and never reuse a functional control's name in it. When a
required control also appears in the reference header or menu, declare it once as the
functional element in place of the decorative copy.
ROLE CONTRACTS: requirement.role_contracts lists the "NAME"(role=ROLE) pairs the requirement
declares; acceptance tests locate them with getByRole(ROLE, {name: NAME, exact: true}). For each
non-template contract declare (or reuse) exactly one functional element whose prop.label is NAME
verbatim and whose role is ROLE. The type implies the role (button, link, input/textarea=textbox,
checkbox, select=combobox, radiogroup); for any other role set prop.role to ROLE on a fitting type:
region/navigation/group/article -> container, dialog -> container shown conditionally, status ->
text shown after the action, tab/menuitem -> button, heading -> heading. A template contract such as
"{item name}"(role=button) is one element per record inside a list/item whose name is the record's
value; declare it once in the repeated item with prop.label set to the template text.

REFERENCE HEADINGS: requirement.reference_headings lists page titles the reference images show
(for example a create form titled "Create New Book"). Graders check such a title to confirm the
flow opened, so declare a heading element with prop.text exactly that title on that page. When one
page serves several flows with different references (create and edit forms), give each flow its
own title heading shown with prop.visible for that flow; never reuse one fixed title for all.
requirement_ui lists element IDs used by the current requirement. step_ui maps every
current scenario step ID to element IDs, using [] when no UI is needed.

UPDATES AND REUSE
Return complete records for new or changed elements/pages, preserving unrelated behavior.
Unchanged records may be omitted from their maps; include all four root keys even when empty.
Reuse canonical variable names and element IDs. Query relevant elements/pages when needed.
mode declares how the user interacts with the element; implementation chooses framework
bindings and routing. No fixed host synchronizes inputs or interprets this design. Additional
interaction behavior belongs to step 09; real data loading and updates belong to step 10.
Preserve visible labels in their original language. UI has no DB field-mapping model.
"""

OUTPUT_TEMPLATE = {"elements": {}, "pages": {}, "requirement_ui": [], "step_ui": {}}


class UISemanticError(ValueError):
    """Report clear-cut accessible-semantics violations in an otherwise readable proposal."""
    def __init__(self, issues):
        super().__init__("UI design violates accessible-semantics conventions: " + "; ".join(issues))
        self.feedback = {"issues": list(issues),
            "repair": "Return the complete corrected proposal with all four root keys. Fix only the listed "
                "elements: password/secret fields use prop.input_type \"password\"; date and time fields use "
                "input_type \"text\" with placeholder \"YYYY-MM-DD\"; a functional name appears once per page "
                "(reuse one element, mark reference decoration as type text with prop.decorative=true, or give "
                "alternatives a prop.visible condition)."}


ROLE_BY_TYPE = {"textarea": "textbox", "select": "combobox", "radiogroup": "radiogroup",
    "checkbox": "checkbox", "button": "button", "link": "link"}
REPEATED_TYPES = {"list", "item", "table"}


def accessible_role(node: dict) -> str | None:
    """Map a design element to the ARIA role tests use; "password" marks a label-located password field."""
    if not isinstance(node, dict):
        return None
    kind = node.get("type")
    prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
    explicit = prop.get("role")
    if isinstance(explicit, str) and explicit.strip():
        # A requirement-declared ARIA role overrides the type's implicit role.
        return explicit.strip().lower()
    if kind == "input":
        prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
        input_type = str(prop.get("input_type") or "text").strip().lower()
        return {"password": "password", "checkbox": "checkbox", "number": "spinbutton",
            "search": "searchbox", "radio": None, "hidden": None, "file": None}.get(input_type, "textbox")
    return ROLE_BY_TYPE.get(kind)


def accessible_name(node: dict) -> str | None:
    """Return the declared exact accessible name, or None when no usable string is declared."""
    if not isinstance(node, dict):
        return None
    prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
    candidates = [prop.get("label")]
    if node.get("type") in ("link", "button"):
        candidates.append(prop.get("text"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def option_names(options) -> list[str]:
    """Return unique option captions from strings or {label} objects; [] when the list is not static text."""
    if not isinstance(options, list):
        return []
    names = []
    for option in options:
        if isinstance(option, dict):
            option = option.get("label", option.get("text"))
        if not isinstance(option, str) or not option.strip():
            return []
        names.append(" ".join(option.split()))
    return [name for name in dict.fromkeys(names) if names.count(name) == 1]


def semantic_scope(design: dict, uid: str) -> str | None:
    """Return "decorative" or "repeated" when the element or an ancestor excludes it from unique-name rules."""
    visited, current = set(), uid
    while current is not None and current not in visited and current in design["elements"]:
        visited.add(current)
        node = design["elements"][current]
        prop = node.get("prop") if isinstance(node, dict) and isinstance(node.get("prop"), dict) else {}
        if prop.get("decorative"):
            return "decorative"
        if current != uid and isinstance(node, dict) and node.get("type") in REPEATED_TYPES:
            return "repeated"
        current = node.get("parent") if isinstance(node, dict) else None
    return None


class UIResponseError(ValueError):
    """Attach the concrete output layout after a model record cannot be consumed."""
    def __init__(self, error, proposal):
        super().__init__(f"Cannot read UI/data response: {error}")
        self.feedback = {"response_template": OUTPUT_TEMPLATE,
            "missing_sections": [key for key in OUTPUT_TEMPLATE if not isinstance(proposal, dict) or key not in proposal],
            "repair": "Write the missing variable and element records in the FINAL JSON, not just in reasoning. Return elements, pages, requirement_ui and step_ui as four sibling root keys. Declare variables inside element.data. A new page needs its root, controls and actual scenario associations. Do not repeat the rejected pages-only answer or merely add empty maps for a new page."}


def empty_design() -> dict:
    """Keep UI elements, pages and requirement associations."""
    return {"pages": {}, "elements": {}, "requirements": {}, "steps": {}}


def page_elements(design: dict, pid: str) -> dict:
    """Return the IDs in one page's UI subtree, including its root."""
    root = design["pages"][pid]["root"]
    selected = {root}
    while True:
        expanded = selected | {uid for uid, node in design["elements"].items() if node.get("parent") in selected}
        if expanded == selected:
            return {uid: deepcopy(node) for uid, node in design["elements"].items() if uid in selected}
        selected = expanded


def data_symbols(elements: dict) -> dict:
    """Derive a variable lookup from element declarations without storing another registry."""
    symbols = {}
    for element in elements.values():
        for declaration in element.get("data", []):
            symbols[declaration["var"]] = deepcopy(declaration)
    return symbols


def page_context(design: dict, pid: str) -> dict:
    """Return a page and its UI subtree, including inline variable declarations."""
    return {"id": pid, **deepcopy(design["pages"][pid]), "elements": page_elements(design, pid)}


def owners(design: dict, pid: str) -> list[str]:
    """Return requirements that directly reference elements on this page."""
    ids = set(page_elements(design, pid))
    return [rid for rid, bound in design["requirements"].items() if ids.intersection(bound)]


def visible(design: dict, uid: str, values: dict) -> bool:
    """Evaluate the supported static/variable-equality visibility rule using the supplied display values."""
    visited = set()
    while uid is not None and uid not in visited and uid in design["elements"]:
        visited.add(uid)
        node = design["elements"][uid]
        rule = node.get("prop", {}).get("visible", True)
        if rule is False or (isinstance(rule, dict) and values.get(rule.get("var")) != rule.get("equals")):
            return False
        uid = node.get("parent")
    return True


def normalize_design(design, tree):
    """Fill optional page metadata; accept model decisions without design gates."""
    for pid, page in design["pages"].items():
        page.setdefault("name", pid)
        page.setdefault("images", [])
    for node in design["elements"].values():
        node.setdefault("mode", [])
        node.setdefault("data", [])
        node.setdefault("prop", {})
    for kind, expected in (("requirements", tree.requirements), ("steps", tree.steps)):
        for identity in expected:
            design[kind].setdefault(identity, [])
    action_links_as_buttons(design, tree)
    return design


def persistent_navigation(design, tree) -> list[str]:
    """Give every page the home page's requirement-declared navigation links; return added ids.

    Requirements declare global navigation such as "Books"(role=link) and graders click it
    from whatever page a flow ends on. Designs tend to make the shared header functional only
    on the home page and decorative (hidden) elsewhere, so `go('Books')` from a book page finds
    nothing. Each home-page link whose name is a declared (role=link) contract is copied, with
    its href and visibility condition, onto every other page that has no link of that name."""
    from infrastructure.stages.requirements.contracts import requirement_text, role_contracts
    texts = "\n".join(requirement_text(tree, rid) for rid in tree.requirements)
    declared = {c["name"] for c in role_contracts(texts) if c["role"] == "link" and not c.get("template")}
    home = next((pid for pid, page in design["pages"].items() if page.get("route") == "/"), None)
    if home is None or not declared:
        return []

    def links(pid):
        found = {}
        for eid, node in page_elements(design, pid).items():
            prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
            name = accessible_name(node)
            if node.get("type") == "link" and name and not prop.get("decorative"):
                found.setdefault(name, (eid, node))
        return found

    navigation = {name: item for name, item in links(home).items() if name in declared}
    added = []
    for pid, page in design["pages"].items():
        if pid == home:
            continue
        present = links(pid)
        for name, (source, node) in navigation.items():
            if name in present:
                continue
            slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "link"
            eid = f"{page['root']}-nav-{slug}"
            design["elements"][eid] = {"type": "link", "parent": page["root"], "mode": [], "data": [],
                "prop": {**deepcopy(node.get("prop") or {}), "persistent_navigation": True}}
            for rid, uids in design["requirements"].items():
                if source in uids and eid not in uids:
                    uids.append(eid)
            added.append(eid)
    return added


def action_links_as_buttons(design, tree) -> list[str]:
    """Turn links for requirement-named actions into buttons; return the converted element ids.

    Graders locate a control the requirements name without a declared role (for example
    "click Edit", "Create New Book opens ...", "then Save Book") with getByRole('button').
    Designs often model such actions as links because they open another page, which makes
    every such step time out. A link keeps its type only when the requirements declare that
    name as (role=link), it is a record template, or the requirement text never names it
    (plain navigation such as "Back to home"). Its destination moves to prop.navigates_to."""
    from infrastructure.stages.requirements.contracts import requirement_text, role_contracts
    texts = "\n".join(requirement_text(tree, rid) for rid in tree.requirements)
    declared = {}
    for contract in role_contracts(texts):
        declared.setdefault(contract["name"], set()).add(contract["role"])
    converted = []
    for eid, node in design["elements"].items():
        if not isinstance(node, dict) or node.get("type") != "link":
            continue
        prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
        name = accessible_name(node)
        if not name or "{" in name or prop.get("role") or "link" in declared.get(name, set()):
            continue
        if not re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", texts):
            continue
        node["type"] = "button"
        if isinstance(prop.get("href"), str):
            prop["navigates_to"] = prop.pop("href")
        node["prop"] = prop
        converted.append(eid)
    return converted


def apply_proposal(design, proposal, rid, tree):
    """Replace supplied JSON records without retrieval, immutable-identity or UI-policy gates."""
    updated = deepcopy(design)
    for kind in ("pages", "elements"):
        updated[kind].update(deepcopy(proposal[kind]))
    updated["requirements"][rid] = proposal["requirement_ui"]
    updated["steps"].update(deepcopy(proposal["step_ui"]))
    normalize_design(updated, tree)
    return updated

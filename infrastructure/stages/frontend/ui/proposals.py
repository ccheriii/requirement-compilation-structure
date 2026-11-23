"""UI design examples with variable declarations inside elements."""

import json


SESSION_STATUS = {"var": "session.status", "dtype": "string",
    "constraints": ["signed_in after a successful sign-in; otherwise empty"]}
FORM_ERROR = {"var": "entry.error", "dtype": "string",
    "constraints": ["Empty until a submission is rejected; name_taken when the requirement's duplicate-name rule fails"]}

EXAMPLE = {
    "elements": {
        "UI-home": {"type": "page", "parent": None, "mode": [], "data": [], "prop": {}},
        "UI-home-brand": {"type": "text", "parent": "UI-home", "mode": [], "data": [],
            "prop": {"text": "Brand name", "decorative": True}},
        "UI-home-user": {"type": "text", "parent": "UI-home", "mode": ["user_read"],
            "data": [SESSION_STATUS, {"var": "session.displayName", "dtype": "string", "constraints": []}],
            "prop": {"visible": {"var": "session.status", "equals": "signed_in"}}},
        "UI-home-new": {"type": "button", "parent": "UI-home", "mode": [], "data": [],
            "prop": {"label": "New entry", "navigates_to": "/entries/new"}},
        "UI-search": {"type": "input", "parent": "UI-home", "mode": ["user_write"],
            "data": [{"var": "search.query", "dtype": "string", "constraints": []}],
            "prop": {"label": "Search"}},
        "UI-results": {"type": "list", "parent": "UI-home", "mode": ["user_read"],
            "data": [{"var": "search.results", "dtype": "array",
                "constraints": ["Entries contain id and title strings and match the submitted query"]}], "prop": {}},
        "UI-entry": {"type": "page", "parent": None, "mode": [], "data": [], "prop": {}},
        "UI-entry-form": {"type": "form", "parent": "UI-entry", "mode": [], "data": [], "prop": {}},
        "UI-entry-name": {"type": "input", "parent": "UI-entry-form", "mode": ["user_write"],
            "data": [{"var": "entry.name", "dtype": "string", "constraints": ["Required"]}],
            "prop": {"label": "Name"}},
        "UI-entry-secret": {"type": "input", "parent": "UI-entry-form", "mode": ["user_write"],
            "data": [{"var": "entry.secret", "dtype": "string", "constraints": ["Required"]}],
            "prop": {"label": "Password", "input_type": "password"}},
        "UI-entry-start": {"type": "input", "parent": "UI-entry-form", "mode": ["user_write"],
            "data": [{"var": "entry.startDate", "dtype": "string", "constraints": ["ISO date YYYY-MM-DD"]}],
            "prop": {"label": "Start date", "input_type": "text", "placeholder": "YYYY-MM-DD"}},
        "UI-entry-size": {"type": "radiogroup", "parent": "UI-entry-form", "mode": ["user_write"],
            "data": [{"var": "entry.size", "dtype": "string", "constraints": ["One of Small, Medium, Large"]}],
            "prop": {"label": "Size", "options": ["Small", "Medium", "Large"]}},
        "UI-entry-region": {"type": "select", "parent": "UI-entry-form", "mode": ["user_write"],
            "data": [{"var": "entry.region", "dtype": "string", "constraints": ["One of the listed regions; defaults to North"]}],
            "prop": {"label": "Region", "options": ["North", "South", "East", "West"]}},
        "UI-entry-hint": {"type": "text", "parent": "UI-entry-form", "mode": [], "data": [],
            "prop": {"text": "All fields are required."}},
        "UI-entry-error": {"type": "alert", "parent": "UI-entry-form", "mode": ["user_read"],
            "data": [FORM_ERROR],
            "prop": {"text": "This name is already in use.", "visible": {"var": "entry.error", "equals": "name_taken"}}},
        "UI-entry-submit": {"type": "button", "parent": "UI-entry-form", "mode": [], "data": [],
            "prop": {"label": "Save"}},
        "UI-entry-back": {"type": "link", "parent": "UI-entry", "mode": [], "data": [],
            "prop": {"label": "Back to home", "href": "/"}},
        "UI-entry-footer": {"type": "text", "parent": "UI-entry", "mode": [], "data": [],
            "prop": {"text": "Help | Contact", "decorative": True}},
    },
    "pages": {"home": {"name": "Home", "route": "/", "root": "UI-home", "images": []},
        "entry-new": {"name": "New entry", "route": "/entries/new", "root": "UI-entry", "images": []}},
    "requirement_ui": ["UI-home", "UI-home-user", "UI-home-new", "UI-search", "UI-results", "UI-entry",
        "UI-entry-form", "UI-entry-name", "UI-entry-secret", "UI-entry-start", "UI-entry-size",
        "UI-entry-region", "UI-entry-error", "UI-entry-submit", "UI-entry-back"],
    "step_ui": {"STEP-search": ["UI-search", "UI-results"],
        "STEP-open-form": ["UI-home-new", "UI-entry"],
        "STEP-fill-form": ["UI-entry-name", "UI-entry-secret", "UI-entry-start", "UI-entry-size", "UI-entry-region"],
        "STEP-submit": ["UI-entry-submit"],
        "STEP-rejected": ["UI-entry-error"],
        "STEP-signed-in": ["UI-home-user"]},
}
REUSE_EXAMPLE = {"elements": {}, "pages": {}, "requirement_ui": ["UI-search"],
    "step_ui": {"STEP-reuse": ["UI-search"]}}
INSTRUCTIONS = (
    "COMPLETE UI DECLARATION EXAMPLE (neutral placeholder names; use the requirement's own wording):\n"
    + json.dumps(EXAMPLE, ensure_ascii=False, indent=2)
    + "\nThe example shows: a password field, a date field as text with a YYYY-MM-DD placeholder, a radiogroup"
    " for a few fixed choices, a select for a dropdown with a default, an alert shown only under its error"
    " condition, a dynamic value alone in its own text element, and reference decoration as decorative text."
    " An action the requirement names without a declared role (\"New entry\" here; e.g. Create ..., New ...,"
    " Edit, Save ..., Delete, Cancel) is a button even when it opens another page: graders locate it with"
    " getByRole('button'). Put its destination in prop.navigates_to. Use type link (with href) only for"
    " controls the requirement declares as (role=link) and for plain navigation such as \"Back to home\"."
    + "\nREUSE WITHOUT CHANGES:\n" + json.dumps(REUSE_EXAMPLE, indent=2)
    + "\nReturn all four sections. Variables belong in element.data; do not add runtime values."
)

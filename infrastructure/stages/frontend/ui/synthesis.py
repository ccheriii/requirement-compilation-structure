"""Step 07 orchestration: retrieve pages, propose UI/data, parse, and publish.

All leaf requirements are designed before step 08 implements any UI. Reuse is
page-first: retrieval returns the corresponding UI/data subtree, not disconnected
components. Prompts and design records live in ui/design.py."""

import json
import re

from infrastructure.stages.frontend.ui.design import option_names, INSTRUCTIONS, apply_proposal, empty_design, page_elements, normalize_design, page_context, UIResponseError, data_symbols
from infrastructure.stages.frontend.ui.design import UISemanticError, accessible_name, accessible_role, semantic_scope, visible
from infrastructure.stages.frontend.ui.design import persistent_navigation
from infrastructure.stages.frontend.ui.proposals import INSTRUCTIONS as EXAMPLE_INSTRUCTIONS
from infrastructure.shared.rag.pages import PageRetriever
from infrastructure.utils.files import save_json
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.tasks import TaskFailures
from infrastructure.shared.agent.discovery import ContextIndex


def bind_requirement_image_pages(design, requirement, rid):
    """Bind image-backed pages to the current requirement when the page hint is unambiguous."""
    owned = list(design["requirements"].get(rid, []))
    images = requirement.get("images", {})
    image_records = images.values() if isinstance(images, dict) else images
    for image in image_records:
        if not isinstance(image, dict):
            continue
        image_id, page_hint = image.get("id"), image.get("page")
        # The requirement's explicit page hint is authoritative. The model may
        # omit the redundant page.images entry when creating a new page.
        if page_hint in design["pages"]:
            page = design["pages"][page_hint]
            page.setdefault("images", [])
            if image_id not in page["images"]:
                page["images"].append(image_id)
            candidates = [page_hint]
        else:
            candidates = [pid for pid, page in design["pages"].items() if image_id in page.get("images", [])]
        if len(candidates) != 1:
            continue
        root = design["pages"][candidates[0]]["root"]
        if root in design["elements"]:
            if root not in owned:
                owned.append(root)
    design["requirements"][rid] = owned


ROLE_INPUT_TYPES = {"date", "datetime-local", "time", "month", "week", "number", "search"}
SEMANTIC_REJECTIONS = 3


# Contracts on per-record roles are satisfied by rendered data rows, not by declared elements.
CONTRACT_EXEMPT_ROLES = {"listitem", "row", "article", "option", "cell", "gridcell", "radio"}


def contract_issues(design: dict, contracts) -> list[str]:
    """Non-template role contracts of the current requirement that no design element provides."""
    present = set()
    for node in design["elements"].values():
        role, name = accessible_role(node), accessible_name(node)
        if role and name:
            present.add(("textbox" if role == "password" else role, name))
        prop = node.get("prop") if isinstance(node, dict) and isinstance(node.get("prop"), dict) else {}
        if node.get("type") == "radiogroup" if isinstance(node, dict) else False:
            for option in option_names(prop.get("options")):
                present.add(("radio", option))
    missing = []
    for contract in contracts or []:
        if contract.get("template") or contract["role"] in CONTRACT_EXEMPT_ROLES:
            continue
        if (contract["role"], contract["name"]) not in present:
            missing.append(f'{contract["name"]!r} (role={contract["role"]})')
    if not missing:
        return []
    return ["requirement.role_contracts not provided by any element (set prop.label to the exact NAME and "
        "the role via type or prop.role): " + ", ".join(missing)]


def heading_issues(design: dict, headings) -> list[str]:
    """Reference page titles (requirement.reference_headings) that no heading element shows."""
    present = set()
    for node in design["elements"].values():
        if isinstance(node, dict) and node.get("type") == "heading":
            prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
            for key in ("text", "label"):
                if isinstance(prop.get(key), str):
                    present.add(" ".join(prop[key].split()))
    missing = [item for item in headings or [] if item["heading"] not in present]
    if not missing:
        return []
    return ["requirement.reference_headings not shown by any heading element: " + "; ".join(
        f'{item["image"]} (page {item["page"]}) is titled "{item["heading"]}"' for item in missing)
        + ". Declare a heading element with prop.text exactly that title on that page. When one page serves"
        " several flows (e.g. create and edit), give each flow its own title heading with prop.visible, "
        "never one fixed title from another flow."]


def semantic_issues(design: dict, proposal: dict) -> list[str]:
    """Return clear-cut accessible-semantics violations introduced or touched by this proposal.

    Deliberately lenient: only password masking, role-changing input types and
    unconditionally visible duplicate role/name pairs are reported."""
    changed = set(proposal.get("elements") or {}) if isinstance(proposal, dict) else set()
    issues = []
    for uid in sorted(changed):
        node = design["elements"].get(uid)
        if not isinstance(node, dict) or node.get("type") != "input":
            continue
        prop = node.get("prop") if isinstance(node.get("prop"), dict) else {}
        input_type = str(prop.get("input_type") or "text").strip().lower()
        label = prop.get("label")
        if isinstance(label, str) and re.search(r"password", label, re.IGNORECASE) and input_type != "password":
            issues.append(f"{uid}: field labelled {label!r} must set prop.input_type \"password\"")
        if input_type in ROLE_INPUT_TYPES:
            issues.append(f"{uid}: prop.input_type {input_type!r} is not allowed (tests locate and type into fields as plain textboxes); use \"text\""
                + (" with placeholder \"YYYY-MM-DD\"" if input_type in {"date", "datetime-local", "time", "month", "week"} else ""))
    pages = set(proposal.get("pages") or {}) if isinstance(proposal, dict) else set()
    for pid, page in design["pages"].items():
        if pid in pages or (isinstance(page, dict) and changed & set(page_elements(design, pid))):
            groups = {}
            for uid, node in page_elements(design, pid).items():
                role, name = accessible_role(node), accessible_name(node)
                if role is None or name is None or semantic_scope(design, uid) or not visible(design, uid, {}):
                    continue
                groups.setdefault((role, name), []).append(uid)
            for (role, name), ids in groups.items():
                if len(ids) > 1 and changed.intersection(ids):
                    issues.append(f"page {pid}: {', '.join(ids)} are all unconditionally visible with role {role} "
                        f"and name {name!r}; keep one functional element or mark reference copies decorative")
    return issues


def synthesize_ui(arc_dir, tree, images, contexts, model, console, top_k=3, max_attempts=10):
    """Design UI/data for all leaf requirements using page-first retrieval.

    Show existing/retrieved pages, ask for an incremental proposal, and feed
    response-consumption errors back for repair. Save accepted ui_design.json
    updates; implementation and browser tests belong to step 08."""
    design, retriever = empty_design(), PageRetriever()
    for rid in tree.requirements:
        if tree.children[rid]:
            design["requirements"][rid] = []
            for sid in tree.own_steps[rid]:
                design["steps"][sid] = []
            console.show(f"{rid} | Skip non-leaf UI/data analysis", "Only leaf requirements are analyzed. Organizational parents do not generate or inherit UI nodes.")
    failures = TaskFailures(arc_dir, "07_design_ui", console)
    save_json(arc_dir / "ui_design.json", design)
    for number, rid in enumerate(tree.leaves, 1):
        with failures.task(rid):
            requirement = contexts[rid]
            label = f"UI/data design {number}/{len(tree.leaves)} {rid}"
            console.show(label + " | 1. Requirement and steps", json.dumps(requirement, ensure_ascii=False, indent=2))
            console.show(label + " | 2. All Existing Pages", json.dumps(design["pages"], ensure_ascii=False, indent=2))
            retrieved = retriever.retrieve(requirement, design, top_k)
            console.show(label + " | 3. Retrieved page subtrees (RAG)", json.dumps(retrieved, ensure_ascii=False, indent=2))
            request = {
                "requirement": requirement, "retrieved_pages": retrieved["pages"],
                "all_pages": {pid: {key: page[key] for key in ("name", "route", "root", "images")}
                    for pid, page in design["pages"].items() if pid in retrieved["pages"]},
            }
            rejections = {"semantic": 0}

            def accept(proposal):
                try:
                    updated = apply_proposal(design, proposal, rid, tree)
                except (KeyError, TypeError, AttributeError) as error:
                    raise UIResponseError(error, proposal) from error
                bind_requirement_image_pages(updated, requirement, rid)
                issues = (semantic_issues(updated, proposal) + contract_issues(updated, requirement.get("role_contracts"))
                    + heading_issues(updated, requirement.get("reference_headings")))
                if issues and rejections["semantic"] < SEMANTIC_REJECTIONS:
                    rejections["semantic"] += 1
                    raise UISemanticError(issues)
                if issues:
                    # Stay lenient: after bounded repair attempts accept and let step 08 tests report it.
                    console.show(label + " | Accepted with semantic warnings", "\n".join(issues))
                for pid in proposal["pages"]:
                    console.show(label + f" | Page context {pid}", json.dumps(page_context(updated, pid), ensure_ascii=False, indent=2))
                return updated

            updated, proposal = propose_json(model, INSTRUCTIONS + "\n\n" + EXAMPLE_INSTRUCTIONS,
                request, accept, console, label, max_attempts, context_index=ContextIndex(lambda: {"page": design["pages"], "ui_element": design["elements"], "ui_data": data_symbols(design["elements"])}))
            console.show(label + " | 4. Incremental UI/data changes", json.dumps(proposal, ensure_ascii=False, indent=2))
            design = updated
            save_json(arc_dir / "ui_design.json", design)
            console.artifact("ui_design", design, label + " | Accepted UI")
    failures.finish()
    normalize_design(design, tree)
    added = persistent_navigation(design, tree)
    if added:
        console.show("UI/data design | Persistent navigation",
            "Declared navigation links copied onto every page that lacked them: " + ", ".join(added))
    save_json(arc_dir / "ui_design.json", design)
    console.show("UI/data design complete for ALL requirements", f"Saved {arc_dir / 'ui_design.json'}\nRun main.py --stage 08 for existence tests and UI implementation. Interactions belong to step 09.")
    return design

"""Frontend inputs retain image/page evidence alongside normalized requirements."""

import json
import re

from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.requirements.contracts import requirement_contracts, requirement_text
from infrastructure.utils.files import read_artifact

REFERENCE = re.compile(r"\\ref\[image\]\(([^)]+)\)")
VISUAL_HEADING = re.compile(r'^\[(main|form)\]\s*heading,\s*label\s*"([^"]+)"', re.IGNORECASE)


def reference_headings(images, requirement_texts):
    """Page titles that reference images show and the requirements also name.

    One title per image: the first heading in its main/form region (its page title).
    Only titles the requirement text names are kept, so sample data shown as a title
    (a record's name) never becomes a fixed heading. Graders check these titles to
    confirm a flow opened (for example a create form titled "Create New Book")."""
    found = []
    for iid, image in images.items():
        analysis = image.get("analysis") if isinstance(image.get("analysis"), dict) else {}
        for entry in analysis.get("elements") or []:
            match = VISUAL_HEADING.match(entry) if isinstance(entry, str) else None
            if not match:
                continue
            title = " ".join(match.group(2).split())
            if title and re.search(r"(?<![\w])" + re.escape(title) + r"(?![\w])", requirement_texts):
                found.append({"image": iid, "page": image.get("page"), "heading": title})
            break
    return found


def load_ui_design(arc_dir, tree):
    from infrastructure.stages.frontend.ui.design import normalize_design
    return normalize_design(read_artifact(arc_dir, "ui_design.json"), tree)


def load_ui_context(arc_dir, input_dir):
    """Load only requirement and image evidence; presentation does not depend on backend artifacts."""
    raw = read_artifact(arc_dir, "requirements.json")
    tree = RequirementTree(raw)
    # Visual evidence is optional; a failed or skipped analysis leaves no usable file.
    analysis_path = arc_dir / "visual_analysis.json"
    analysis = read_artifact(arc_dir, "visual_analysis.json") if analysis_path.is_file() else {}
    if not isinstance(analysis, dict):
        analysis = {}
    images, nodes = {}, {}

    def visit(node):
        """Index raw requirements and their concrete image definitions, rejecting missing files or conflicting evidence."""
        nodes[node["id"]] = node
        description = node.get("description", {})
        for image in description.get("image", []) if isinstance(description, dict) else []:
            if REFERENCE.fullmatch(image["reference"]):
                continue
            source = (input_dir / image["reference"]).resolve()
            if not source.is_relative_to(input_dir.resolve()) or not source.is_file():
                raise ValueError(f"Missing input image or image escapes input: {image['reference']}")
            record = {**image, "analysis": analysis.get(image["reference"], {})}
            images[image["id"]] = record
        for child in node.get("children", []):
            visit(child)

    visit(raw)
    requirement_texts = "\n".join(requirement_text(tree, rid) for rid in tree.requirements)
    contexts = {}
    for rid in tree.leaves:
        node = nodes[rid]
        direct = {key: value for key, value in node.items() if key != "children"}
        refs = set(REFERENCE.findall(json.dumps(direct)))
        description = node.get("description", {})
        for image in description.get("image", []) if isinstance(description, dict) else []:
            if image.get("id") in images:
                refs.add(image["id"])
        contexts[rid] = {**tree.requirements[rid], "steps": [tree.steps[sid] for sid in tree.own_steps[rid]],
            "images": {key: images[key] for key in sorted(refs) if key in images}}
        headings = reference_headings(contexts[rid]["images"], requirement_texts)
        if headings:
            contexts[rid]["reference_headings"] = headings
        contracts = requirement_contracts(tree, rid)
        if contracts:
            contexts[rid]["role_contracts"] = contracts
    return tree, images, contexts


def load_frontend_context(arc_dir, input_dir):
    """Combine visual requirements with database and API artifacts for design stages."""
    tree, images, contexts = load_ui_context(arc_dir, input_dir)
    schema = read_artifact(arc_dir, "db_schema.json")
    contracts = read_artifact(arc_dir, "backend_apis.json")
    return tree, schema, contracts, images, contexts


def behavior_contexts(contexts):
    """Behavior stages use structured requirement text without repeated visual evidence."""
    return {rid: {key: value for key, value in context.items() if key != "images"}
        for rid, context in contexts.items()}

# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
# https://www.apache.org/licenses/LICENSE-2.0
"""Render documentation diagrams with the repository's licensed Manrope font."""

import base64
import html
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/diagrams"
FONT = base64.b64encode((ROOT / "docs/assets/fonts/manrope-latin-wght-normal.woff2").read_bytes()).decode()
LICENSE = html.escape((ROOT / "docs/assets/fonts/OFL.txt").read_text())
PAPER, INK, MUTED, BORDER, ACCENT = "#f3f1eb", "#272820", "#60645b", "#d8d4ca", "#b57921"

# First-party geometry follows the existing local-stack.svg icon vocabulary.
ICONS = {
    "components": '<rect x="1" y="1" width="8" height="8" rx="2"/>'
    '<rect x="15" y="1" width="8" height="8" rx="2"/>'
    '<rect x="8" y="15" width="8" height="8" rx="2"/>'
    '<path d="M5 9v3h14V9M12 12v3"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/>',
    "identity": '<path d="M12 2 3 6v6c0 5 5 8 9 10 4-2 9-5 9-10V6Z"/><path d="m8 12 3 3 5-6"/>',
}

DIAGRAMS = {
    "architecture-components": {
        "icon": "components",
        "title": "Native services behind the factory",
        "subtitle": "Clients submit contracts; the execution leader owns effects.",
        "cards": [
            ("Operator surfaces", "Studio, CLI, ForgeClient and 158 factory MCP tools use authenticated contracts."),
            (
                "Identity and project access",
                "OperatorAuth validates identity. AccessRoutes checks the requested resource and operation.",
            ),
            (
                "Typed controllers",
                "Native pyfly REST mappings bind inputs. Admission services freeze the run's selected inputs.",
            ),
            ("Execution services", "Engine, Journal and delivery consumers coordinate retained work under one leader."),
            (
                "Typed repositories",
                "Store uses SessionProvider.unit(). Native repositories persist entities in the selected schema.",
            ),
            (
                "Durable state and effects",
                "PostgreSQL owns records. Private storage owns credentials, Git workspaces and artifacts.",
            ),
        ],
        "footer": "Default combined process or split API replicas with one execution leader.",
    },
    "architecture-oauth": {
        "icon": "identity",
        "title": "OAuth consent and project resource custody",
        "subtitle": "Account authorization precedes exact schema review and project tool admission.",
        "cards": [
            ("Admit the service", "Native policy fixes issuer, endpoints, resource and scope. Local development uses different literal loopback hosts."),
            ("Register the client", "Current administrator authority and expected revision bind the client to its selected project and retained policy."),
            ("Authorize in the browser", "Single-use state, issuer, browser nonce and S256 PKCE bind consent to the actor and immutable registration."),
            ("Retain token effects", "PostgreSQL records intent before exchange or refresh. Encrypted grants and exact receipts retain observed outcomes."),
            ("Bind the project resource", "OAuthResources rechecks policy, grant family, expiry and current project authority before resource use."),
            ("Review and grant tools", "Exact discovered schemas, repository/tool grants and frozen role permission precede broker dispatch and receipt reuse."),
        ],
        "footer": "Five vendor OAuth templates remain blocked; the owned local service has four read tools.",
    },
    "architecture-ledger": {
        "icon": "database",
        "title": "Records that connect a run to its evidence",
        "subtitle": "Actual references and keys; external services remain outside the database transaction.",
        "cards": [
            (
                "Repository → run",
                "forge_runs.repository_id references forge_repositories. Run data freezes admission snapshots.",
            ),
            ("Run → plan revision", "forge_plans retains a digest. The unique key is run_id plus revision."),
            (
                "Plan → task state",
                "forge_tasks references plan_id and run_id. One task_id per run retains attempt and evidence.",
            ),
            (
                "Run → journal",
                "Sequence and previous_hash link records. An intent key plus kind is unique within the run.",
            ),
            (
                "Campaign → child attempt",
                "A project request owns component attempts. Each child references one immutable run admission.",
            ),
            (
                "Separate effect records",
                "Execution requests and deployments have their own status and receipts. Artifacts "
                "remain private files.",
            ),
        ],
        "footer": "Run locks serialize journal updates; PostgreSQL rejects changed immutable history.",
        "connect": False,
    },
    "architecture-admission": {
        "title": "From admission to verified source",
        "subtitle": "API acceptance retains work; it does not establish a coding or publication outcome.",
        "lanes": ["Operator", "API", "PostgreSQL", "Leader"],
        "steps": [
            (0, 1, "Submit run", "Identity and project access precede typed RunInput admission."),
            (
                1,
                2,
                "Freeze inputs",
                "Store.create_run retains product, provider, policy, role, runner and knowledge snapshots.",
            ),
            (1, 0, "Return run ID", "The queued record is retained; no project command ran in the API request."),
            (
                3,
                2,
                "Claim queued run",
                "Engine rechecks ownership; Store.claim locks the row and current campaign boundary.",
            ),
            (
                3,
                3,
                "Prepare and implement",
                "The leader records task intent, prepares the authorized Git root and invokes the selected provider.",
            ),
            (
                3,
                3,
                "Verify and checkpoint",
                "Actual commands, optional review and matching source support the local checkpoint.",
            ),
            (3, 2, "Retain evidence", "Failures remain recorded. A changed verification tree requires fresh checks."),
        ],
        "footer": "One active coding run per repository; independent repositories share the leader budget.",
    },
    "architecture-wal": {
        "title": "Record an intent before producing an effect",
        "subtitle": "A receipt records the observed outcome; a hash chain checks retained record integrity.",
        "lanes": ["Leader", "Journal", "Executor", "Operator"],
        "steps": [
            (
                0,
                1,
                "Commit intent",
                "Journal.intent locks the run, derives the task/attempt key and commits its intent.",
            ),
            (
                0,
                2,
                "Produce effect",
                "Only the admitted effect proceeds: coding, verification, publication or another planned action.",
            ),
            (2, 0, "Observe outcome", "The returned result and actual source identify what the executor observed."),
            (0, 1, "Commit receipt", "Journal.receipt updates task evidence and the checkpoint in one database unit."),
            (
                1,
                3,
                "Hold unknown effect",
                "A process interruption or missing receipt requires reconciliation; no blind remote replay.",
            ),
            (
                3,
                1,
                "Reconcile evidence",
                "Observe the actual workspace or service. Generic reconciliation cannot retry an "
                "ambiguous remote effect.",
            ),
        ],
        "footer": "A database owner can rewrite a whole chain; this ledger is not independent notarization.",
    },
    "architecture-campaign": {
        "title": "Admit a campaign, then release its components",
        "subtitle": "The coordinator uses the frozen dependency graph and matching executable receipts.",
        "lanes": ["Operator", "Admission", "PostgreSQL", "Coordinator"],
        "steps": [
            (
                0,
                1,
                "Preflight request",
                "Resolve source commits and runner capabilities before taking admission locks.",
            ),
            (1, 2, "Lock and compare", "Request identity, project revision and configuration vector must still match."),
            (
                1,
                2,
                "Commit all children",
                "Campaign and component admissions commit together, or the database unit rolls back.",
            ),
            (
                3,
                2,
                "Release ready child",
                "Queue a component only after its dependencies pass the selected receipt gate.",
            ),
            (
                3,
                3,
                "Check exact evidence",
                "Passing executable checks, tree/checkpoint and requested review must agree. "
                "Published gates add a PR receipt.",
            ),
            (
                3,
                2,
                "Retain dependencies",
                "Downstream context receives upstream references. It does not merge code or prove product integration.",
            ),
            (
                3,
                3,
                "Hold uncertainty",
                "Unknown effects and pending controls stop release. Individual child resume/retry "
                "cannot bypass the campaign.",
            ),
        ],
        "footer": "Explicit campaign actions preserve request identities and prior component attempts.",
    },
    "architecture-authority": {
        "icon": "identity",
        "title": "Recheck authority where an effect happens",
        "subtitle": "Frozen identity and connection references do not freeze a grant indefinitely.",
        "cards": [
            (
                "Authenticate the request",
                "OIDC access tokens or server sessions establish identity. Cookie writes also pass origin protection.",
            ),
            (
                "Authorize project operation",
                "AccessRoutes and ProjectAccess check membership and operation. Client approval data is not authority.",
            ),
            (
                "Retain actor and connection",
                "The run captures actor and SCM identity. Private credential revisions remain "
                "outside source workspaces.",
            ),
            (
                "Recheck before execution",
                "Current identity, project authority and unexpired OIDC administrator grants govern host effects.",
            ),
            (
                "Resolve current SCM grant",
                "SCMConnections refreshes credentials and narrows GitHub installation access to "
                "the selected repository.",
            ),
            (
                "Publish matching source",
                "Ephemeral Git credentials stay server-side. Source, checkpoint and actual "
                "publication receipt must agree.",
            ),
        ],
        "footer": "Revoked access remains revoked; a queued administrator flag does not renew sign-in.",
    },
    "architecture-agent-context": {
        "title": "Separate instructions, context and tool authority",
        "subtitle": "Instruction roles describe stage responsibility; they do not create seven model workers.",
        "cards": [
            (
                "Freeze the handoff",
                "Product, role instructions, pinned skill provenance and model selection belong to the admitted run.",
            ),
            (
                "Retrieve cited context",
                "Flycanon supplies the repository workspace and shared framework references. Cited "
                "context stays untrusted data.",
            ),
            (
                "Select available tools",
                "Native Agentic and bridge receive requested tools intersected with actual executor capabilities.",
            ),
            (
                "Keep credentials private",
                "Brave or Tavily search uses its selected server credential. The model receives bounded cited results.",
            ),
            (
                "Execute and report",
                "One selected coding provider writes source. Structured feedback can pause for an explicit answer.",
            ),
            (
                "Verify before memory",
                "Factory checks are independent. Verified memory is per repository, with retained "
                "source/version provenance.",
            ),
        ],
        "footer": "Official CLI permissions govern CLI workers; Python native-tool lists are advisory for them.",
        "connect": False,
    },
    "architecture-runners": {
        "title": "Keep ownership and execution boundaries explicit",
        "subtitle": "Current transports are host and Docker; future pool bindings fail closed.",
        "cards": [
            (
                "Admit from the API",
                "API replicas retain requests and read results. They do not execute project "
                "commands or delivery effects.",
            ),
            (
                "Acquire both fences",
                "One leader holds the private-storage lock and PostgreSQL schema advisory lease before recovery.",
            ),
            (
                "Choose the frozen transport",
                "Host uses the worker identity. Docker requires its explicit context and bounded execution policy.",
            ),
            (
                "Retain actual results",
                "Commands record outcomes. Docker source collection retains its archive before "
                "atomic workspace exchange.",
            ),
            (
                "Reconcile failed collection",
                "An uncertain import retains evidence and stops continuation. Repeating the "
                "invocation does not rerun the command.",
            ),
            (
                "Drain before takeover",
                "Lease loss stops consumers. The storage fence remains while old effects can still "
                "run; standbys must wait.",
            ),
        ],
        "footer": "Shared storage requires its own multi-host verification; standbys add no execution capacity.",
    },
    "learning-lookup": {
        "icon": "database",
        "title": "Check what can be reused before searching",
        "subtitle": "Learning contract under implementation; every admitted reuse requires scoped evidence.",
        "cards": [
            (
                "Bind the question",
                "Retain task, role, repository, source and dependency versions before choosing a reference.",
            ),
            (
                "Retrieve authorized knowledge",
                "Required flycanon retrieval must succeed. Similarity supplies candidates; it cannot approve reuse.",
            ),
            (
                "Evaluate exact evidence",
                "The ledger checks scope, status, context, source, version and database-time freshness.",
            ),
            (
                "Choose reuse or search",
                (
                    "A verified exact match returns its evidence. Missing or stale knowledge "
                    "records a bounded search reason."
                ),
            ),
            (
                "Retain the observation",
                "A tool receipt keeps citations and outcome. Web snippets remain reference candidates.",
            ),
            (
                "Verify before learning",
                (
                    "Actual task checks and source custody support a local procedure outcome. "
                    "Unknown effects require reconciliation."
                ),
            ),
        ],
        "footer": "Credentials stay in private configuration. A model cannot declare a lesson verified.",
    },
    "learning-feedback": {
        "icon": "components",
        "title": "Retain outcomes with their limits",
        "subtitle": (
            "Learning contract under implementation; local outcomes and failed checks retain distinct evidence."
        ),
        "cards": [
            ("Reference candidate", "Question, citations and tool receipt identify a hypothesis or source for review."),
            (
                "Verified local outcome",
                "Matching task, source, executable checks and configured review support one bounded procedure.",
            ),
            (
                "Observed failed check",
                "Retain actual command evidence and exact context. A failure does not establish a generalized defect.",
            ),
            (
                "Repair repeat control",
                (
                    "An unchanged repair attempt can be held. Independent verification, "
                    "diagnostics and intentional RED checks still run."
                ),
            ),
            (
                "Invalidate or supersede",
                (
                    "New source, versions, expiry or a reviewed contradiction change reuse "
                    "eligibility while preserving prior evidence."
                ),
            ),
            (
                "Review shared learning",
                (
                    "Cross-project promotion needs a separately reviewed safe framework record. "
                    "Private origins remain private."
                ),
            ),
        ],
        "footer": (
            "Quota, authentication, rate limits, transport errors and unknown outcomes remain operational records."
        ),
        "connect": False,
    },
}


def text(x, y, value, *, size=19, color=INK, anchor="start", width=None, weight=400):
    lines = textwrap.wrap(value, width) if width else [value]
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else size + 7}">{html.escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{spans}</text>'
    )


def header_layout(title, subtitle, narrow, icon=False):
    title_size = 22 if narrow else 25
    title_lines = len(textwrap.wrap(title, (22 if icon else 25) if narrow else 60))
    subtitle_lines = len(textwrap.wrap(subtitle, 33 if narrow else 95))
    subtitle_y = 43 + (title_lines - 1) * (title_size + 7) + 40
    return subtitle_y, subtitle_y + (subtitle_lines - 1) * 24 + 38


def begin(title, subtitle, width, height, icon=None):
    subtitle_y, _ = header_layout(title, subtitle, width < 400, bool(icon))
    return [
        "<!-- Copyright 2026 Firefly Software Foundation. Diagram: Apache-2.0; embedded Manrope: OFL-1.1. -->",
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-labelledby="title desc" viewBox="0 0 {width} {height}">',
        f'<title id="title">{html.escape(title)}</title><desc id="desc">{html.escape(subtitle)}</desc>',
        f"<metadata>{LICENSE}</metadata>",
        "<defs><style>",
        f'@font-face{{font-family:Manrope;src:url(data:font/woff2;base64,{FONT}) format("woff2");font-weight:200 800}}',
        "text{font-family:Manrope,Arial,sans-serif}",
        '</style><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0L10 5L0 10Z" fill="{ACCENT}"/></marker></defs>',
        f'<rect width="{width}" height="{height}" rx="12" fill="{PAPER}"/>',
        (
            f'<g class="diagram-icon" data-license="Apache-2.0" aria-hidden="true" '
            f'transform="translate(28 20) scale(1.25)" fill="none" stroke="{ACCENT}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[icon]}</g>'
            if icon
            else ""
        ),
        text(
            78 if icon else 28,
            43,
            title,
            size=25 if width > 400 else 22,
            width=60 if width > 400 else 22 if icon else 25,
            weight=650,
        ),
        text(28, subtitle_y, subtitle, size=17, color=MUTED, width=95 if width > 400 else 33),
    ]


def cards(diagram, narrow):
    entries = diagram.get("cards") or [
        (diagram["lanes"][start] + " → " + diagram["lanes"][end], label + ". " + detail)
        for start, end, label, detail in diagram["steps"]
    ]
    width, card_width = (375, 319) if narrow else (960, 436)
    _, top = header_layout(diagram["title"], diagram["subtitle"], narrow, bool(diagram.get("icon")))
    positions = []
    y = top
    stride = 1 if narrow else 2
    for offset in range(0, len(entries), stride):
        row = entries[offset : offset + stride]
        body_positions = [57 + (len(textwrap.wrap(title, 28 if narrow else 40)) - 1) * 27 + 37 for title, _ in row]
        row_height = max(
            body_y + (len(textwrap.wrap(detail, 31 if narrow else 46)) - 1) * 24 + 24
            for body_y, (_, detail) in zip(body_positions, row, strict=True)
        )
        for column, body_y in enumerate(body_positions):
            positions.append((28 + column * 468, y, row_height, body_y))
        y += row_height + 34
    footer_lines = len(textwrap.wrap(diagram["footer"], 34 if narrow else 105))
    height = y + footer_lines * 23 + 25
    svg = begin(diagram["title"], diagram["subtitle"], width, height, diagram.get("icon"))
    for index, (title, detail) in enumerate(entries):
        x, y, card_height, body_y = positions[index]
        svg.append(
            f'<rect x="{x}" y="{y}" width="{card_width}" height="{card_height}" '
            f'rx="8" fill="#ffffff" stroke="{BORDER}"/>'
        )
        svg.append(text(x + 18, y + 28, f"{index + 1:02d}", size=13, color=ACCENT, weight=700))
        svg.append(text(x + 18, y + 57, title, size=20, width=28 if narrow else 40, weight=650))
        svg.append(text(x + 18, y + body_y, detail, size=17, width=31 if narrow else 46))
        if diagram.get("connect", True) and index + 1 < len(entries):
            next_x, next_y, next_height, _ = positions[index + 1]
            if narrow:
                path = f"M187.5 {y + card_height}V{next_y - 6}"
            elif index % 2 == 0:
                path = f"M{x + card_width} {y + card_height / 2}H{x + 462}"
            else:
                path = (
                    f"M{x + card_width / 2} {y + card_height}V{y + card_height + 17}"
                    f"H14V{next_y + next_height / 2}H{next_x - 6}"
                )
            svg.append(f'<path d="{path}" fill="none" stroke="{ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>')
    svg.append(
        text(28, height - footer_lines * 23 - 4, diagram["footer"], size=16, color=MUTED, width=34 if narrow else 105)
    )
    svg.append("</svg>")
    return "".join(svg)


def sequence(diagram):
    _, top = header_layout(diagram["title"], diagram["subtitle"], False)
    height = top + 70 + len(diagram["steps"]) * 132 + 55
    svg = begin(diagram["title"], diagram["subtitle"], 960, height)
    xs = [100, 350, 610, 860]
    for x, lane in zip(xs, diagram["lanes"], strict=True):
        svg.append(text(x, top + 8, lane, size=20, anchor="middle", weight=650))
        svg.append(f'<path d="M{x} {top + 21}V{height - 75}" stroke="{BORDER}" stroke-dasharray="4 5"/>')
    for index, (start, end, label, detail) in enumerate(diagram["steps"]):
        y = top + 70 + index * 132
        a, b = xs[start], xs[end]
        if start == end:
            path = f"M{a} {y}H{a + 28}V{y + 17}H{a}"
            label_x, anchor = min(a + 55, 925), "end"
        else:
            path = f"M{a} {y}H{b}"
            label_x, anchor = (a + b) / 2, "middle"
        svg.append(f'<path d="{path}" fill="none" stroke="{ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>')
        svg.append(text(label_x, y - 13, f"{index + 1:02d} · {label}", size=18, anchor=anchor, weight=650))
        svg.append(text(28, y + 47, detail, size=17, width=100))
    svg.append(text(28, height - 25, diagram["footer"], size=16, color=MUTED, width=108))
    svg.append("</svg>")
    return "".join(svg)


def main():
    for name, diagram in DIAGRAMS.items():
        wide = sequence(diagram) if "steps" in diagram else cards(diagram, False)
        (OUTPUT / f"{name}.svg").write_text(wide, encoding="utf-8")
        (OUTPUT / f"{name}-narrow.svg").write_text(cards(diagram, True), encoding="utf-8")
        if name == "architecture-components":
            (OUTPUT / "architecture.svg").write_text(wide, encoding="utf-8")


if __name__ == "__main__":
    main()

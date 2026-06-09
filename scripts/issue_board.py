#!/usr/bin/env python3
"""issue_board.py — pull a repo's open GitHub issues, read the triage-label
mapping, parse dependency signals, and emit a ranked, dependency-ordered
execution board as JSON.

Why a script: dependency parsing + topological ordering is deterministic and
fiddly. Doing it in code makes the result reproducible and frees the model to
focus on the judgement calls (priority narrative + the clarity gate) that a
script can't make.

Stdlib only. Shells out to `gh`. Run from inside the target repo, or pass
--repo OWNER/NAME. Prints JSON to stdout; prints an {"error": ...} object and
exits non-zero if the issues can't be fetched, so the caller can fall back to a
manual `gh issue list`.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# The five canonical triage roles (see triage-labels.md). The repo may map any
# of these to a different label string; load_label_map() resolves the mapping.
CANONICAL_ROLES = ["needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix"]
DEFAULT_LABEL_MAP = {r: r for r in CANONICAL_ROLES}

# Priority weight by ROLE — lower runs sooner. ready-for-agent first because an
# AFK agent can take it without a human; untriaged work sorts last.
ROLE_WEIGHT = {"ready-for-agent": 1, "ready-for-human": 2, "needs-info": 3, "needs-triage": 4}
UNTRIAGED_WEIGHT = 5
WONTFIX_ROLE = "wontfix"


def run_gh(args):
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        return None, "`gh` CLI not found on PATH"
    if out.returncode != 0:
        return None, (out.stderr.strip() or f"gh exited {out.returncode}")
    return out.stdout, None


def load_label_map(labels_file):
    """Parse a triage-labels.md markdown table (role | label | meaning) into a
    role->label dict. Falls back to the identity default if the file is missing
    or has no recognisable rows. Returns (mapping, source) where source is the
    file path actually used or None."""
    mapping = dict(DEFAULT_LABEL_MAP)
    if not labels_file or not os.path.isfile(labels_file):
        return mapping, None
    try:
        text = open(labels_file, encoding="utf-8").read()
    except OSError:
        return mapping, None
    found = False
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        role, label = cells[0], cells[1]
        if role in CANONICAL_ROLES and label:
            mapping[role] = label
            found = True
    return (mapping, labels_file) if found else (mapping, None)


HEADING_RE = re.compile(r"^#{1,6}\s+")
DEP_SECTION_RE = re.compile(r"^#{1,6}\s*(blocked\s*by|depends\s+on|阻塞)", re.IGNORECASE)
INLINE_DEP_RE = re.compile(r"(?:blocked\s*by|depends\s+on)\s*[:：]?\s*((?:#\d+[,\s、]*)+)", re.IGNORECASE)
PARENT_RE = re.compile(r"parent\s*[:：]?\s*#(\d+)", re.IGNORECASE)
ISSUE_REF_RE = re.compile(r"#(\d+)")
AC_HEADING_RE = re.compile(r"^#{1,6}\s*(acceptance|验收|accept)", re.IGNORECASE)
CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]")
DESIGN_SIGNAL_RE = re.compile(r"brainstorm|决策点|待.{0,6}决定|待.{0,6}brainstorm|open question|未决", re.IGNORECASE)


def parse_body(body):
    """Extract dependency + readiness signals from one issue body.

    Returns dict with: blockers (set[int] of hard 'Blocked by' refs), parent
    (int|None — a grouping link, NOT a hard dependency), has_ac (bool),
    has_design_questions (bool)."""
    blockers, parent, soft_refs = set(), None, set()
    has_ac = False
    has_design = False
    if not body:
        return {"blockers": blockers, "parent": parent, "soft_refs": soft_refs,
                "sync_note": "", "has_ac": False, "has_design_questions": False}

    m = PARENT_RE.search(body)
    if m:
        parent = int(m.group(1))

    for im in INLINE_DEP_RE.finditer(body):
        blockers.update(int(r) for r in ISSUE_REF_RE.findall(im.group(1)))

    # Section form: under a "Blocked by"/"depends on"/"阻塞" heading, a hard
    # blocker is written as a LIST ITEM ("- #183") or a standalone "#183" line.
    # A #ref that appears mid-PROSE in the same section is NOT a hard dep — it's
    # a *soft* sync hint ("Not hard-blocked — ... waits until #184 publishes its
    # skeleton"). We capture those into soft_refs (ordering hint only, never a
    # block) and keep the raw prose in sync_note so the caller can show it.
    # Prose-only blockers without #refs (e.g. "## 阻塞链: B2+B3 全部落地") yield
    # nothing parseable, and the `blocked` label is the backstop for those.
    in_dep = False
    in_ac = False
    sync_lines = []
    for ln in body.splitlines():
        if DEP_SECTION_RE.match(ln):
            in_dep, in_ac = True, False
            continue
        if AC_HEADING_RE.match(ln):
            in_ac, in_dep = True, False
            continue
        if HEADING_RE.match(ln):
            in_dep = in_ac = False
        if in_dep:
            if re.match(r"^\s*[-*]\s+", ln) or re.match(r"^\s*#\d+", ln):
                blockers.update(int(r) for r in ISSUE_REF_RE.findall(ln))
            else:
                soft_refs.update(int(r) for r in ISSUE_REF_RE.findall(ln))
            if ln.strip():
                sync_lines.append(ln.strip())
        if in_ac and CHECKBOX_RE.match(ln):
            has_ac = True
    # Fallback: any checkbox anywhere still signals an actionable AC list.
    if not has_ac:
        has_ac = any(CHECKBOX_RE.match(ln) for ln in body.splitlines())
    has_design = bool(DESIGN_SIGNAL_RE.search(body))
    return {"blockers": blockers, "parent": parent, "soft_refs": soft_refs,
            "sync_note": " ".join(sync_lines).strip(),
            "has_ac": has_ac, "has_design_questions": has_design}


def kind_of(title):
    t = title.strip()
    if re.match(r"^\s*PRD\s*[:：]", t, re.IGNORECASE):
        return "prd"
    if re.match(r"^\s*design\s*[:：]", t, re.IGNORECASE):
        return "design"
    return "task"


def line_from_labels(labels, prefixes):
    """First label matching one of the business-line prefixes wins; return its
    suffix (e.g. 'area:writer' -> 'writer'). None when no label carries a line."""
    for label in labels:
        for p in prefixes:
            if label.lower().startswith(p.lower()) and len(label) > len(p):
                return label[len(p):].strip(" /:-") or None
    return None


def classify(labels, label_map):
    """Return (role, weight, excluded). role is the most-actionable triage role
    present; excluded is True when wontfix."""
    lset = set(labels)
    if label_map.get(WONTFIX_ROLE) in lset:
        return WONTFIX_ROLE, None, True
    best_role, best_weight = None, UNTRIAGED_WEIGHT
    for role, label in label_map.items():
        if role == WONTFIX_ROLE or label not in lset:
            continue
        w = ROLE_WEIGHT.get(role, UNTRIAGED_WEIGHT)
        if w < best_weight:
            best_weight, best_role = w, role
    return best_role, best_weight, False


def topo_order(nodes, edges, weight_of, number_of, soft_after=None):
    """Kahn's algorithm over HARD edges (edges[d] = blockers that must precede d).

    Among the currently-ready nodes we still need a deterministic pick. Priority
    (weight) then a *soft* sync hint then issue number, in that order:

    - soft_after[n] holds issues n should follow per a body sync note (e.g. #183
      "waits until #184 publishes its skeleton"). These are NOT blocks — n is
      still ready — but if a soft predecessor hasn't been emitted yet we defer n
      in favour of a soft-free peer, so #184 lands before #183 instead of losing
      to the meaningless "lower issue number wins" tie-break.
    - issue number is only the *last* resort, and the caller flags any position
      decided purely by it as a `tie` so it isn't mistaken for a real ordering.

    Returns (order, cycle_nodes). A soft cycle can't stall us — if every ready
    node has an unmet soft predecessor we fall back to the whole ready set."""
    soft_after = soft_after or {}
    nodeset = set(nodes)
    indeg = {n: len(edges.get(n, set())) for n in nodes}
    dependents = {n: [] for n in nodes}
    for d, blockers in edges.items():
        for b in blockers:
            if b in dependents:
                dependents[b].append(d)
    emitted = set()
    ready = [n for n in nodes if indeg[n] == 0]
    order = []
    while ready:
        def soft_blocked(n):
            return any((s in nodeset) and (s not in emitted) for s in soft_after.get(n, ()))
        free = [n for n in ready if not soft_blocked(n)]
        pool = free if free else ready  # soft cycle / all deferred → don't stall
        n = min(pool, key=lambda x: (weight_of(x), number_of(x)))
        ready.remove(n)
        order.append(n)
        emitted.add(n)
        for d in dependents[n]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    cycle = [n for n in nodes if indeg[n] > 0]
    return order, cycle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="OWNER/NAME (default: gh auto-detects from cwd)")
    ap.add_argument("--state", default="open")
    ap.add_argument("--labels-file", default="docs/agents/triage-labels.md",
                    help="markdown table mapping triage roles to label strings (SSOT)")
    ap.add_argument("--blocked-label", default="blocked",
                    help="label meaning 'depends on other issues, not yet startable'")
    ap.add_argument("--line-label-prefixes",
                    default="area:,module:,epic:,line:,业务线:,域:,模块:",
                    help="comma-separated label prefixes that name a business line (e.g. 'area:writer'). "
                         "The first matching label's suffix becomes business_line. Falls back to the "
                         "PRD/parent umbrella when no such label exists; the caller's AI fills the rest.")
    args = ap.parse_args()

    line_prefixes = [p.strip() for p in args.line_label_prefixes.split(",") if p.strip()]

    gh_args = ["issue", "list", "--state", args.state, "--limit", "300",
               "--json", "number,title,body,labels,assignees"]
    if args.repo:
        gh_args += ["--repo", args.repo]
    raw, err = run_gh(gh_args)
    if err:
        print(json.dumps({"error": err, "hint": "Authenticate with `gh auth login` or run inside the repo; "
                                                 "then fall back to a manual `gh issue list`."}, ensure_ascii=False))
        sys.exit(1)
    try:
        raw_issues = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"could not parse gh output: {e}"}, ensure_ascii=False))
        sys.exit(1)

    label_map, label_src = load_label_map(args.labels_file)
    open_numbers = {it["number"] for it in raw_issues}

    # Resolve OWNER/NAME so the HTML renderer can build issue links. Best-effort:
    # an unresolved slug just means cards link nowhere, not a failure.
    repo_slug = args.repo
    if not repo_slug:
        slug_out, _ = run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        repo_slug = slug_out.strip() if slug_out else None

    issues = {}
    for it in raw_issues:
        num = it["number"]
        labels = [l["name"] for l in it.get("labels", [])]
        assignees = [a.get("login") for a in it.get("assignees", []) if a.get("login")]
        parsed = parse_body(it.get("body", ""))
        role, weight, excluded = classify(labels, label_map)
        has_blocked_label = args.blocked_label in labels
        open_blockers = sorted(b for b in parsed["blockers"] if b in open_numbers and b != num)
        open_soft = sorted(s for s in parsed["soft_refs"]
                           if s in open_numbers and s != num and s not in open_blockers)
        is_blocked = has_blocked_label or bool(open_blockers)
        ready_now = (not excluded) and (role in ("ready-for-agent", "ready-for-human")) and not is_blocked
        issues[num] = {
            "number": num,
            "title": it["title"],
            "labels": labels,
            "assignees": assignees,
            "in_progress": bool(assignees),    # someone's on it; render as 进行中
            "role": role,
            "weight": weight,
            "kind": kind_of(it["title"]),
            "parent": parsed["parent"],
            "blockers": sorted(parsed["blockers"]),
            "open_blockers": open_blockers,
            "unlocks": [],                     # filled below: open issues this one hard-unblocks
            "soft_after": open_soft,           # ordering hint only (body sync note), NOT a block
            "soft_blocks": [],                 # filled in below: who softly waits on this one
            "sync_note": parsed["sync_note"],
            "business_line": line_from_labels(labels, line_prefixes),  # may be refined below
            "business_line_source": "label" if line_from_labels(labels, line_prefixes) else None,
            "has_blocked_label": has_blocked_label,
            "is_blocked": is_blocked,
            "excluded": excluded,
            "ready_now": ready_now,
            "signals": {
                "has_acceptance_criteria": parsed["has_ac"],
                "has_design_questions": parsed["has_design_questions"],
            },
        }

    # Reverse soft links: who softly waits on n (so n's reason can say "do first").
    for n in issues:
        issues[n]["soft_blocks"] = sorted(m for m in issues if n in issues[m]["soft_after"])
        # Hard unlocks: open issues that list n among their open blockers. This is
        # the "complete n → these become startable" edge the visual board draws.
        issues[n]["unlocks"] = sorted(m for m in issues if n in issues[m]["open_blockers"])

    # Business line, level 2 (label is level 1, set above): the PRD/design umbrella.
    # An umbrella issue heads its own line; a child inherits its parent umbrella.
    # Anything still unclassified is left null for the caller's AI to cluster.
    def umbrella_line(n):
        v = issues[n]
        if v["kind"] in ("prd", "design"):
            return f"#{n} {v['title']}".strip(), "self"
        p = v["parent"]
        if p in issues and issues[p]["kind"] in ("prd", "design"):
            return f"#{p} {issues[p]['title']}".strip(), "parent"
        if p is not None:  # parent closed/out of board — still a grouping signal
            return f"PRD #{p}", "parent"
        return None, None
    for n in issues:
        if issues[n]["business_line"]:
            continue
        line, via = umbrella_line(n)
        if line:
            issues[n]["business_line"] = line
            issues[n]["business_line_source"] = "prd"

    # Topological order over everything except wontfix. Edges only from real
    # 'Blocked by' refs to still-open issues; parent links are NOT edges.
    nodes = [n for n, v in issues.items() if not v["excluded"]]
    node_set = set(nodes)
    edges = {n: set(issues[n]["open_blockers"]) & node_set for n in nodes}
    soft_after = {n: set(issues[n]["soft_after"]) & node_set for n in nodes}
    order, cycle = topo_order(nodes, edges,
                              weight_of=lambda n: issues[n]["weight"] or UNTRIAGED_WEIGHT,
                              number_of=lambda n: n,
                              soft_after=soft_after)

    def reason(n):
        v = issues[n]
        if v["open_blockers"]:
            base = f"被 #{', #'.join(map(str, v['open_blockers']))} 阻塞（未完成）"
        elif v["has_blocked_label"]:
            base = "标记 blocked（依赖未在正文列出具体 issue）"
        elif v["role"] == "ready-for-agent":
            base = "ready-for-agent，无硬依赖，可直接派 agent"
        elif v["role"] == "ready-for-human":
            base = "ready-for-human，无硬依赖，需人工实施"
        elif v["role"] in ("needs-info", "needs-triage"):
            base = f"{v['role']}：尚未规约，先 triage"
        else:
            base = "未打 triage label，先 triage"
        if v["soft_after"]:
            base += f"；软同步点：宜排在 #{', #'.join(map(str, v['soft_after']))} 之后（正文同步说明，非硬阻塞）"
        if v["soft_blocks"]:
            base += f"；#{', #'.join(map(str, v['soft_blocks']))} 的同步点指向它，宜先做"
        return base

    def order_basis(n):
        """Why this issue sits where it does — so a pure issue-number tie isn't
        mistaken for a meaningful order."""
        v = issues[n]
        if v["open_blockers"]:
            return "dependency"     # forced after its hard blockers
        if v["soft_after"] or v["soft_blocks"]:
            return "soft-sync"      # placed by a body sync note, not the tie-break
        if v["has_blocked_label"]:
            return "blocked-label"
        return "tie"                # arbitrary among equals — resolve by content

    for n in nodes:
        issues[n]["reason"] = reason(n)
        issues[n]["order_basis"] = order_basis(n)

    warnings = []
    if cycle:
        warnings.append(f"检测到依赖环，涉及 #{', #'.join(map(str, sorted(cycle)))}——无法给出有效顺序，需人工拆环")
    excluded_nums = sorted(n for n, v in issues.items() if v["excluded"])
    if excluded_nums:
        warnings.append(f"已排除 wontfix：#{', #'.join(map(str, excluded_nums))}")

    board = {
        "repo": args.repo or "(cwd auto-detect)",
        "repo_slug": repo_slug,
        "state": args.state,
        "total_open": len(raw_issues),
        "label_map": label_map,
        "label_map_source": label_src or "built-in defaults (triage-labels.md not found)",
        "blocked_label": args.blocked_label,
        "ready_now": [issues[n] for n in order if issues[n]["ready_now"]],
        "blocked": [issues[n] for n in order if issues[n]["is_blocked"] and not issues[n]["excluded"]],
        "not_ready": [issues[n] for n in order
                      if not issues[n]["ready_now"] and not issues[n]["is_blocked"] and not issues[n]["excluded"]],
        "recommended_order": [
            {"number": n, "title": issues[n]["title"], "role": issues[n]["role"],
             "ready_now": issues[n]["ready_now"], "order_basis": issues[n]["order_basis"],
             "sync_note": issues[n]["sync_note"], "reason": issues[n]["reason"]}
            for n in order
        ],
        "issues": [issues[n] for n in sorted(issues)],
        "cycles": sorted(cycle),
        "warnings": warnings,
    }
    print(json.dumps(board, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

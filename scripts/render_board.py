#!/usr/bin/env python3
"""render_board.py — turn an issue_board.py board (plus optional AI annotations)
into a self-contained HTML "business-line & unlock map".

Division of labour, mirroring the skill: the board JSON carries everything
DETERMINISTIC (status, hard/soft dependency edges, label/PRD business lines);
the annotations JSON carries the JUDGEMENT the script can't make — the
"complete this line → these features ship" narrative, the headline, and the
business-line grouping for issues that have no label/PRD signal (so the AI's
semantic clustering lands somewhere). Annotations are OPTIONAL: with none you
still get a valid board grouped by label/PRD/ungrouped, just without the
unlock copy.

Stdlib only. Reads the board from a file (--board) or stdin, writes HTML to
--out, prints the path. The template lives at assets/board_template.html next
to this scripts/ dir.
"""
import argparse
import datetime
import json
import os
import sys

STATUS_ORDER = {"ready": 0, "todo": 1, "blocked": 2}
LANE_TAG_FALLBACK = "ungrouped"


def issue_status(it):
    if it.get("excluded"):
        return None  # wontfix — drop from the visual board
    if it.get("ready_now"):
        return "ready"
    if it.get("is_blocked"):
        return "blocked"
    return "todo"


def load_json(path):
    if path in (None, "", "-"):
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_model(board, ann, generated_at):
    slug = board.get("repo_slug")
    url_of = (lambda n: f"https://github.com/{slug}/issues/{n}") if slug else (lambda n: "")
    by_num = {it["number"]: it for it in board.get("issues", [])}

    # Position of each issue in the recommended execution order (for sorting
    # cards within a lane and lanes against each other). Missing => last.
    order_pos = {e["number"]: i for i, e in enumerate(board.get("recommended_order", []))}
    pos = lambda n: order_pos.get(n, 10**6)

    # Lane assignment from annotations takes precedence (this is where AI
    # semantic clusters for label/PRD-less issues land), then the board's own
    # business_line, then an "ungrouped" bucket.
    ann_lines = (ann or {}).get("business_lines", []) or []
    num_to_ann = {}
    for li, lane in enumerate(ann_lines):
        for n in lane.get("issues", []):
            num_to_ann[n] = li

    lanes = {}  # key -> lane dict

    def ensure_lane(key, name, source, unlock="", sort_hint=10**6):
        if key not in lanes:
            lanes[key] = {"key": key, "name": name, "source": source,
                          "unlock": unlock, "issues": [], "sort": sort_hint}
        return lanes[key]

    # Seed annotation lanes first so their order/copy is authoritative.
    for li, lane in enumerate(ann_lines):
        ensure_lane(f"ann-{li}", lane.get("name", f"业务线 {li+1}"),
                    lane.get("source", "ai"), lane.get("unlock", ""), sort_hint=li)

    rendered_nums = set()
    for n, it in by_num.items():
        status = issue_status(it)
        if status is None:
            continue
        rendered_nums.add(n)
        if n in num_to_ann:
            lane = lanes[f"ann-{num_to_ann[n]}"]
        elif it.get("business_line"):
            src = it.get("business_line_source") or "prd"
            lane = ensure_lane(f"bl::{it['business_line']}", it["business_line"], src)
        else:
            lane = ensure_lane("__ungrouped__", "未归组", LANE_TAG_FALLBACK)
        lane["issues"].append({
            "number": n,
            "title": it.get("title", ""),
            "url": url_of(n),
            "status": status,
            "role": it.get("role"),
            "kind": it.get("kind", "task"),
            "labels": it.get("labels", []),
            "assignees": it.get("assignees", []),
            "mine": it.get("mine", False),
            "in_progress": it.get("in_progress", False),
            "unlocks": it.get("unlocks", []),
            "blockers": it.get("open_blockers", []),
            "soft": it.get("soft_after", []),
            "_pos": pos(n),
        })

    # Sort cards within a lane by execution order, then status, then number.
    for lane in lanes.values():
        lane["issues"].sort(key=lambda c: (c["_pos"], STATUS_ORDER.get(c["status"], 9), c["number"]))
        for c in lane["issues"]:
            c.pop("_pos", None)
        # earliest execution position in the lane drives lane ordering
        positions = [pos(c["number"]) for c in lane["issues"]]
        lane["_earliest"] = min(positions) if positions else 10**6

    lane_list = [l for l in lanes.values() if l["issues"]]
    # Annotation-provided lanes keep their authored order; the rest sort by the
    # most-actionable issue they contain. Ungrouped always trails.
    def lane_key(l):
        ann_rank = l["sort"] if str(l["key"]).startswith("ann-") else 10**5
        ungrouped = 1 if l["key"] == "__ungrouped__" else 0
        return (ungrouped, ann_rank, l["_earliest"])
    lane_list.sort(key=lane_key)
    for l in lane_list:
        l.pop("sort", None); l.pop("_earliest", None)

    # Edges: hard = complete `from` unblocks `to`; soft = `to` should follow `from`.
    edges, seen = [], set()
    for n in rendered_nums:
        it = by_num[n]
        for m in it.get("unlocks", []):
            if m in rendered_nums and ("h", n, m) not in seen:
                seen.add(("h", n, m)); edges.append({"from": n, "to": m, "type": "hard"})
        for m in it.get("soft_blocks", []):
            if m in rendered_nums and ("s", n, m) not in seen:
                seen.add(("s", n, m)); edges.append({"from": n, "to": m, "type": "soft"})

    # the AI's recommended execution order, restricted to rendered issues —
    # drives the route breadcrumb, the "先做" flag, and within-column ordering
    rec_order = [{"number": e["number"]} for e in board.get("recommended_order", [])
                 if e.get("number") in rendered_nums]

    return {
        "repo": board.get("repo_slug") or board.get("repo") or "",
        "viewer": board.get("viewer"),
        "total_open": board.get("total_open"),
        "generated_at": generated_at,
        "label_map_source": board.get("label_map_source"),
        "warnings": board.get("warnings", []),
        "headline": (ann or {}).get("headline", ""),
        "order_note": (ann or {}).get("order_note", ""),
        "recommended_order": rec_order,
        "lanes": lane_list,
        "edges": edges,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="-", help="board JSON from issue_board.py (file, or '-'/omit for stdin)")
    ap.add_argument("--annotations", default=None, help="optional AI annotations JSON (business lines + unlock copy)")
    ap.add_argument("--out", default="/tmp/next-issue-board.html")
    ap.add_argument("--template", default=None, help="override template path")
    ap.add_argument("--date", default=None, help="generated-at stamp (default: today)")
    args = ap.parse_args()

    board = load_json(args.board)
    if "error" in board:
        print(json.dumps(board, ensure_ascii=False)); sys.exit(1)
    ann = load_json(args.annotations) if args.annotations else None

    template = args.template or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "board_template.html")
    with open(template, encoding="utf-8") as f:
        html = f.read()

    stamp = args.date or datetime.date.today().isoformat()
    model = build_model(board, ann, stamp)
    html = html.replace("__BOARD_DATA__", json.dumps(model, ensure_ascii=False))

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(args.out)


if __name__ == "__main__":
    main()

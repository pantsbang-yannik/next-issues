---
name: next-issue
description: >-
  Surveys the current repo's open GitHub issues, ranks them by triage label and
  dependency graph, and recommends an optimal execution order; when you pick one
  to start, it gates on whether the issue is clear enough to execute and routes
  unclear ones to the grill-with-docs skill before any code is written. It can
  also render the board as a self-contained HTML map that groups issues into
  business lines, draws their dependency arrows, and spells out which feature
  each chain ships once completed. Use WHENEVER the user asks what to work on
  next, which issue to pick up, to list / prioritize / sequence the backlog, for
  a dependency analysis, to visualize / draw / map the issues, their
  relationships, business lines, or what completing a chain unlocks, anything
  about *their own* issues ("my issues", who's blocking me, who's waiting on me,
  "我负责的 / 分给我的 / 谁在阻塞我 / 谁在等我"),
  "下一个做什么 / 排一下 issue 优先级 / 接下来推进哪个 / 这个 issue 能直接做吗 / 把 issue 关系和业务线画出来 / 生成依赖图 / 看板",
  or is about to start an issue and needs to know if it's ready — even without
  the word "issue". Detects the local `gh` login to build the personal view.
  Reads triage-labels.md as the label SSOT.
---

# Next Issue

Help the user decide **what to work on next** and **whether they can start it
immediately**. You survey the open issues, rank them by triage state and
dependencies, recommend an execution order, and — when the user commits to one —
check whether it's specified well enough to execute, routing fuzzy ones to a
`grill-with-docs` session first.

## What this is, and what it is not

- It **consumes** an already-triaged board. It does **not** re-triage incoming
  issues — that's the `triage` skill's job. If issues have no triage labels, say
  so and suggest triaging first; don't invent priorities for un-triaged work.
- It **stops at the clarity gate**. It does **not** implement the issue. Once an
  issue is judged execution-ready, hand back to the user to start the work (or to
  whatever implementation flow they use); if it's not ready, hand off to
  `grill-with-docs`.

Keeping this boundary tight is what makes the skill trustworthy: the user knows
that invoking it surveys and sequences, never silently starts changing code.

## Workflow

### 1. Pull and rank the board

Run the bundled script — it lives next to this file and only needs `python3`
and an authenticated `gh`:

```bash
python3 "<this-skill-dir>/scripts/issue_board.py"
```

It auto-detects the repo from the current directory (pass `--repo OWNER/NAME` to
override). It returns JSON with the issues already classified, ordered, and
annotated. Do the dependency + ordering work in the script, not in your head —
it's deterministic and the script handles the fiddly cases (prose vs. list-item
blockers, cycles, label remapping) consistently.

If the script prints an `{"error": ...}` object (no `gh`, not authenticated, not
a repo), tell the user what's wrong, then fall back to
`gh issue list --state open --json number,title,body,labels` and reason over it
by hand using the model below.

**Key fields in the output:**

- `ready_now` — actionable triage label (ready-for-agent / ready-for-human), no
  open blockers, not labelled blocked. These are what the user can pick today.
- `blocked` — has a `blocked` label and/or open `Blocked by` issues. Shows
  `open_blockers` so you can explain *what* unblocks it.
- `not_ready` — no actionable triage label yet (needs-triage / needs-info / no
  label). Surface these but flag that they need triage, not execution.
- `recommended_order` — a topologically valid sequence (hard blockers before the
  issues they block). Each entry carries a `reason` and an **`order_basis`** that
  tells you *why* it sits there — trust it accordingly:
  - `dependency` — forced after a hard `Blocked by`. Real, keep it.
  - `soft-sync` — placed by a body sync note (e.g. "#205 waits until #221 publishes
    its email templates"); the `sync_note` field has the text. Real, and worth surfacing.
  - `tie` — **arbitrary**. Same priority, no hard or soft signal, so the script
    fell back to issue number. This is NOT a meaningful order. When two `tie`
    items sit next to each other, read their bodies and reorder by actual leverage
    (does one unblock more? is one a prerequisite the other's text implies?) —
    don't present "lower number first" as if it meant something. If you genuinely
    can't tell, say they're interchangeable rather than inventing a winner.
- `signals.has_acceptance_criteria` / `signals.has_design_questions`,
  `kind` (task / prd / design) — hints for the clarity gate in step 3.
- `label_map_source` — which triage-labels.md it read, or that it used defaults.
- `cycles` / `warnings` — surface these prominently if non-empty.

**The personal view** — the script asks `gh api user` who is looking (top-level
`viewer`; null if it can't tell, `--viewer LOGIN` to override, `--viewer ''` to
disable) and builds `my_board` from it:

- per-issue `mine` — the viewer is among its assignees.
- `open_blockers_detail` / `unlocks_detail` — the upstream/downstream refs
  echoed *with titles and assignees*, so you can say "被 #204 阻塞——那是
  @alice 的" or "你做完 #202 就解锁了 @carol 的 #205" without extra `gh` calls.
- `my_board.mine` / `mine_ready` / `mine_blocked` — the viewer's issues in
  recommended order; each blocked one carries its `blockers` (with owners) and a
  `blocker_unspecified` flag for label-only blocks.
- `my_board.waiting_on_me` — the downstream view: whose issues are waiting on
  something of mine. This is the "别人在等你" signal — surface it, it often
  outranks the viewer's own preference for what to do next.
- `my_board.claimable` — ready, *unassigned* `task` issues, offered as 可认领
  candidates (small teams routinely leave issues unassigned; don't treat
  unassigned as "not mine to touch").
- `my_board.next_for_me` — first of `mine_ready`, else first claimable, with a
  `basis` of `assigned` / `claimable` so you can phrase it honestly.

Fields that feed the **visual board** in step 2:

- `unlocks` — open issues this one *hard-unblocks* (the reverse of their
  `open_blockers`). This is the "complete X → these become startable" edge the
  map draws as a solid arrow. `soft_blocks` is the dashed (soft-sync) version.
- `business_line` / `business_line_source` — the script's first guess at which
  feature line an issue belongs to: a label prefix (`area:` / `module:` /
  `业务线:` …, source `label`) if present, else its PRD/design umbrella (source
  `prd`), else `null`. **A `null` is your cue to cluster it yourself** in step 2.
- `assignees` / `in_progress` — someone's already on it; the map flags it 进行中,
  shows the assignees as `@name` chips, and marks the viewer's own cards (`mine`)
  with an accent border + 我 badge and a "只看我的" toolbar filter.
- `repo_slug` — `OWNER/NAME`, used to build the card→GitHub links.

### 2. Present the board — a visual map first, a terminal summary second

The payoff the user actually wants is a **picture they can read at a glance**:
which business line each issue belongs to, what blocks what, and — by completing
a given line — which feature ships. So the primary artifact of this step is an
HTML "business-line & unlock map"; the terminal summary is the quick companion.

#### 2a. Cluster business lines and write the unlock copy

The script already grouped every issue that carried a signal (a `business_line`
from a label prefix or a PRD/design umbrella). Two things are left that only you
can do, and they're what make the map worth looking at:

1. **Cluster the leftovers.** Every issue with `business_line: null` needs a home.
   Read its title/body and group by the *feature it serves* — an epic or ADR
   reference, a "Phase 2 / Mn" milestone tag, a shared service or command, a common
   user-facing capability are all good seeds. Issues the script grouped by PRD are
   usually fine as-is; you can rename a lane to the feature it delivers rather than
   the raw PRD title if that reads clearer.
2. **Write the "complete this line → this ships" copy.** This is the sentence the
   whole picture exists to deliver, and the script can't write it — it's a
   judgement about what capability the line unlocks. One line per business line.

Put both in an annotations JSON. It's *optional* (without it you still get a valid
map grouped by label/PRD, just no unlock copy) — but the unlock copy is the point,
so write it unless the user only wants the bare dependency picture.

```json
{
  "headline": "Start with #202 (cart service) — it's in progress and unblocks the whole checkout chain; #203 can run in parallel.",
  "order_note": "Search (#210→#212) and Notifications (#220→#222) are independent tracks; #205 order-emails softly waits on #221 templates; triage #230 first; #240 is blocked on legal sign-off.",
  "business_lines": [
    {"name": "Checkout revamp", "issues": [201, 202, 203, 204, 205, 206],
     "unlock": "Ship one-click checkout end to end: cart → payment → confirmation → A/B"},
    {"name": "Search relevance", "issues": [210, 211, 212],
     "unlock": "Relevant search from ingestion → ranking → new results UI"},
    {"name": "Notifications", "issues": [220, 221, 222],
     "unlock": "One fan-out service powering transactional email + web push"},
    {"name": "Platform stability", "issues": [230], "unlock": "Kill the Safari login 500 blocking sign-ins"},
    {"name": "Compliance", "issues": [240], "unlock": "GDPR data-export endpoint (gated on legal sign-off)"}
  ]
}
```

Keep a business line honest: it's a **deliverable feature**, not a label dump.
Merge two umbrellas if they serve one feature; an issue that genuinely stands
alone (an isolated bug) gets a one-issue lane with an empty `unlock` — don't
manufacture a feature for it. Every rendered open issue should land in exactly
one lane; if you leave some out of the annotations they fall back to their
script-derived line (or a "未归组" lane), which is usually a sign you missed a
cluster.

#### 2b. Render and open the map

```bash
python3 "<this-skill-dir>/scripts/issue_board.py" > /tmp/issue_board.json
# write /tmp/issue_annotations.json per 2a
python3 "<this-skill-dir>/scripts/render_board.py" \
  --board /tmp/issue_board.json \
  --annotations /tmp/issue_annotations.json \
  --out /tmp/next-issue-board.html
open /tmp/next-issue-board.html      # macOS; use xdg-open on Linux
```

The HTML is self-contained (no network) and opens anywhere — the user can keep or
share it. It renders as a **workflow-style path graph on an infinite canvas**, not
a list:

- **Nodes** are issue cards laid out left→right by dependency depth (root issues
  on the left, the things they unlock to the right), so following the connectors
  *is* the implementation path. Each card shows the issue number, title, its
  labels, and `解锁 / 阻塞 / 软等` issue refs in grouped rows; status is a colored
  pill (可推进 / 被阻塞 / 未就绪 / 进行中); the business line shows as a colored dot
  + name. Clicking a card opens the issue on GitHub.
- **Connectors** are orthogonal rounded cables — solid = hard dependency pointing
  **prerequisite → unlocked**, dashed = soft-sync.
- **Top-left** is a lightweight "AI 推荐推进路线" note: the headline, an ordered
  breadcrumb of `recommended_order` (hover a chip to highlight that issue's path,
  click to fly to it), and the `order_note`. The board also flags the top pick
  with a **▶ 先做** badge right on its node.
- **Right panel** is the business-line legend = the unlock map: per line a
  progress bar (by status), the **交付** (unlock) sentence, and the **起点** (next
  actionable issue, or what's blocking it).
- **Canvas**: pan by holding space + drag (or drag empty space), wheel to zoom,
  with 适应 / 复位 buttons. Hover a node to light up its upstream+downstream path;
  hover a business line in the panel to highlight that whole line.
- **Person-aware**: cards show their assignees as `@name` chips; the viewer's own
  cards get an accent border and a 我 badge, and a **只看我的** toolbar button
  dims everything except the viewer's issues plus their full upstream/downstream
  chain (the button only appears when the board knows the viewer and they have
  assigned issues).

The colour discipline is deliberate: status is the only saturated colour (pill +
the progress bar + the 解锁/阻塞 refs), business lines use one quiet dot each, and
everything else stays neutral — so state reads clearly without the canvas turning
into confetti. If you adjust the template, preserve that.

#### 2c. Terminal summary

The HTML carries the detail, so keep the terminal note short and scannable — lead
with what's actionable, point at the map, and stop:

```markdown
# Issue 看板 · <repo> · open <N>

## 👤 我的（@alice）
| # | 标题 | 状态 | 上游 / 下游 |
|---|------|------|--------------|
| 204 | Rebuild checkout as one-page flow | ⛔ 被阻塞 | 等 #202(@bob)、#203(无人)；完成后解锁 @carol 的 #205 |
| 212 | New search results UI | 🟢 可推进 | 无依赖 |
> 下一个：**#212**（你名下 ready 中顺序最靠前）；可认领：#210（ready、无人负责）。
> @carol 的 #205 在等你的 #204 —— 它卡在 @bob 的 #202 上，先去对齐 #202 的进度。

## 🟢 可直接推进（已规约、依赖已清）
| # | 标题 | label | 备注 |
|---|------|-------|------|
| 202 | Refactor cart into a stateless service | ready-for-agent | 进行中；解锁 #204 |
| 203 | Integrate new payment-provider SDK | ready-for-agent | 与 #202 并行 |
| 210 | Search ingestion pipeline v2 | ready-for-agent | 独立链根 |

## ⛔ 被阻塞（依赖未完成）
| # | 标题 | 阻塞于 | 解锁条件 |
|---|------|--------|----------|
| 204 | Rebuild checkout as one-page flow | #202 #203 | 两者合并后启动 |
| 205 | Order-confirmation emails | #204 | 且软等 #221 模板 |

## 🟡 未就绪（需先 triage）
| # | 标题 | 状态 |
|---|------|------|
| 230 | Login 500 on Safari 17 | 无 triage label，bug，先 triage |

## 推荐执行顺序
1. **#202** — 进行中、解锁整条 checkout 链，先做收益最大；#203 可并行
2. **#210 / #220** — 搜索、通知两条独立链的根，可并行起爆
3. **#204 → #205 → #206** — 依次随 #202/#203 解锁（#205 另软等 #221 模板）
> #201 是 PRD 伞，不单独"实施"——它统领 #202–#206，做完子项即闭环；#240 被 blocked，#230 先 triage。

## ⚠️ 注意
- <循环依赖 / wontfix 排除 / 用了默认 label 映射 等 warnings>
```

**The 👤 我的 section** comes from `my_board` and is what turns the board from
"团队全貌" into "我现在该干嘛": the viewer's issues, *who owns each upstream
blocker* (so a blocked issue becomes "去找 @bob 对齐 #202" instead of a dead
end), and *who is waiting downstream* (someone waiting on you usually outranks
your own pick). Include it whenever `my_board` is non-null and has anything to
say; skip it silently when `viewer` is null, the viewer has no issues and
nothing is claimable, or every issue belongs to the viewer anyway (solo repo —
a personal section that repeats the whole board is noise). Unassigned ready
issues are 可认领 candidates, not someone else's turf.

**Umbrella items (`kind` = prd / design):** don't recommend "implementing" a PRD
or a design issue. Present it as the header that groups its children (issues
whose `parent` points at it) and point the user at the children — or, for a
`design` issue, at the clarity gate (design issues almost always route to
grill-with-docs, see step 3).

**Soft sync points:** the script only treats list-item `Blocked by` refs as hard
dependencies. A #ref written as *prose* under that heading ("can start now, but
the wiring waits until #X publishes its skeleton") is a soft hint, not a block —
the issue stays `ready_now`, but it's recorded in `soft_after` (issues it should
follow) / `soft_blocks` (issues that wait on it), with the raw text in `sync_note`.
Use those to order the two correctly (the one others wait on goes first, even if
its number is higher — this is exactly the case the `tie` fallback gets wrong) and
surface the note in the 备注 column so the user sees the coordination point.

### 3. Clarity gate — when the user picks an issue to advance

When the user names a specific issue ("能直接做 #204 吗？" / "我想开始做 #240"),
your job is **not** to stamp a go/no-go verdict. It's to map the issue into two
buckets — **已经定了、可以照着做** vs **还没定、现在做会返工** — with real rigour,
then hand the second bucket to `grill-with-docs`.

Do the rigorous read: `gh issue view <n> --comments`, and cross-check the body
and its acceptance criteria against the glossary (`CONTEXT.md` / `CLAUDE.md`),
`docs/adr/`, and the actual code. The most valuable thing you can do here is
**surface a gap the user hadn't noticed** — that's worth far more than a confident
"绿灯". Read like the careful reviewer who finds the landmine before it goes off.

**If the "还没定" bucket is empty** — the issue is genuinely ready. Say so, restate
the acceptance criteria / scope so they start with the target in view, and stop.
Don't manufacture doubt just to look thorough.

**If the "还没定" bucket is non-empty** — name each open point precisely and route
those points to `grill-with-docs`. Three things matter here, and the skill exists
largely to get them right:

- **Describe what's unclear, don't decide it.** Say *what hasn't been settled*,
  not *what you'd choose*. Picking the design is grill-with-docs' job (it sharpens
  terms and updates CONTEXT.md / ADRs as decisions land); yours is to surface and
  hand off. Don't render your own resolution of an open question — even a tempting
  one.
- **Don't force a binary verdict that papers over the gaps.** "大方向清楚，但这 3
  点没定、先澄清" is the honest answer — not "绿灯，可以开干". You don't owe the user a
  yes/no; you owe them an accurate clarity map and a clean hand-off.
- **Don't rationalise a real *what*-gap as "implementation discretion."** An
  acceptance criterion with no decidable boundary — "大幅压缩" with no target,
  "更新所有引用" without saying which files count, "重构成模块化" without the
  module list — is a *what*-gap to clarify, not a *how*-choice to wave through.
  When unsure which it is, treat it as *what* and route it. A `ready-for-agent`
  label means triage *thought* it was AFK-ready — a strong prior, not a free pass.
  Verify independently; catching a gap triage missed is exactly the point.

The hand-off is a sharp seed for the grill-with-docs session — the specific
undecided points, why each would cause rework, and one line saying you won't
decide them yourself:

> #204 大方向是个设计议题、还没拍板，现在动 `app/checkout/` 会返工。还没定的点：
> 1. 是否支持游客结账（issue 列了正反论据，没结论）
> 2. 支付流程按单页全展开 / 分步向导 / 按金额阈值切（三个候选未选）
> 3. 地址自动补全用哪家服务、与现有 order schema 字段如何映射
> 建议先过一轮 **grill-with-docs** 把这些逐个敲定——它会顺手锁术语、更新
> CONTEXT.md/ADR，定了再 `to-issues` 拆可执行子任务。这几个决策我不替你拍。

For a `design` / `prd` issue the whole thing is usually undecided — route the
whole thing.

**Stay focused on the chosen issue — don't reprint the board.** When the user
picks one issue, answer about *that* issue; they already have the board, and a
trailing full-board dump is noise. One line pointing back is the most you'd add.

Either way, don't start grilling or implementing yourself — the gate ends at the
hand-off.

## The dependency model (how the script reads issues)

- **`Blocked by` list items** (`- #202`) → hard edges; the blocker must precede
  the dependent. The recommended order respects these.
- **`blocked` label** → blocked, even when the body names no specific issue
  (e.g. prose like "## 阻塞链: 等 B2+B3 落地"). Shown as blocked with
  "blocker unspecified".
- **`Parent: #201`** → grouping only, **not** a dependency. A child of a PRD is
  not blocked by the PRD; it's blocked only by its own `Blocked by` refs. This is
  why several siblings under one PRD can all be `ready_now`.
- **Closed blockers auto-clear** — the script only fetches open issues, so a
  `Blocked by #X` where #X is already closed stops counting as a blocker.

## Portability

The script reads `docs/agents/triage-labels.md` (a role→label markdown table) as
the label SSOT, so renamed labels are followed automatically. On a repo without
that file it falls back to the five canonical labels (needs-triage / needs-info /
ready-for-agent / ready-for-human / wontfix) and the `blocked` convention — say
so (`label_map_source`) so the user knows it's using defaults. Override the
blocked-label string with `--blocked-label` if a repo uses a different one.

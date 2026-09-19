import { useEffect, useState } from "react";
import {
  AGENT_OWNED_STAGES,
  MANUAL_STAGES,
  outreachApi,
  STAGE_ORDER,
  type Campaign,
  type ImportedProspect,
  type JobSnapshot,
  type MessagePreview,
  type OutreachItem,
  type Stage,
} from "../../agents/outreach";
import { Avatar, EmptyState, Icon, ScorePill, type IconName } from "../../components/ui";
import { RunOverlay } from "../../components/RunOverlay";
import { useCachedFetch, invalidate, patch } from "../../hooks/useCachedFetch";
import { ConfirmModal } from "../../components/ConfirmModal";
import { routeToPath } from "../../router";

const STAGE_LABEL: Record<Stage, string> = {
  needs_review: "Needs Review",
  pending: "Pending",
  sent: "Sent",
  waiting: "No Reply / Waiting",
  replied: "Replied",
  booked: "Booked",
  failed: "Failed",
};

const STAGE_STAT_LABEL: Record<Stage, string> = {
  needs_review: "Needs Review",
  pending: "Pending",
  sent: "Sent",
  waiting: "Waiting",
  replied: "Replied",
  booked: "Booked",
  failed: "Failed",
};

const STAGE_SUBLABEL: Record<Stage, string> = {
  needs_review: "Generic draft, not send-ready",
  pending: "Ready to send",
  sent: "Delivered",
  waiting: "Follow-up due",
  replied: "Needs attention",
  booked: "Meetings",
  failed: "Send failed",
};

const STAGE_ICON: Record<Stage, IconName> = {
  needs_review: "info",
  pending: "send",
  sent: "check",
  waiting: "clock",
  replied: "mail",
  booked: "calendar",
  failed: "close",
};

const STAGE_TONE: Record<Stage, "accent" | "mint" | "amber" | "violet"> = {
  needs_review: "amber",
  pending: "accent",
  sent: "mint",
  waiting: "amber",
  replied: "mint",
  booked: "violet",
  failed: "amber",
};

const STAGE_EMPTY: Record<Stage, string> = {
  needs_review: "Nothing needs review",
  pending: "Nothing queued yet.",
  sent: "No sent prospects yet",
  waiting: "No pending follow-ups",
  replied: "No replies yet",
  booked: "No meetings booked yet",
  failed: "No failed sends",
};

const STAGE_EMPTY_SUB: Record<Stage, string> = {
  needs_review: "Prospects with no usable research data at all (or a failed draft) land here for approval.",
  pending: "Import prospects and create a campaign to fill this column.",
  sent: "Send a message to move prospects here.",
  waiting: "Replies will appear here once prospects respond or reach their follow-up date.",
  replied: "Replies will appear here once prospects respond.",
  booked: "Booked meetings will appear here once prospects confirm a time.",
  failed: "Failed sends will appear here if delivery errors out.",
};

interface Props {
  campaignId: string;
  campaignName?: string | null;
  onBack: () => void;
}

export function CampaignDetail({ campaignId, campaignName, onBack }: Props) {
  const [activeJob, setActiveJob] = useState<JobSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<OutreachItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  const [preview, setPreview] = useState<MessagePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [scheduleValue, setScheduleValue] = useState("");
  const [scheduling, setScheduling] = useState(false);
  const [confirmDeleteItem, setConfirmDeleteItem] = useState(false);
  const [deletingItem, setDeletingItem] = useState(false);

  const [editOpen, setEditOpen] = useState(false);
  const [editInstruction, setEditInstruction] = useState("");
  const [revising, setRevising] = useState(false);
  const [reviseError, setReviseError] = useState<string | null>(null);

  const [redrafting, setRedrafting] = useState(false);
  const [bulkRedrafting, setBulkRedrafting] = useState(false);
  const [bulkRedraftResult, setBulkRedraftResult] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);

  const [campaignScheduleOpen, setCampaignScheduleOpen] = useState(false);
  const [campaignScheduleValue, setCampaignScheduleValue] = useState("");
  const [campaignScheduling, setCampaignScheduling] = useState(false);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);

  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dragOverStage, setDragOverStage] = useState<Stage | null>(null);

  const { data: campaigns } = useCachedFetch<Campaign[]>("campaigns", () => outreachApi.listCampaigns());
  const campaign = campaigns?.find((c) => c.campaign_id === campaignId) ?? null;
  // Fall back to the name carried in via the route (from the campaigns list
  // click, or recovered from the URL slug) so the header shows a real name
  // immediately instead of the raw UUID while the campaigns cache loads.
  const displayName = campaign?.name || campaignName || null;

  useEffect(() => {
    if (!campaign?.name) return;
    const path = routeToPath({ name: "outreach-campaign", campaignId, campaignName: campaign.name });
    if (path !== window.location.pathname) window.history.replaceState(null, "", path);
  }, [campaign?.name, campaignId]);

  const {
    data: itemsData,
    loading,
    refresh: refreshItems,
  } = useCachedFetch<OutreachItem[]>(`campaign-items:${campaignId}`, () => outreachApi.campaignItems(campaignId));
  const items = itemsData ?? [];

  const { data: prospectsData } = useCachedFetch<ImportedProspect[]>("prospects", () => outreachApi.listProspects());
  const prospectByDomain = new Map((prospectsData ?? []).map((p) => [p.research_domain, p]));

  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
    setScheduleValue(selected?.scheduled_at ? selected.scheduled_at.slice(0, 16) : "");
    setEditOpen(false);
    setEditInstruction("");
    setReviseError(null);
    if (!selected || selected.subject || selected.stage !== "pending") return;
    setPreviewLoading(true);
    outreachApi.itemPreview(selected.item_id)
      .then(setPreview)
      .catch((e) => setPreviewError(e instanceof Error ? e.message : String(e)))
      .finally(() => setPreviewLoading(false));
  }, [selected]);

  const pendingCount = items.filter((i) => i.stage === "pending").length;
  const sentCount = items.filter((i) => i.stage === "sent").length;
  const busy = activeJob?.status === "running" || activeJob?.status === "pending";
  const isDraft = sentCount === 0 && items.every((i) => i.stage === "pending" || i.stage === "needs_review" || i.stage === "failed");

  const pendingItems = items.filter((i) => i.stage === "pending");
  const needsReviewCount = items.filter((i) => i.stage === "needs_review").length;
  const genericPendingCount = items.filter(
    (i) => (i.stage === "pending" || i.stage === "needs_review") && i.personalized_from === "template_only",
  ).length;
  const campaignScheduledAt = pendingItems.length > 0 && pendingItems.every((i) => i.scheduled_at === pendingItems[0].scheduled_at)
    ? pendingItems[0].scheduled_at
    : null;

  async function handleSend() {
    setError(null);
    try {
      const resp = await outreachApi.startSend(campaignId);
      if (!resp.job_id) return;
      setActiveJob({
        job_id: resp.job_id, kind: "send", campaign_id: campaignId, status: "pending",
        done: 0, total: resp.items || 0, current_domain: null, result: null, error: null,
      });
      const ws = outreachApi.jobSocket(resp.job_id, (snap) => {
        setActiveJob(snap);
        if (snap.status === "done" || snap.status === "error") {
          ws.close();
          refreshItems();
          invalidate("outreach-analytics");
          setTimeout(() => setActiveJob((cur) => (cur?.job_id === resp.job_id ? null : cur)), 4000);
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // Optimistic: patch the cached items list immediately so both the drawer
  // buttons and the drag-and-drop columns react instantly instead of
  // waiting on the API round trip + refetch. Rolled back via invalidate()
  // (forces a real refetch) if the request turns out to have failed.
  function advanceStage(item: OutreachItem, stage: "waiting" | "replied" | "booked") {
    const itemsKey = `campaign-items:${campaignId}`;
    patch<OutreachItem[]>(itemsKey, (cur) =>
      (cur ?? []).map((i) => (i.item_id === item.item_id ? { ...i, stage } : i)),
    );
    setSelected(null);
    outreachApi.setItemStage(item.item_id, stage)
      .then(() => invalidate("outreach-analytics"))
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
        invalidate(itemsKey);
      });
  }

  // Drag-and-drop between the manual post-send columns (Waiting/Replied/
  // Booked), as an alternative to opening a card and picking a stage button.
  // Only these three are droppable -- pending/sent/needs_review stay
  // agent-owned (see AGENT_OWNED_STAGES) and are never set by dragging.
  function handleCardDrop(item: OutreachItem, stage: Stage) {
    setDragOverStage(null);
    setDraggingId(null);
    if (item.stage === stage) return;
    if (stage !== "waiting" && stage !== "replied" && stage !== "booked") return;
    advanceStage(item, stage);
  }

  async function handleSchedule(item: OutreachItem) {
    setScheduling(true);
    setError(null);
    try {
      const iso = scheduleValue ? new Date(scheduleValue).toISOString() : null;
      await outreachApi.scheduleItem(item.item_id, iso);
      setSelected((cur) => (cur ? { ...cur, scheduled_at: iso } : cur));
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) => (i.item_id === item.item_id ? { ...i, scheduled_at: iso } : i)),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScheduling(false);
    }
  }

  async function handleScheduleCampaign() {
    setCampaignScheduling(true);
    setError(null);
    try {
      const iso = campaignScheduleValue ? new Date(campaignScheduleValue).toISOString() : null;
      await outreachApi.scheduleCampaign(campaignId, iso);
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) => (i.stage === "pending" ? { ...i, scheduled_at: iso } : i)),
      );
      setCampaignScheduleOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCampaignScheduling(false);
    }
  }

  async function handleCancelCampaignSchedule() {
    setCampaignScheduling(true);
    setError(null);
    try {
      await outreachApi.scheduleCampaign(campaignId, null);
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) => (i.stage === "pending" ? { ...i, scheduled_at: null } : i)),
      );
      setCampaignScheduleValue("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCampaignScheduling(false);
    }
  }

  async function handleRename() {
    const name = renameValue.trim();
    if (!name) return;
    setRenaming(true);
    setRenameError(null);
    try {
      await outreachApi.renameCampaign(campaignId, name);
      patch<Campaign[]>("campaigns", (cur) =>
        (cur ?? []).map((c) => (c.campaign_id === campaignId ? { ...c, name } : c)),
      );
      invalidate("outreach-analytics");
      const path = routeToPath({ name: "outreach-campaign", campaignId, campaignName: name });
      if (path !== window.location.pathname) window.history.replaceState(null, "", path);
      setRenameOpen(false);
    } catch (e) {
      setRenameError(e instanceof Error ? e.message : String(e));
    } finally {
      setRenaming(false);
    }
  }

  async function handleRevise() {
    if (!selected || !editInstruction.trim()) return;
    setRevising(true);
    setReviseError(null);
    try {
      const revised = await outreachApi.reviseItem(selected.item_id, editInstruction.trim());
      setPreview(revised);
      setSelected((cur) => (cur ? { ...cur, subject: revised.subject, body: revised.body, personalized_from: revised.personalized_from } : cur));
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) =>
          i.item_id === selected.item_id
            ? { ...i, subject: revised.subject, body: revised.body, personalized_from: revised.personalized_from }
            : i,
        ),
      );
      invalidate("outreach-analytics");
      setEditOpen(false);
      setEditInstruction("");
    } catch (e) {
      setReviseError(e instanceof Error ? e.message : String(e));
    } finally {
      setRevising(false);
    }
  }

  async function handleRedraft() {
    if (!selected) return;
    setRedrafting(true);
    setReviseError(null);
    try {
      const redrafted = await outreachApi.redraftItem(selected.item_id);
      setPreview(redrafted);
      setSelected((cur) =>
        cur ? { ...cur, subject: redrafted.subject, body: redrafted.body, personalized_from: redrafted.personalized_from, stage: redrafted.stage } : cur,
      );
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) =>
          i.item_id === selected.item_id
            ? { ...i, subject: redrafted.subject, body: redrafted.body, personalized_from: redrafted.personalized_from, stage: redrafted.stage as Stage }
            : i,
        ),
      );
      invalidate("outreach-analytics");
    } catch (e) {
      setReviseError(e instanceof Error ? e.message : String(e));
    } finally {
      setRedrafting(false);
    }
  }

  async function handleApprove() {
    if (!selected) return;
    setApproving(true);
    setReviseError(null);
    try {
      await outreachApi.approveItem(selected.item_id);
      setSelected((cur) => (cur ? { ...cur, stage: "pending" } : cur));
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) =>
        (cur ?? []).map((i) => (i.item_id === selected.item_id ? { ...i, stage: "pending" } : i)),
      );
      invalidate("outreach-analytics");
    } catch (e) {
      setReviseError(e instanceof Error ? e.message : String(e));
    } finally {
      setApproving(false);
    }
  }

  async function handleBulkRedraft() {
    setBulkRedrafting(true);
    setBulkRedraftResult(null);
    setError(null);
    try {
      const res = await outreachApi.redraftGenericItems(campaignId);
      invalidate(`campaign-items:${campaignId}`);
      invalidate("outreach-analytics");
      setBulkRedraftResult(
        res.checked === 0
          ? "No generic drafts to re-draft."
          : `Checked ${res.checked} — ${res.upgraded} now personalized${res.still_generic ? `, ${res.still_generic} still generic (personalization failed even with a web search)` : ""}.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBulkRedrafting(false);
    }
  }

  async function handleDeleteItem() {
    if (!selected) return;
    const itemId = selected.item_id;
    setConfirmDeleteItem(false);
    setDeletingItem(true);
    setError(null);
    setSelected(null);
    try {
      await outreachApi.deleteItem(itemId);
      patch<OutreachItem[]>(`campaign-items:${campaignId}`, (cur) => (cur ?? []).filter((i) => i.item_id !== itemId));
      invalidate("outreach-analytics");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      invalidate(`campaign-items:${campaignId}`);
    } finally {
      setDeletingItem(false);
    }
  }

  async function confirmDeleteCampaign() {
    setConfirmDelete(false);
    setDeleting(true);
    setError(null);
    try {
      await outreachApi.deleteCampaign(campaignId);
      patch<Campaign[]>("campaigns", (cur) => (cur ?? []).filter((c) => c.campaign_id !== campaignId));
      invalidate("outreach-analytics");
      onBack();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  }

  const byStage = (stage: Stage) => items.filter((i) => i.stage === stage);

  return (
    <>
      <button type="button" className="back-link" onClick={onBack}>
        <Icon name="chevronLeft" size={15} />
        Back to campaigns
      </button>

      <div className="page-head">
        <div className="page-head-text">
          <p className="breadcrumb">Outreach Agent / Campaigns / <span>{displayName || campaignId}</span></p>
          <h1>
            {displayName || campaignId}
            <span className={`status-pill${isDraft ? " status-pill--draft" : " status-pill--live"}`}>
              {isDraft ? "Draft" : "Active"}
            </span>
          </h1>
          <p>5-stage pipeline for research-based outreach.</p>
        </div>
        <div className="page-head-actions">
          <button type="button" className="mint" onClick={handleSend} disabled={busy || pendingCount === 0}>
            <Icon name="send" size={15} />
            {busy ? "Sending…" : `Send ${pendingCount} pending`}
          </button>
          <div className="menu-wrap">
            <button
              type="button"
              className="ghost menu-trigger"
              title="More actions"
              disabled={deleting}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <Icon name="more" size={16} />
            </button>
            {menuOpen && (
              <>
                <div className="menu-backdrop" onClick={() => setMenuOpen(false)} />
                <div className="menu-popover">
                  <button
                    type="button"
                    className="menu-item"
                    onClick={() => {
                      setMenuOpen(false);
                      setCampaignScheduleValue(campaignScheduledAt ? campaignScheduledAt.slice(0, 16) : "");
                      setCampaignScheduleOpen(true);
                    }}
                  >
                    <Icon name="clock" size={14} />
                    Schedule campaign
                  </button>
                  {campaignScheduledAt && (
                    <button
                      type="button"
                      className="menu-item"
                      onClick={() => {
                        setMenuOpen(false);
                        handleCancelCampaignSchedule();
                      }}
                    >
                      <Icon name="close" size={14} />
                      Cancel schedule
                    </button>
                  )}
                  <button
                    type="button"
                    className="menu-item"
                    onClick={() => {
                      setMenuOpen(false);
                      setRenameValue(displayName || "");
                      setRenameError(null);
                      setRenameOpen(true);
                    }}
                  >
                    <Icon name="edit" size={14} />
                    Rename campaign
                  </button>
                  {genericPendingCount > 0 && (
                    <button
                      type="button"
                      className="menu-item"
                      disabled={bulkRedrafting}
                      onClick={() => {
                        setMenuOpen(false);
                        handleBulkRedraft();
                      }}
                    >
                      <Icon name="sparkle" size={14} />
                      {bulkRedrafting ? "Re-drafting…" : `Re-draft ${genericPendingCount} generic`}
                    </button>
                  )}
                  <button
                    type="button"
                    className="menu-item menu-item--danger"
                    onClick={() => {
                      setMenuOpen(false);
                      setConfirmDelete(true);
                    }}
                  >
                    <Icon name="trash" size={14} />
                    Delete campaign
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {error && <p className="error-text">{error}</p>}
      {bulkRedraftResult && <p className="hint">{bulkRedraftResult}</p>}

      {campaignScheduledAt && (
        <div className="schedule-banner">
          <Icon name="clock" size={14} />
          <span>
            Campaign scheduled for {new Date(campaignScheduledAt).toLocaleString()} — sending still requires
            clicking "Send pending".
          </span>
          <button
            type="button"
            className="schedule-banner-clear"
            onClick={handleCancelCampaignSchedule}
            disabled={campaignScheduling}
          >
            {campaignScheduling ? "Cancelling…" : "Cancel schedule"}
          </button>
        </div>
      )}

      {needsReviewCount > 0 && (
        <div className="schedule-banner">
          <Icon name="info" size={14} />
          <span>
            {needsReviewCount} prospect{needsReviewCount === 1 ? "" : "s"} need{needsReviewCount === 1 ? "s" : ""}{" "}
            review — no usable research data was found to personalize automatically (or drafting failed).
            Open a card in the Needs Review column to re-draft or approve it.
          </span>
        </div>
      )}

      {!loading && items.length === 0 ? (
        <EmptyState icon="send" message="No prospects in this campaign." />
      ) : (
        <>
          <div className="pipeline-stats">
            {STAGE_ORDER.map((stage) => (
              <div className="pipeline-stat" key={stage}>
                <span className={`pipeline-stat-icon pipeline-stat-icon--${STAGE_TONE[stage]}`}>
                  <Icon name={STAGE_ICON[stage]} size={17} />
                </span>
                <div>
                  <div className="pipeline-stat-value">{byStage(stage).length}</div>
                  <div className="pipeline-stat-label">{STAGE_STAT_LABEL[stage]}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="kanban-board">
            {STAGE_ORDER.map((stage) => {
              const isDropTarget = stage === "waiting" || stage === "replied" || stage === "booked";
              return (
              <div
                className={`kanban-col kanban-col--${STAGE_TONE[stage]}${isDropTarget && dragOverStage === stage ? " kanban-col--drag-over" : ""}`}
                key={stage}
                onDragOver={(e) => {
                  if (!isDropTarget || !draggingId) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  if (dragOverStage !== stage) setDragOverStage(stage);
                }}
                onDragLeave={() => {
                  if (dragOverStage === stage) setDragOverStage(null);
                }}
                onDrop={(e) => {
                  if (!isDropTarget) return;
                  e.preventDefault();
                  const item = items.find((i) => i.item_id === draggingId);
                  if (item) handleCardDrop(item, stage);
                }}
              >
                <div className="kanban-col-head">
                  <span className={`kanban-col-title${AGENT_OWNED_STAGES.includes(stage) ? " kanban-col-title--agent" : ""}`}>
                    <span className={`kanban-col-icon kanban-col-icon--${STAGE_TONE[stage]}`}>
                      <Icon name={STAGE_ICON[stage]} size={13} />
                    </span>
                    {STAGE_LABEL[stage]}
                  </span>
                  <span className="kanban-col-count">{byStage(stage).length}</span>
                </div>
                <p className="kanban-col-sub">{STAGE_SUBLABEL[stage]}</p>
                <div className="kanban-col-body">
                  {byStage(stage).map((item) => {
                    const prospect = prospectByDomain.get(item.research_domain);
                    const ready = Boolean(prospect?.pain_summary || prospect?.recommended_angle);
                    const draggable = stage === "sent" || stage === "waiting" || stage === "replied" || stage === "booked";
                    return (
                      <button
                        type="button"
                        key={item.item_id}
                        className={`kanban-card${draggable ? " kanban-card--draggable" : ""}${draggingId === item.item_id ? " kanban-card--dragging" : ""}`}
                        onClick={() => setSelected(item)}
                        draggable={draggable}
                        onDragStart={(e) => {
                          if (!draggable) return;
                          e.dataTransfer.effectAllowed = "move";
                          setDraggingId(item.item_id);
                        }}
                        onDragEnd={() => {
                          setDraggingId(null);
                          setDragOverStage(null);
                        }}
                      >
                        {draggable && <span className="kanban-card-drag-handle" title="Drag to move"><Icon name="grip" size={12} /></span>}
                        <div className="kanban-card-head">
                          <Avatar name={prospect?.name || item.research_domain} size="sm" />
                          <div className="kanban-card-headtext">
                            <div className="cell-company-name">{prospect?.name || item.research_domain}</div>
                            <div className="cell-company-domain">{item.to_email || "no contact"}</div>
                          </div>
                        </div>
                        <div className="kanban-card-row2">
                          {prospect?.contact_name && (
                            <span className="kanban-card-contact">{prospect.contact_name}</span>
                          )}
                          <span className="kanban-source-chip">Research Agent</span>
                        </div>
                        {(prospect?.tier || prospect?.score != null) && (
                          <div className="kanban-card-meta">
                            {prospect?.tier && <span className={`tier-badge tier-${prospect.tier}`}>Tier {prospect.tier}</span>}
                            {prospect?.score != null && <ScorePill score={prospect.score} />}
                          </div>
                        )}
                        {ready && (
                          <div className="kanban-card-ready">
                            <Icon name="check" size={12} />
                            Personalization ready
                          </div>
                        )}
                        {prospect?.recommended_angle && (
                          <div className="kanban-card-angle">
                            <span>Outreach angle:</span> {prospect.recommended_angle}
                          </div>
                        )}
                        {item.subject && <div className="kanban-card-subject">{item.subject}</div>}
                        {item.error && <div className="kanban-card-error">{item.error}</div>}
                      </button>
                    );
                  })}
                  {byStage(stage).length === 0 && (
                    <div className="kanban-col-empty">
                      <span className={`kanban-col-empty-icon kanban-col-empty-icon--${STAGE_TONE[stage]}`}>
                        <Icon name={STAGE_ICON[stage]} size={18} />
                      </span>
                      <div className="kanban-col-empty-title">{STAGE_EMPTY[stage]}</div>
                      <p className="kanban-col-empty-sub">{STAGE_EMPTY_SUB[stage]}</p>
                    </div>
                  )}
                </div>
              </div>
              );
            })}
          </div>
        </>
      )}

      {selected && (() => {
        const prospect = prospectByDomain.get(selected.research_domain);
        const displaySubject = selected.subject || preview?.subject;
        const displayBody = selected.body || preview?.body;
        const isLiveDraft = selected.stage === "needs_review" || selected.stage === "pending"; // not sent yet -- still editable
        return (
          <div className="drawer-overlay" onClick={() => setSelected(null)}>
            <div className="drawer" onClick={(e) => e.stopPropagation()}>
              <button className="drawer-close" onClick={() => setSelected(null)} type="button" aria-label="Close">
                <Icon name="close" size={16} />
              </button>
              <div className="drawer-head">
                <Avatar name={prospect?.name || selected.research_domain} />
                <div>
                  <h2>{prospect?.name || selected.research_domain}</h2>
                  <div className="cell-company-domain">{selected.research_domain}</div>
                </div>
              </div>

              <div className="drawer-meta">
                <span className={`tier-badge tier-${selected.stage === "pending" ? "D" : "A"}`}>
                  {STAGE_LABEL[selected.stage]}
                </span>
                {prospect?.tier && <span className={`tier-badge tier-${prospect.tier}`}>Tier {prospect.tier}</span>}
                {prospect?.score != null && <ScorePill score={prospect.score} />}
                {selected.scheduled_at && (
                  <span className="meta-chip">
                    <Icon name="clock" size={11} /> Scheduled {new Date(selected.scheduled_at).toLocaleString()}
                  </span>
                )}
              </div>

              <h3 className="drawer-section-title">Contact</h3>
              <div className="drawer-fact-grid">
                <div>
                  <span className="drawer-fact-label">Name</span>
                  <span className="drawer-fact-value">{prospect?.contact_name || "—"}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Position</span>
                  <span className="drawer-fact-value">{prospect?.contact_position || "—"}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Email</span>
                  <span className="drawer-fact-value">
                    {selected.to_email ? <a href={`mailto:${selected.to_email}`}>{selected.to_email}</a> : "—"}
                  </span>
                </div>
                <div>
                  <span className="drawer-fact-label">LinkedIn</span>
                  <span className="drawer-fact-value">
                    {prospect?.contact_linkedin_url ? (
                      <a href={prospect.contact_linkedin_url} target="_blank" rel="noreferrer">Profile</a>
                    ) : "—"}
                  </span>
                </div>
              </div>

              <h3 className="drawer-section-title">Company</h3>
              <div className="drawer-fact-grid">
                <div>
                  <span className="drawer-fact-label">Industry</span>
                  <span className="drawer-fact-value">{prospect?.industry || "—"}</span>
                </div>
                <div>
                  <span className="drawer-fact-label">Region</span>
                  <span className="drawer-fact-value">{prospect?.region || "—"}</span>
                </div>
              </div>

              {prospect?.pain_summary && (
                <>
                  <h3 className="drawer-section-title">Pain summary</h3>
                  <div className="quote-box">{prospect.pain_summary}</div>
                </>
              )}

              {prospect?.recommended_angle && (
                <>
                  <h3 className="drawer-section-title">Recommended angle</h3>
                  <div className="angle-box">{prospect.recommended_angle}</div>
                </>
              )}

              {prospect?.open_reqs && prospect.open_reqs.length > 0 ? (
                <>
                  <h3 className="drawer-section-title">Open roles cited</h3>
                  <ul className="drawer-list">
                    {prospect.open_reqs.slice(0, 5).map((r, i) => (
                      <li key={i}>{String((r as Record<string, unknown>).title || "Open role")}</li>
                    ))}
                  </ul>
                </>
              ) : prospect?.hiring_signal === "unknown" ? (
                <>
                  <h3 className="drawer-section-title">Open roles cited</h3>
                  <p className="hint" title="Research could not find this company's job board or careers page -- this is not evidence they aren't hiring.">
                    Job posting data unavailable for this company (not confirmed as zero openings).
                  </p>
                </>
              ) : null}

              <div className="drawer-section-head">
                <h3 className="drawer-section-title">
                  {isLiveDraft ? "Message preview" : "Message sent"}
                </h3>
                {isLiveDraft && displaySubject && !editOpen && (
                  <div style={{ display: "flex", gap: 6 }}>
                    <button type="button" className="drawer-section-action" onClick={handleRedraft} disabled={redrafting}>
                      <Icon name="refresh" size={12} className={redrafting ? "icon-spin" : undefined} />
                      {redrafting ? "Re-drafting…" : "Re-draft"}
                    </button>
                    <button type="button" className="drawer-section-action" onClick={() => setEditOpen(true)}>
                      <Icon name="edit" size={12} />
                      Edit
                    </button>
                  </div>
                )}
              </div>
              {isLiveDraft && (
                <p className="hint">
                  This hasn't been sent yet — drafted once when the campaign was created, so this preview always
                  matches what will actually send. Sending still requires clicking "Send pending" above.
                </p>
              )}
              {isLiveDraft && selected.personalized_from === "template_only" && (
                <p className="hint">
                  This is a generic template — every attempt to personalize it failed, including a live web
                  search for more information. Click "Re-draft" to try again (useful if the prospect has since
                  been qualified or re-imported with fresh research), or edit it manually.
                </p>
              )}
              {previewLoading && <p className="hint">Drafting…</p>}
              {previewError && <p className="error-text">Couldn't draft a message: {previewError}</p>}
              {reviseError && !editOpen && <p className="error-text">{reviseError}</p>}
              {displaySubject && (
                <>
                  <h4 className="drawer-subsection-title">Subject</h4>
                  <p className="drawer-subject-line">{displaySubject}</p>
                </>
              )}
              {displayBody && (
                <>
                  <h4 className="drawer-subsection-title">Body</h4>
                  <div className="quote-box" style={{ whiteSpace: "pre-wrap" }}>{displayBody}</div>
                </>
              )}
              {selected.error && <p className="error-text">{selected.error}</p>}

              {isLiveDraft && editOpen && (
                <div className="edit-draft-box">
                  <p className="hint">
                    Describe what to change — the existing email will be edited, not rewritten from scratch.
                  </p>
                  <textarea
                    className="edit-draft-textarea"
                    rows={3}
                    placeholder='e.g. "make it shorter" or "mention our AI hiring experience instead"'
                    value={editInstruction}
                    onChange={(e) => setEditInstruction(e.target.value)}
                  />
                  {reviseError && <p className="error-text">{reviseError}</p>}
                  <div className="action-row">
                    <button
                      type="button"
                      className="mint"
                      onClick={handleRevise}
                      disabled={revising || !editInstruction.trim()}
                    >
                      {revising ? "Updating…" : "Update email"}
                    </button>
                    <button type="button" onClick={() => setEditOpen(false)} disabled={revising}>
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {selected.stage === "needs_review" && (
                <>
                  <h3 className="drawer-section-title">Approval needed</h3>
                  <div className="drawer-panel drawer-panel--attention">
                    <p className="hint">
                      This draft used the generic fallback template — personalizing it failed even after trying
                      the prospect's cached research and a live web search for more information. Click
                      "Re-draft" to try again, edit it manually, or approve it as-is to move it into the Pending
                      column.
                    </p>
                    <div className="action-row">
                      <button type="button" className="mint" onClick={handleApprove} disabled={approving}>
                        <Icon name="check" size={14} />
                        {approving ? "Approving…" : "Approve → move to Pending"}
                      </button>
                    </div>
                  </div>
                </>
              )}

              {selected.stage === "pending" && (
                <>
                  <h3 className="drawer-section-title">Schedule send</h3>
                  <div className="drawer-panel">
                    <p className="hint">
                      Sets a target time for reference — sending still requires clicking "Send pending" on the
                      campaign; nothing fires automatically yet.
                    </p>
                    <div className="field-row">
                      <div className="field-group">
                        <input
                          type="datetime-local"
                          value={scheduleValue}
                          onChange={(e) => setScheduleValue(e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="action-row">
                      <button type="button" onClick={() => handleSchedule(selected)} disabled={scheduling}>
                        <Icon name="clock" size={14} />
                        {scheduling ? "Saving…" : selected.scheduled_at ? "Update schedule" : "Set schedule"}
                      </button>
                      {selected.scheduled_at && (
                        <button
                          type="button"
                          onClick={() => {
                            setScheduleValue("");
                            handleSchedule({ ...selected, scheduled_at: null });
                          }}
                          disabled={scheduling}
                        >
                          Clear
                        </button>
                      )}
                    </div>
                  </div>
                </>
              )}

              {selected.stage !== "pending" && selected.stage !== "needs_review" && (
                <>
                  <h3 className="drawer-section-title">Advance stage</h3>
                  <div className="drawer-panel">
                    <p className="hint">
                      Manual for now — this agent's job stops at Sent. Set by a person until a reply-tracking
                      agent exists.
                    </p>
                    <div className="action-row">
                      {MANUAL_STAGES.map((stage) => (
                        <button
                          key={stage}
                          type="button"
                          disabled={selected.stage === stage}
                          onClick={() => advanceStage(selected, stage)}
                        >
                          {STAGE_LABEL[stage]}
                        </button>
                      ))}
                    </div>
                  </div>
                </>
              )}

              <h3 className="drawer-section-title">Danger zone</h3>
              <div className="drawer-panel drawer-panel--danger">
                <button type="button" className="danger" onClick={() => setConfirmDeleteItem(true)} disabled={deletingItem}>
                  <Icon name="trash" size={14} />
                  {deletingItem ? "Removing…" : "Remove from campaign"}
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {confirmDeleteItem && selected && (
        <ConfirmModal
          title="Remove prospect?"
          message={`Remove ${prospectByDomain.get(selected.research_domain)?.name || selected.research_domain} from this campaign? This cannot be undone.`}
          danger
          confirmLabel="Remove"
          onConfirm={handleDeleteItem}
          onCancel={() => setConfirmDeleteItem(false)}
        />
      )}

      {campaignScheduleOpen && (
        <div className="drawer-overlay" onClick={() => setCampaignScheduleOpen(false)}>
          <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Schedule campaign</h3>
            <p className="hint">
              Sets a target send time on all {pendingItems.length} pending prospect{pendingItems.length === 1 ? "" : "s"}
              {" "}— informational only for now, sending still requires clicking "Send pending".
            </p>
            <div className="field-row">
              <div className="field-group">
                <input
                  type="datetime-local"
                  value={campaignScheduleValue}
                  onChange={(e) => setCampaignScheduleValue(e.target.value)}
                />
              </div>
            </div>
            <div className="action-row">
              <button
                type="button"
                className="mint"
                onClick={handleScheduleCampaign}
                disabled={campaignScheduling || pendingItems.length === 0}
              >
                <Icon name="clock" size={14} />
                {campaignScheduling ? "Saving…" : "Set schedule"}
              </button>
              <button type="button" onClick={() => setCampaignScheduleOpen(false)} disabled={campaignScheduling}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {renameOpen && (
        <div className="drawer-overlay" onClick={() => setRenameOpen(false)}>
          <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Rename campaign</h3>
            <div className="field-row">
              <div className="field-group">
                <input
                  type="text"
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleRename();
                  }}
                  placeholder="Campaign name"
                />
              </div>
            </div>
            {renameError && <p className="error-text">{renameError}</p>}
            <div className="action-row">
              <button
                type="button"
                className="mint"
                onClick={handleRename}
                disabled={renaming || !renameValue.trim()}
              >
                <Icon name="check" size={14} />
                {renaming ? "Saving…" : "Save"}
              </button>
              <button type="button" onClick={() => setRenameOpen(false)} disabled={renaming}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {activeJob && busy && <RunOverlay job={activeJob} />}

      {confirmDelete && (
        <ConfirmModal
          title="Delete campaign?"
          message={`Delete campaign "${displayName || campaignId}" and all its items? This cannot be undone.`}
          danger
          confirmLabel="Delete"
          onConfirm={confirmDeleteCampaign}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </>
  );
}

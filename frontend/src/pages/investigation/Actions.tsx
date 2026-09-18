import { Briefcase, CircleArrowUp, CircleCheck, CircleX } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { useApprove, useEscalate, useReject } from "@/api/queries";
import type { MatchDetail } from "@/api/types";
import { useIsAdmin } from "@/auth/AuthProvider";
import { StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { FieldError, Input, Label, Textarea } from "@/components/ui/form";
import { DialogContent, DialogRoot } from "@/components/ui/overlay";
import { Card, CardBody, CardHeader, Kbd } from "@/components/ui/surface";
import { formatDate, formatDateTime } from "@/lib/format";

import { providerName } from "./fields";

export type ActionKind = "approve" | "reject" | "escalate";

const DEFAULT_MONTHS = 3;

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** What the current user may do with this result, given its state. */
export function allowedActions(detail: MatchDetail, isAdmin: boolean): Set<ActionKind> {
  const out = new Set<ActionKind>();
  if (detail.superseded_by) return out;
  const status = detail.review_status;
  if (status === "PENDING") {
    out.add("reject");
    out.add("escalate");
    if (isAdmin && detail.candidates.length > 0) out.add("approve");
  } else if (status === "ESCALATED" && isAdmin) {
    out.add("reject");
    if (detail.candidates.length > 0) out.add("approve");
  }
  return out;
}

export function ActionsPanel({
  detail,
  selectedProvider,
  open,
  onOpenChange,
}: {
  detail: MatchDetail;
  selectedProvider: string | null;
  open: ActionKind | null;
  onOpenChange: (kind: ActionKind | null) => void;
}) {
  const isAdmin = useIsAdmin();
  const allowed = allowedActions(detail, isAdmin);
  const reviewed = detail.review_status === "APPROVED" || detail.review_status === "REJECTED";

  return (
    <Card>
      <CardHeader title="Verdict" description={reviewed ? "This result has been decided." : "Your decision is audited and becomes training data."} />
      <CardBody className="space-y-3">
        {detail.review_status !== "PENDING" ? <VerdictSummary detail={detail} /> : null}

        {allowed.size > 0 ? (
          <div className="flex flex-wrap gap-2">
            {allowed.has("approve") ? (
              <Button variant="good" onClick={() => onOpenChange("approve")} aria-keyshortcuts="a">
                <CircleCheck /> Approve… <Kbd>a</Kbd>
              </Button>
            ) : null}
            {allowed.has("reject") ? (
              <Button onClick={() => onOpenChange("reject")} aria-keyshortcuts="r">
                <CircleX /> Reject… <Kbd>r</Kbd>
              </Button>
            ) : null}
            {allowed.has("escalate") ? (
              <Button onClick={() => onOpenChange("escalate")} aria-keyshortcuts="e">
                <CircleArrowUp /> Escalate… <Kbd>e</Kbd>
              </Button>
            ) : null}
          </div>
        ) : !reviewed && !detail.superseded_by ? (
          <p className="text-sm text-muted">
            {detail.review_status === "ESCALATED" ? "Escalated to an admin, who decides it now." : "No action is available to you here."}
          </p>
        ) : null}
        {!isAdmin && detail.review_status === "PENDING" ? (
          <p className="text-xs text-muted">Approval opens a compliance case, so only an admin can approve. Escalate to hand it up.</p>
        ) : null}
      </CardBody>

      <ApproveDialog detail={detail} providerId={selectedProvider} open={open === "approve"} onClose={() => onOpenChange(null)} />
      <CommentDialog kind="reject" detail={detail} providerId={selectedProvider} open={open === "reject"} onClose={() => onOpenChange(null)} />
      <CommentDialog kind="escalate" detail={detail} providerId={null} open={open === "escalate"} onClose={() => onOpenChange(null)} />
    </Card>
  );
}

function VerdictSummary({ detail }: { detail: MatchDetail }) {
  return (
    <div className="space-y-2 rounded-md bg-surface-2 p-3 text-sm">
      <div className="flex items-center gap-2">
        <StatusBadge status={detail.review_status} />
        <span className="text-xs text-muted">
          by {detail.reviewed_by_email ?? "unknown"} · {formatDateTime(detail.reviewed_at)}
        </span>
      </div>
      {detail.reviewer_comment ? <p className="text-ink-2">“{detail.reviewer_comment}”</p> : null}
      {detail.approved_provider_id && detail.approved_provider_id !== detail.chosen_provider_id ? (
        <p className="text-xs text-ink-2">
          The reviewer chose <span className="font-mono">{detail.approved_provider_id}</span> over the engine's{" "}
          <span className="font-mono">{detail.chosen_provider_id ?? "none"}</span>.
        </p>
      ) : null}
      {(detail.cases ?? []).map((c) => (
        <Link key={c.id} to={`/cases/${c.id}`} className="flex items-center gap-2 text-xs text-accent hover:underline">
          <Briefcase className="size-3.5" /> {c.case_number} · {c.status.toLowerCase()} · {formatDate(c.start_date)} – {formatDate(c.end_date)}
        </Link>
      ))}
    </div>
  );
}

function ApproveDialog({ detail, providerId, open, onClose }: { detail: MatchDetail; providerId: string | null; open: boolean; onClose: () => void }) {
  const approve = useApprove(detail.id);
  const navigate = useNavigate();
  const [months, setMonths] = useState(String(DEFAULT_MONTHS));
  const [start, setStart] = useState(today());
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  const candidate = detail.candidates.find((c) => c.provider_id === providerId);
  const ambiguous = detail.decision === "AMBIGUOUS";
  const differs = providerId !== detail.chosen_provider_id;
  const monthsValue = Number(months);
  const monthsValid = Number.isInteger(monthsValue) && monthsValue >= 1 && monthsValue <= 60;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!monthsValid || !providerId) return;
    setError(null);
    try {
      const result = await approve.mutateAsync({
        provider_id: ambiguous || differs ? providerId : undefined,
        duration_months: monthsValue,
        start_date: start || undefined,
        comment: comment.trim() || undefined,
      });
      onClose();
      toast.success(result.case_created ? `Approved. Case ${result.case.case_number} opened.` : "Approved. An active case already covered this provider.", {
        action: { label: "Open case", onClick: () => navigate(`/cases/${result.case.id}`) },
      });
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "already_approved") {
        setError(`Already approved; case ${String(cause.details.case_number ?? "")} was opened then. Nothing new was created.`);
      } else {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
  };

  return (
    <DialogRoot open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Approve and open a case" description="Confirms the match and opens a compliance case for the chosen provider, in one step.">
        <form onSubmit={submit} className="space-y-4">
          <div className="rounded-md bg-surface-2 p-3 text-sm">
            <p className="text-xs text-muted">Provider</p>
            <p className="font-medium text-ink">
              {candidate ? providerName(candidate.provider) : "No candidate selected"}{" "}
              <span className="font-mono text-xs text-muted">{providerId}</span>
            </p>
            {differs && candidate ? (
              <p className="mt-1 text-xs text-warning-text">
                Not the engine's choice. Both are kept: your pick as approved, the engine's as proposed.
              </p>
            ) : null}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="case-months">Case duration (months)</Label>
              <Input
                id="case-months"
                type="number"
                min={1}
                max={60}
                value={months}
                onChange={(e) => setMonths(e.target.value)}
                aria-invalid={!monthsValid}
              />
              <FieldError>{monthsValid ? null : "Between 1 and 60 months."}</FieldError>
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-start">Start date</Label>
              <Input id="case-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="approve-comment">Comment (optional)</Label>
            <Textarea id="approve-comment" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="What convinced you" />
          </div>
          {error ? (
            <p role="alert" className="text-sm text-critical-text">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="good" loading={approve.isPending} disabled={!providerId || !monthsValid}>
              <CircleCheck /> Approve and open case
            </Button>
          </div>
        </form>
      </DialogContent>
    </DialogRoot>
  );
}

function CommentDialog({
  kind,
  detail,
  providerId,
  open,
  onClose,
}: {
  kind: "reject" | "escalate";
  detail: MatchDetail;
  providerId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const reject = useReject(detail.id);
  const escalate = useEscalate(detail.id);
  const mutation = kind === "reject" ? reject : escalate;
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = comment.trim();
    if (!text) return;
    setError(null);
    try {
      if (kind === "reject") await reject.mutateAsync({ comment: text, provider_id: providerId });
      else await escalate.mutateAsync({ comment: text });
      setComment("");
      onClose();
      toast.success(kind === "reject" ? "Rejected" : "Escalated to an admin");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <DialogRoot open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={kind === "reject" ? "Reject this match" : "Escalate to an admin"}
        description={
          kind === "reject"
            ? `Records that ${providerId ?? "the proposed provider"} is not the sanctioned party. Saved as a training label.`
            : "Hands the decision to an admin. Say what made it hard."
        }
      >
        <form onSubmit={submit} className="space-y-3">
          <Textarea autoFocus aria-label="Comment" required value={comment} onChange={(e) => setComment(e.target.value)} placeholder="A comment is required" />
          {error ? (
            <p role="alert" className="text-sm text-critical-text">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant={kind === "reject" ? "danger" : "primary"} loading={mutation.isPending} disabled={!comment.trim()}>
              {kind === "reject" ? "Reject" : "Escalate"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </DialogRoot>
  );
}

"use client";

import { useEffect, useState } from "react";
import { RotateCcw, Square, Timer, X } from "lucide-react";
import { api, Job, relativeTime } from "@/lib/api";
import { EmptyState, ProgressBar, StatusBadge } from "@/components/studio/ui";

function etaLabel(job: Job): string | null {
  if (job.state !== "running" || job.progress <= 0) return null;
  const started = job.started_at ? new Date(job.started_at).getTime() : NaN;
  if (!Number.isFinite(started)) return null;
  const elapsedSeconds = (Date.now() - started) / 1000;
  if (elapsedSeconds < 3) return null;
  const remaining = (elapsedSeconds / job.progress) * (100 - job.progress);
  if (remaining < 5) return null;
  if (remaining < 60) return `~${Math.round(remaining)}s left`;
  return `~${Math.floor(remaining / 60)}m ${Math.round(remaining % 60)}s left`;
}

export function JobRow({
  job, onChanged, onToast, compact = false, collapsedFinished = false,
}: { job: Job; onChanged: () => void; onToast: (message: string) => void; compact?: boolean; collapsedFinished?: boolean }) {
  const [busy, setBusy] = useState(false);
  const active = job.state === "queued" || job.state === "running";
  const retryable = job.state === "failed" || job.state === "cancelled";
  const finished = !active && !retryable;
  const eta = etaLabel(job);
  const collapsed = collapsedFinished && finished;

  async function act(action: "cancel" | "retry") {
    setBusy(true);
    try {
      if (action === "cancel") await api.cancelJob(job.id);
      else await api.retryJob(job.id);
      onToast(action === "cancel" ? "Cancellation requested." : "Retry queued.");
      onChanged();
    } catch (cause) {
      onToast(cause instanceof Error ? cause.message : "The job action failed.");
    } finally { setBusy(false); }
  }

  if (collapsed) {
    return (
      <div className={`st-job compact collapsed ${job.state}`}>
        <div className="st-job-main">
          <div className="st-job-title">
            <strong>{job.project_name ?? "Project"}</strong>
            <span className="st-kind">{job.kind ?? "ingestion"}</span>
            <StatusBadge state={job.state} />
          </div>
        </div>
        <div className="st-job-side">
          <span className="st-job-time">{relativeTime(job.created_at)}</span>
          {retryable && <button className="st-mini-button" disabled={busy} onClick={() => void act("retry")}><RotateCcw size={12} /> Retry</button>}
          {!retryable && <span className="st-job-done"><Square size={9} /> done</span>}
        </div>
      </div>
    );
  }

  return (
    <div className={`st-job ${compact ? "compact" : ""} ${job.state}`}>
      <div className="st-job-main">
        <div className="st-job-title">
          <strong>{job.project_name ?? "Project"}</strong>
          <span className="st-kind">{job.kind ?? "ingestion"}</span>
          <StatusBadge state={job.state} />
        </div>
        <span className="st-job-stage">{job.stage}{job.error ? ` · ${job.error}` : ""}</span>
        {active && <ProgressBar value={job.progress} state={job.state} />}
      </div>
      <div className="st-job-side">
        <span className="st-job-time">{relativeTime(job.created_at)}</span>
        {eta && <span className="st-job-eta"><Timer size={10} /> {eta}</span>}
        {active && <button className="st-mini-button" disabled={busy} onClick={() => void act("cancel")}><X size={12} /> Cancel</button>}
        {retryable && <button className="st-mini-button" disabled={busy} onClick={() => void act("retry")}><RotateCcw size={12} /> Retry</button>}
        {finished && <span className="st-job-done"><Square size={9} /> done</span>}
      </div>
    </div>
  );
}

export function JobList({
  jobs, onChanged, onToast, emptyLabel = "No jobs have run yet.", limit,
}: {
  jobs: Job[]; onChanged: () => void; onToast: (message: string) => void; emptyLabel?: string; limit?: number;
}) {
  const visible = limit ? jobs.slice(0, limit) : jobs;
  if (!visible.length) {
    return <EmptyState icon={<RotateCcw size={19} />} title={emptyLabel}>Jobs appear here as soon as local processing starts.</EmptyState>;
  }
  return (
    <div className="st-job-list">
      {visible.map((job) => <JobRow key={job.id} job={job} onChanged={onChanged} onToast={onToast} compact />)}
    </div>
  );
}

export function QueueView({ onToast }: { onToast: (message: string) => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [filter, setFilter] = useState("all");
  const [hideFinished, setHideFinished] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async (quiet = false) => {
    try {
      setJobs(await api.jobs());
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not read the job queue.");
    } finally { if (!quiet) setLoading(false); }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const activeCount = jobs.filter((job) => job.state === "queued" || job.state === "running").length;
  const filters = ["all", "active", "completed", "failed", "cancelled"];
  const visible = jobs.filter((job) => {
    if (filter === "all") return true;
    if (filter === "active") return job.state === "queued" || job.state === "running";
    return job.state === filter;
  });
  const shown = hideFinished && filter === "all"
    ? visible.filter((job) => !["completed", "cancelled"].includes(job.state) || job.state === "failed")
    : visible;

  return (
    <div className="st-stack">
      <div className="st-section-head">
        <div>
          <h1>Processing queue<span className="heading-period">.</span></h1>
          <p>{activeCount ? `${activeCount} local job${activeCount === 1 ? "" : "s"} running. Heavy inference is serialized on this laptop.` : "Nothing is running. Every local job is recorded here."}</p>
        </div>
        <span className={`st-live ${activeCount ? "on" : ""}`}><span /> {activeCount ? "LIVE" : "IDLE"}</span>
      </div>

      <div className="st-toolbar">
        <div className="st-tabs">
          {filters.map((item) => (
            <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>
              {item}{item === "all" ? ` (${jobs.length})` : ""}
            </button>
          ))}
        </div>
        <div className="st-toolbar-actions">
          <button className={`secondary-button sm ${hideFinished ? "active" : ""}`} onClick={() => setHideFinished(!hideFinished)}>
            {hideFinished ? "Show finished" : "Hide finished"}
          </button>
          <button className="secondary-button sm" onClick={() => void load()}>Refresh</button>
        </div>
      </div>

      {error && <div className="st-note st-note-error"><span>{error}</span></div>}
      {loading ? <div className="st-note">Reading the local job queue…</div> : (
        <div className="st-stack-sm">
          {shown.map((job) => <JobRow key={job.id} job={job} collapsedFinished={hideFinished} onChanged={() => void load(true)} onToast={onToast} />)}
          {!shown.length && <EmptyState icon={<RotateCcw size={19} />} title={filter === "all" ? "The queue is empty" : `No ${filter} jobs`}>Start ingestion, transcription, highlights or a render to see live progress.</EmptyState>}
        </div>
      )}
    </div>
  );
}

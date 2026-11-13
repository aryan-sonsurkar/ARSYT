"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowRight, Check, Clapperboard, Download, Film, LoaderCircle,
  Music2, Package, PencilLine, Plus, RefreshCw, Search, Sparkles, Trash2, Upload, WandSparkles, X,
} from "lucide-react";
import {
  api, AppSettings, Candidate, Clip, ExportResult, formatBytes, formatTimestamp, Job, ProjectDetail, relativeTime,
} from "@/lib/api";
import { TranscriptionPanel } from "@/components/transcription-panel";
import { ClipCard } from "@/components/studio/clip-card";
import { JobList } from "@/components/studio/job-list";
import { Busy, EmptyState, Field, InlineNote, Panel, ProgressBar, StatusBadge } from "@/components/studio/ui";

type Props = {
  projectId: string;
  onBack: () => void;
  onChanged: () => void;
  onToast: (message: string) => void;
};

type Tab = "overview" | "highlights" | "clips";
type HighlightForm = { model: string; count: number; min_duration: number; max_duration: number };
type CandidateEdit = { id: string; title: string; hook: string; reason: string };

const SCORE_KEYS = ["hook", "emotion", "clarity", "context", "value"];

export function ProjectDetailView({ projectId, onBack, onChanged, onToast }: Props) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [highlightForm, setHighlightForm] = useState<HighlightForm>({ model: "", count: 5, min_duration: 25, max_duration: 60 });
  const [selectedCandidates, setSelectedCandidates] = useState<Set<string>>(new Set());
  const [candidateSearch, setCandidateSearch] = useState("");
  const [editing, setEditing] = useState<CandidateEdit | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [manual, setManual] = useState({ title: "", start: 0, end: 30 });
  const [projectExport, setProjectExport] = useState<ExportResult | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const initialized = useRef(false);

  const load = useCallback(async (quiet = false) => {
    try {
      const [project, config] = await Promise.all([api.project(projectId), api.settings()]);
      setDetail(project);
      setSettings(config);
      setError("");
      if (!initialized.current) {
        initialized.current = true;
        setHighlightForm({
          model: project.settings.highlight_model,
          count: Math.round(project.settings.clip_count),
          min_duration: project.settings.min_clip_seconds,
          max_duration: project.settings.max_clip_seconds,
        });
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load this project.");
    } finally { if (!quiet) setLoading(false); }
  }, [projectId]);

  useEffect(() => { initialized.current = false; setDetail(null); setLoading(true); setTab("overview"); void load(); }, [load]);
  useEffect(() => {
    if (!detail) return;
    const active = detail.jobs.some((job) => job.state === "queued" || job.state === "running")
      || detail.clips.some((clip) => clip.state === "queued" || clip.state === "rendering");
    if (!active) return;
    const timer = window.setInterval(() => void load(true), 2800);
    return () => window.clearInterval(timer);
  }, [detail, load]);

  const metadata = useMemo(() => {
    try { return detail?.metadata_json ? JSON.parse(detail.metadata_json) : {}; }
    catch { return {}; }
  }, [detail?.metadata_json]);

  const candidates = useMemo(() => (detail?.candidates ?? []).filter((item) =>
    `${item.title} ${item.category} ${item.excerpt}`.toLowerCase().includes(candidateSearch.toLowerCase())), [detail?.candidates, candidateSearch]);
  const approvedCandidates = (detail?.candidates ?? []).filter((item) => item.state === "approved");
  const sourceReady = detail?.status === "source_ready";
  const activeJobs = detail?.jobs.filter((job) => job.state === "queued" || job.state === "running") ?? [];

  function toastAndRefresh(message: string) {
    onToast(message);
    onChanged();
    void load(true);
  }

  async function act<T>(key: string, action: () => Promise<T>, success: string): Promise<T | null> {
    setBusy(key); setError("");
    try {
      const result = await action();
      toastAndRefresh(success);
      return result;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "The action failed.";
      setError(message);
      onToast(message);
      return null;
    } finally { setBusy(""); }
  }

  async function startHighlights() {
    await act("highlights", () => api.startHighlights(projectId, highlightForm), "Highlight detection started with local Ollama.");
  }

  async function candidateAction(candidateId: string, action: "approve" | "reject" | "reset") {
    await act(`candidate-${candidateId}`, () => api.candidateAction(candidateId, action), `Candidate ${action === "approve" ? "approved — a draft clip was created" : action + "d"}.`);
  }

  async function batchApprove() {
    const ids = [...selectedCandidates];
    if (!ids.length) return;
    const result = await act("batch-approve", () => api.batchApprove(ids), `${ids.length} candidate${ids.length === 1 ? "" : "s"} approved.`);
    if (result) setSelectedCandidates(new Set());
  }

  async function clipsFromApproved() {
    await act("from-approved", () => api.clipsFromApproved(projectId), "Draft clips created from every approved candidate.");
  }

  async function saveCandidate() {
    if (!editing) return;
    await act("save-candidate", () => api.updateCandidate(editing.id, {
      title: editing.title, hook: editing.hook, reason: editing.reason,
    }), "Candidate updated.");
    setEditing(null);
  }

  async function createManualClip() {
    if (manual.end <= manual.start) { setError("The clip end must be after its start."); return; }
    await act("manual-clip", () => api.createClip(projectId, {
      title: manual.title || undefined, start_time: manual.start, end_time: manual.end,
    }), "Draft clip created from your timestamps.");
    setManualOpen(false);
    setManual({ title: "", start: 0, end: 30 });
    setTab("clips");
  }

  async function deleteProject() {
    if (!detail) return;
    if (!window.confirm(`Delete “${detail.name}” with its source, transcript, clips and exports? This cannot be undone.`)) return;
    await act("delete", () => api.deleteProject(projectId), "Project deleted.");
    onBack();
  }

  async function exportProject() {
    const result = await act("export", () => api.exportProject(projectId), "Project ZIP exported locally.");
    if (result) setProjectExport(result);
  }

  async function uploadMusic(file: File | null) {
    if (!file) return;
    await act("music", () => api.uploadMusic(projectId, file), `Stored ${file.name} in the project's local music folder.`);
  }

  async function removeMusic(name: string) {
    if (!window.confirm(`Remove ${name} from this project?`)) return;
    await act(`music-${name}`, () => api.deleteMusic(projectId, name), "Music file removed.");
  }

  async function copySourceUrl() {
    if (!detail) return;
    const url = api.sourceDownloadUrl(projectId);
    try { await navigator.clipboard.writeText(url); setSourceUrl(url); onToast("Local source URL copied."); }
    catch { setSourceUrl(url); }
  }

  if (loading) return <div className="st-loading"><LoaderCircle size={18} className="spin" /> Loading the local project…</div>;
  if (!detail) return <div className="st-stack"><InlineNote tone="error">{error || "Project not found."}</InlineNote><button className="secondary-button" onClick={onBack}><ArrowLeft size={14} /> Back to projects</button></div>;

  const renderable = detail.clips.filter((clip) => clip.state === "draft" || clip.state === "failed");
  const allCandidatesSelected = candidates.length > 0 && candidates.every((item) => selectedCandidates.has(item.id));

  return (
    <div className="st-stack">
      <div className="st-detail-top">
        <button className="st-back" onClick={onBack}><ArrowLeft size={15} /> All projects</button>
        <div className="st-detail-actions">
          <button className="secondary-button sm" onClick={() => void load()}><RefreshCw size={13} /> Refresh</button>
          <button className="secondary-button sm" disabled={!detail.clips.some((clip) => clip.state === "rendered") || busy === "export"} onClick={() => void exportProject()}>
            {busy === "export" ? <LoaderCircle size={13} className="spin" /> : <Package size={13} />} Export ZIP
          </button>
          <button className="st-danger-button" disabled={busy === "delete"} onClick={() => void deleteProject()}><Trash2 size={13} /> Delete</button>
        </div>
      </div>

      <div className="st-detail-head">
        <div>
          <div className="st-detail-eyebrow">
            <StatusBadge state={detail.status} />
            <span>{detail.source_filename || "No source yet"}</span>
            <span>· updated {relativeTime(detail.updated_at)}</span>
          </div>
          <h1>{detail.name}<span className="heading-period">.</span></h1>
          <p>{metadata.duration_seconds ? `${formatTimestamp(metadata.duration_seconds)} source · ${metadata.video?.width ?? "?"} × ${metadata.video?.height ?? "?"} · ${formatBytes(metadata.file_size_bytes ?? detail.source_size_bytes ?? 0)}` : "Add a local recording to begin."}</p>
        </div>
        <div className="st-detail-stats">
          <span><strong>{detail.candidates.length}</strong> candidates</span>
          <span><strong>{detail.clips.filter((clip) => clip.state === "rendered").length}</strong> rendered</span>
          <span><strong>{detail.transcript?.word_count ?? 0}</strong> words</span>
        </div>
      </div>

      {error && <InlineNote tone="error">{error}</InlineNote>}
      {projectExport?.path && (
        <div className="st-export-banner">
          <Package size={16} />
          <div><strong>{projectExport.filename}</strong><span>{projectExport.clip_count} rendered clips packaged locally.</span></div>
          <a className="secondary-button sm" href={api.projectExportUrl(projectId, projectExport.filename ?? "")}><Download size={13} /> Download ZIP</a>
        </div>
      )}

      <div className="st-tabs st-detail-tabs">
        <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>Overview</button>
        <button className={tab === "highlights" ? "active" : ""} onClick={() => setTab("highlights")}>Highlights <span>{detail.candidates.length}</span></button>
        <button className={tab === "clips" ? "active" : ""} onClick={() => setTab("clips")}>Clips <span>{detail.clips.length}</span></button>
      </div>

      {tab === "overview" && (
        <>
          <div className="st-source-grid">
            <Panel title="Source media" subtitle="The original file is preserved inside this project.">
              {sourceReady ? (
                <div className="st-source">
                  <div className="st-source-icon"><Film size={20} /></div>
                  <div className="st-source-copy">
                    <strong>{detail.source_filename || detail.source_title || "source video"}</strong>
                    <span>{metadata.format ? `${metadata.format.split(",")[0]} · ` : ""}{metadata.video?.codec ?? "video"} · {metadata.audio?.available ? "audio ready" : "no audio"}</span>
                    <span className="st-mono st-path">{detail.source_path}</span>
                  </div>
                  <div className="st-source-actions">
                    <a className="secondary-button sm" href={api.sourceDownloadUrl(projectId)}><Download size={13} /> Source</a>
                    <button className="secondary-button sm" onClick={() => void copySourceUrl()}>Copy local URL</button>
                  </div>
                </div>
              ) : (
                <EmptyState icon={<Upload size={19} />} title="No playable source yet">
                  Delete this project and create a new one with a local video, or retry a failed ingestion job below.
                </EmptyState>
              )}
              {sourceUrl && <div className="st-note">Local stream URL: <code>{sourceUrl}</code></div>}
            </Panel>

            <Panel title="Pipeline" subtitle="Each step runs locally and can be retried safely.">
              <div className="st-pipeline">
                <span className={sourceReady ? "done" : ""}><i />Source</span>
                <span className={detail.transcript ? "done" : ""}><i />Transcript</span>
                <span className={detail.candidates.length ? "done" : ""}><i />Highlights</span>
                <span className={detail.clips.some((clip) => clip.state === "rendered") ? "done" : ""}><i />Rendered</span>
              </div>
              <div className="st-pipeline-meta">
                {detail.transcript ? (
                  <>
                    <span>{detail.transcript.language?.toUpperCase()} · {detail.transcript.segment_count} segments · {detail.transcript.word_count} words</span>
                    <span>{detail.transcript.model_name} on {detail.transcript.device} · RTF {Number(detail.transcript.real_time_factor).toFixed(2)}×</span>
                  </>
                ) : <span>No saved transcript yet. Use the panel below to run faster-whisper.</span>}
              </div>
              {activeJobs.length > 0 && (
                <div className="st-active-job">
                  <div><strong>{activeJobs[0].stage}</strong><span>{activeJobs[0].project_name ?? detail.name} · {activeJobs[0].progress}%</span></div>
                  <ProgressBar value={activeJobs[0].progress} state={activeJobs[0].state} />
                </div>
              )}
            </Panel>
          </div>

          <TranscriptionPanel project={detail} onJobChange={() => { void load(true); onChanged(); }} />

          <Panel title={<><Music2 size={15} /> Background music</>} subtitle="Optional local audio. It is looped and ducked under speech during render.">
            <div className="st-music-row">
              <label className="secondary-button sm file-picker">
                <Upload size={13} /> Add music
                <input type="file" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac,audio/*" onChange={(event) => void uploadMusic(event.target.files?.[0] ?? null)} />
              </label>
              <div className="st-music-list">
                {detail.music_files?.map((file) => (
                  <span className="st-music-chip" key={file.path}>
                    <Music2 size={12} />{file.name}<span>{formatBytes(file.size_bytes)}</span>
                    <button onClick={() => void removeMusic(file.name)} aria-label={`Remove ${file.name}`}><X size={11} /></button>
                  </span>
                ))}
                {!detail.music_files?.length && <span className="st-muted">No local music uploaded yet.</span>}
              </div>
            </div>
          </Panel>

          <Panel title="Recent activity" subtitle="Cancel or retry any job in this project.">
            <JobList jobs={detail.jobs} limit={6} onChanged={() => { void load(true); onChanged(); }} onToast={onToast} emptyLabel="No jobs for this project yet." />
          </Panel>
        </>
      )}

      {tab === "highlights" && (
        <>
          <Panel
            title={<><Sparkles size={15} /> Find highlight moments</>}
            subtitle="Local Ollama reads timestamped transcript chunks and returns ranked, validated moments."
            actions={approvedCandidates.length ? <button className="secondary-button sm" onClick={() => void clipsFromApproved()} disabled={Boolean(busy)}><Clapperboard size={13} /> Clips from approved</button> : undefined}
          >
            <div className="st-highlight-form">
              <Field label="Ollama model">
                <select value={highlightForm.model} onChange={(event) => setHighlightForm({ ...highlightForm, model: event.target.value })}>
                  {(settings?.ollama.models ?? []).map((model) => <option key={model} value={model}>{model}</option>)}
                  {!settings?.ollama.models.length && <option value={highlightForm.model}>{highlightForm.model || "gemma3:4b"}</option>}
                </select>
              </Field>
              <Field label="Clips"><input className="form-input" type="number" min={1} max={20} value={highlightForm.count} onChange={(event) => setHighlightForm({ ...highlightForm, count: Number(event.target.value) })} /></Field>
              <Field label="Min seconds"><input className="form-input" type="number" min={8} max={120} value={highlightForm.min_duration} onChange={(event) => setHighlightForm({ ...highlightForm, min_duration: Number(event.target.value) })} /></Field>
              <Field label="Max seconds"><input className="form-input" type="number" min={12} max={180} value={highlightForm.max_duration} onChange={(event) => setHighlightForm({ ...highlightForm, max_duration: Number(event.target.value) })} /></Field>
              <button className="primary-button" disabled={!detail.transcript || Boolean(busy)} onClick={() => void startHighlights()}>
                {busy === "highlights" ? <Busy label="Starting…" /> : <><WandSparkles size={15} /> Detect highlights</>}
              </button>
            </div>
            {!detail.transcript && <InlineNote tone="warn">Transcribe this project first — highlight detection needs timestamped transcript segments.</InlineNote>}
          </Panel>

          <div className="st-toolbar">
            <label className="search-box"><Search size={14} /><input placeholder="Search candidates" value={candidateSearch} onChange={(event) => setCandidateSearch(event.target.value)} /></label>
            <div className="st-toolbar-actions">
              <button className="secondary-button sm" disabled={!candidates.length} onClick={() => setSelectedCandidates(allCandidatesSelected ? new Set() : new Set(candidates.map((item) => item.id)))}>{allCandidatesSelected ? "Clear" : "Select all"}</button>
              <button className="primary-button sm" disabled={!selectedCandidates.size || Boolean(busy)} onClick={() => void batchApprove()}>
                {busy === "batch-approve" ? <LoaderCircle size={13} className="spin" /> : <Check size={13} />} Approve selected
              </button>
            </div>
          </div>

          {candidates.length ? (
            <div className="st-candidate-list">
              {candidates.map((candidate) => (
                <CandidateCard
                  key={candidate.id}
                  candidate={candidate}
                  checked={selectedCandidates.has(candidate.id)}
                  busy={Boolean(busy)}
                  editing={editing?.id === candidate.id ? editing : null}
                  onCheck={() => setSelectedCandidates((current) => {
                    const next = new Set(current);
                    if (next.has(candidate.id)) next.delete(candidate.id); else next.add(candidate.id);
                    return next;
                  })}
                  onEdit={() => setEditing({ id: candidate.id, title: candidate.title, hook: candidate.hook, reason: candidate.reason })}
                  onCancelEdit={() => setEditing(null)}
                  onChangeEdit={setEditing}
                  onSaveEdit={() => void saveCandidate()}
                  onAction={(action) => void candidateAction(candidate.id, action)}
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={<Sparkles size={20} />} title={detail.candidates.length ? "No candidates match your search" : "No highlight candidates yet"}>
              {detail.candidates.length ? "Try another search." : "Run highlight detection to let the local model propose ranked moments from the transcript."}
            </EmptyState>
          )}
        </>
      )}

      {tab === "clips" && (
        <>
          <div className="st-toolbar">
            <div className="st-toolbar-title"><Clapperboard size={15} /><strong>{detail.clips.length} clips</strong><span>{renderable.length} ready to render</span></div>
            <div className="st-toolbar-actions">
              {approvedCandidates.length > 0 && <button className="secondary-button sm" onClick={() => void clipsFromApproved()} disabled={Boolean(busy)}><Plus size={13} /> From approved</button>}
              <button className="secondary-button sm" onClick={() => setManualOpen((open) => !open)}><Plus size={13} /> Manual clip</button>
            </div>
          </div>

          {manualOpen && (
            <Panel title="Create a clip from timestamps" subtitle={`Source duration ${metadata.duration_seconds ? formatTimestamp(metadata.duration_seconds) : "unknown"}. Timestamps snap to transcript segments.`}>
              <div className="st-highlight-form">
                <Field label="Title"><input className="form-input" value={manual.title} onChange={(event) => setManual({ ...manual, title: event.target.value })} placeholder="Optional title" /></Field>
                <Field label="Start seconds"><input className="form-input" type="number" min={0} step={0.1} value={manual.start} onChange={(event) => setManual({ ...manual, start: Number(event.target.value) })} /></Field>
                <Field label="End seconds"><input className="form-input" type="number" min={0} step={0.1} value={manual.end} onChange={(event) => setManual({ ...manual, end: Number(event.target.value) })} /></Field>
                <button className="primary-button" onClick={() => void createManualClip()} disabled={Boolean(busy)}><Plus size={14} /> Create draft</button>
              </div>
            </Panel>
          )}

          {detail.clips.length ? (
            <div className="st-clip-grid">
              {detail.clips.map((clip) => (
                <ClipCard
                  key={clip.id}
                  clip={clip}
                  settings={settings}
                  musicFiles={detail.music_files}
                  onChanged={() => { void load(true); onChanged(); }}
                  onToast={onToast}
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={<Clapperboard size={20} />} title="No clips yet">
              Approve highlight candidates to create draft clips, or add one manually from timestamps.
            </EmptyState>
          )}
        </>
      )}
    </div>
  );
}

function CandidateCard({
  candidate, checked, busy, editing, onCheck, onEdit, onCancelEdit, onChangeEdit, onSaveEdit, onAction,
}: {
  candidate: Candidate;
  checked: boolean;
  busy: boolean;
  editing: CandidateEdit | null;
  onCheck: () => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onChangeEdit: (value: CandidateEdit) => void;
  onSaveEdit: () => void;
  onAction: (action: "approve" | "reject" | "reset") => void;
}) {
  const duration = candidate.end_time - candidate.start_time;
  return (
    <article className={`st-candidate ${candidate.state}`}>
      <label className="st-candidate-check">
        <input type="checkbox" checked={checked} onChange={onCheck} />
        <span />
      </label>
      <div className="st-candidate-score">{Math.round(candidate.overall_score)}<span>score</span></div>
      <div className="st-candidate-main">
        <div className="st-candidate-top">
          <span className="st-category">{candidate.category || "Moment"}</span>
          <span>{formatTimestamp(candidate.start_time)} → {formatTimestamp(candidate.end_time)} · {duration.toFixed(1)}s</span>
          <StatusBadge state={candidate.state} />
        </div>
        {editing ? (
          <div className="st-form st-candidate-edit">
            <Field label="Title"><input className="form-input" value={editing.title} onChange={(event) => onChangeEdit({ ...editing, title: event.target.value })} /></Field>
            <Field label="Hook"><input className="form-input" value={editing.hook} onChange={(event) => onChangeEdit({ ...editing, hook: event.target.value })} /></Field>
            <Field label="Reason"><textarea className="form-input" rows={2} value={editing.reason} onChange={(event) => onChangeEdit({ ...editing, reason: event.target.value })} /></Field>
            <div className="st-inline-actions">
              <button className="primary-button sm" onClick={onSaveEdit} disabled={busy}>Save</button>
              <button className="secondary-button sm" onClick={onCancelEdit}>Cancel</button>
            </div>
          </div>
        ) : (
          <>
            <h3>{candidate.title}</h3>
            {candidate.hook && <p className="st-candidate-hook">“{candidate.hook}”</p>}
            {candidate.reason && <p className="st-candidate-reason">{candidate.reason}</p>}
            {candidate.excerpt && <blockquote>{candidate.excerpt}</blockquote>}
          </>
        )}
        <div className="st-candidate-foot">
          <div className="st-score-row">
            {SCORE_KEYS.map((key) => candidate.scores?.[key] !== undefined && (
              <span key={key}><em>{key}</em>{candidate.scores[key]}</span>
            ))}
          </div>
          <div className="st-candidate-actions">
            {!editing && <button className="st-icon-action" title="Edit candidate" onClick={onEdit}><PencilLine size={13} /></button>}
            {candidate.state !== "approved" && <button className="secondary-button sm" disabled={busy} onClick={() => onAction("approve")}><Check size={13} /> Approve</button>}
            {candidate.state !== "rejected" && <button className="secondary-button sm" disabled={busy} onClick={() => onAction("reject")}><X size={13} /> Reject</button>}
            {candidate.state !== "candidate" && <button className="text-button" disabled={busy} onClick={() => onAction("reset")}>Reset</button>}
          </div>
        </div>
      </div>
    </article>
  );
}

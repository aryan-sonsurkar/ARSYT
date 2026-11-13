"use client";

import { useEffect, useState } from "react";
import { ArrowRight, FolderPlus, Link2, LoaderCircle, Upload } from "lucide-react";
import { api } from "@/lib/api";
import { Modal, InlineNote } from "@/components/studio/ui";

type Props = {
  open: boolean;
  onClose: () => void;
  onCreated: (projectId: string) => void;
  onToast: (message: string) => void;
};

type Mode = "file" | "url" | "empty";

export function NewProjectModal({ open, onClose, onCreated, onToast }: Props) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState<Mode>("file");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setName(""); setMode("file"); setUrl(""); setFile(null);
      setAuthorized(false); setBusy(false); setStage(""); setError("");
    }
  }, [open]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    if (mode === "url" && !authorized) {
      setError("Confirm that you own this media or are authorized to process it.");
      return;
    }
    if (mode === "file" && !file) {
      setError("Choose a local video file, or switch to an empty project.");
      return;
    }
    setBusy(true);
    try {
      setStage("Creating the local project…");
      const project = await api.createProject(name.trim(), mode === "url" ? url.trim() : "");
      if (mode === "file" && file) {
        setStage("Uploading and probing the local video…");
        const job = await api.createJob(project.id);
        await api.uploadSource(project.id, job.id, file);
        onToast("Source video stored locally and audio extracted.");
      } else if (mode === "url") {
        setStage("Queueing the authorized direct-media import…");
        await api.ingestUrl(project.id, url.trim());
        onToast("Authorized URL import queued. Progress is visible in the queue.");
      } else {
        onToast("Project created. Add a source video whenever you are ready.");
      }
      onCreated(project.id);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create the project.");
    } finally {
      setBusy(false);
      setStage("");
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => { if (!busy) onClose(); }}
      title="Create a new project"
      subtitle="Everything stays on this device. Import an owned recording or start an empty edit."
      width={560}
    >
      <form className="st-form" onSubmit={submit}>
        <label className="st-field">
          <span>Project name</span>
          <input
            className="form-input" autoFocus required maxLength={120}
            placeholder="e.g. The Founder Mindset — Ep. 24"
            value={name} onChange={(event) => setName(event.target.value)}
          />
        </label>

        <div className="st-segmented" role="tablist" aria-label="Source type">
          <button type="button" role="tab" aria-selected={mode === "file"} className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}>
            <Upload size={14} /> Local video
          </button>
          <button type="button" role="tab" aria-selected={mode === "url"} className={mode === "url" ? "active" : ""} onClick={() => setMode("url")}>
            <Link2 size={14} /> Direct URL
          </button>
          <button type="button" role="tab" aria-selected={mode === "empty"} className={mode === "empty" ? "active" : ""} onClick={() => setMode("empty")}>
            <FolderPlus size={14} /> Empty
          </button>
        </div>

        {mode === "file" && (
          <>
            <label className="st-dropzone file-picker">
              <Upload size={19} />
              <strong>{file ? file.name : "Choose a local video"}</strong>
              <span>MP4, MOV, MKV, WebM or AVI · stored inside the project folder</span>
              <input type="file" accept=".mp4,.m4v,.mov,.mkv,.webm,.avi,video/*" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
            </label>
            <InlineNote>
              Only upload recordings you own or are allowed to process. ClipFree never bypasses DRM, logins or paywalls.
            </InlineNote>
          </>
        )}

        {mode === "url" && (
          <>
            <label className="st-field">
              <span>Direct media URL</span>
              <input
                className="form-input" type="url" placeholder="https://example.com/authorized-video.mp4"
                value={url} onChange={(event) => setUrl(event.target.value)}
              />
            </label>
            <label className="authorization-check">
              <input type="checkbox" checked={authorized} onChange={(event) => setAuthorized(event.target.checked)} />
              I own this media or am authorized to process this direct URL.
            </label>
          </>
        )}

        {mode === "empty" && (
          <InlineNote>Start with an empty project if you want to add a recording later.</InlineNote>
        )}

        {error && <InlineNote tone="error">{error}</InlineNote>}
        {busy && stage && <InlineNote tone="info"><span className="st-inline-busy"><LoaderCircle size={14} className="spin" />{stage}</span></InlineNote>}

        <div className="st-modal-actions">
          <button type="button" className="secondary-button" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="primary-button" disabled={busy}>
            {busy ? <><LoaderCircle size={15} className="spin" /> Creating…</> : <>Create project <ArrowRight size={15} /></>}
          </button>
        </div>
      </form>
    </Modal>
  );
}

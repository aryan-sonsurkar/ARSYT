"use client";

import { useEffect, useRef, useState } from "react";
import { Download, FileAudio, LoaderCircle, PencilLine, Play, Save, Search, Sparkles } from "lucide-react";
import { api, ApiError, formatBytes, Job, Project, Transcript, WhisperModel } from "@/lib/api";

type Props = { project: Project; onJobChange: () => void };

const LANGUAGES: Array<{ code: string; label: string }> = [
  { code: "", label: "Auto detect" },
  { code: "en", label: "English" },
  { code: "hi", label: "Hindi / Hinglish" },
  { code: "mr", label: "Marathi" },
  { code: "bn", label: "Bengali" },
  { code: "ta", label: "Tamil" },
  { code: "te", label: "Telugu" },
  { code: "ur", label: "Urdu" },
  { code: "gu", label: "Gujarati" },
  { code: "pa", label: "Punjabi" },
  { code: "es", label: "Spanish" },
  { code: "pt", label: "Portuguese" },
  { code: "fr", label: "French" },
  { code: "de", label: "German" },
  { code: "ar", label: "Arabic" },
  { code: "id", label: "Indonesian" },
  { code: "ru", label: "Russian" },
  { code: "zh", label: "Chinese" },
  { code: "ja", label: "Japanese" },
  { code: "ko", label: "Korean" },
];

export function TranscriptionPanel({ project, onJobChange }: Props) {
  const [models, setModels] = useState<WhisperModel[]>([]);
  const [engineAvailable, setEngineAvailable] = useState<boolean | null>(null);
  const [model, setModel] = useState<WhisperModel["name"]>("base");
  const [language, setLanguage] = useState("");
  const [device, setDevice] = useState<"auto" | "cpu" | "cuda">("auto");
  const [job, setJob] = useState<Job | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [search, setSearch] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingText, setEditingText] = useState("");
  const audioRef = useRef<HTMLAudioElement>(null);

  const refreshModels = async (selectDefault = false) => {
    try {
      const data = await api.transcriptionModels();
      setModels(data.models);
      setEngineAvailable(data.engine_available);
      setModel((current) => selectDefault || !data.models.some((item) => item.name === current) ? data.default_model : current);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to inspect local Whisper models."); }
  };
  const loadTranscript = async () => {
    try { setTranscript(await api.transcript(project.id)); }
    catch (error) {
      if (error instanceof ApiError && error.status === 404) { setTranscript(null); return; }
      setMessage(error instanceof Error ? error.message : "Unable to load the saved transcript.");
    }
  };

  useEffect(() => { void refreshModels(true); void loadTranscript(); }, [project.id]);
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const updated = await api.job(job.id); setJob(updated); onJobChange();
        if (updated.state === "completed") { await loadTranscript(); setMessage("Transcript saved locally."); }
      } catch { /* normal network retry on the next poll */ }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.id, job?.state, project.id]);

  const selected = models.find((item) => item.name === model);
  const jobActive = job && ["queued", "running"].includes(job.state);
  const visible = transcript?.content.segments.filter((segment) => segment.text.toLowerCase().includes(search.toLowerCase())) ?? [];

  async function downloadModel() {
    if (!selected || !window.confirm(`Download the ${selected.name} Whisper model (${selected.size}) to this device?`)) return;
    setBusy(true); setMessage(`Downloading ${selected.name} only after your confirmation…`);
    try { await api.downloadWhisperModel(selected.name); await refreshModels(); setMessage(`${selected.name} is ready locally.`); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Model download failed."); }
    finally { setBusy(false); }
  }

  async function start() {
    setBusy(true); setMessage("");
    try {
      const created = await api.startTranscription(project.id, { model, language: language || undefined, word_timestamps: true, device });
      setJob(created); setMessage("Transcription started locally."); onJobChange();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not start transcription."); }
    finally { setBusy(false); }
  }

  function beginEdit(index: number, text: string) {
    setEditingIndex(index);
    setEditingText(text);
  }

  async function saveEdit() {
    if (editingIndex === null || !editingText.trim()) { setEditingIndex(null); return; }
    setBusy(true);
    try {
      await api.editTranscriptSegments(project.id, [{ index: editingIndex, text: editingText }]);
      await loadTranscript();
      setMessage("Segment updated — captions and SRT stay in sync.");
      onJobChange();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save the transcript edit.");
    } finally { setBusy(false); setEditingIndex(null); setEditingText(""); }
  }

  return <section className="transcription-panel">
    <div className="card-heading"><div><h2>Local transcription</h2><p>faster-whisper runs only on audio saved in this project.</p></div><span className="local-model-label"><Sparkles size={13} /> LOCAL AI</span></div>
    {project.status !== "source_ready" && <div className="transcription-note">Finish source ingestion before starting transcription.</div>}
    {project.status === "source_ready" && <div className="transcription-controls">
      <label><span>Whisper model</span><select value={model} onChange={(event) => setModel(event.target.value as WhisperModel["name"])}>{models.map((item) => <option key={item.name} value={item.name}>{item.name} · {item.size} {item.installed ? "(installed)" : "(not installed)"}</option>)}</select></label>
      <label><span>Language</span><select value={language} onChange={(event) => setLanguage(event.target.value)}>{LANGUAGES.map((item) => <option key={item.code || "auto"} value={item.code}>{item.label}</option>)}</select></label>
      <label><span>Device</span><select value={device} onChange={(event) => setDevice(event.target.value as "auto" | "cpu" | "cuda")}><option value="auto">Auto</option><option value="cuda">NVIDIA GPU</option><option value="cpu">CPU</option></select></label>
      <div className="model-summary"><span>{selected?.description}</span><small>{selected?.memory}</small></div>
      {engineAvailable === false ? <div className="transcription-note">Install the project requirements to enable faster-whisper. Models are downloaded separately only after confirmation.</div> : selected && !selected.installed ? <button className="secondary-button" disabled={busy || engineAvailable === null} onClick={() => void downloadModel()}><Download size={14} /> Download {selected.name}</button> : <button className="primary-button" disabled={busy || engineAvailable === null || Boolean(jobActive)} onClick={() => void start()}>{busy || jobActive ? <LoaderCircle size={15} className="spin" /> : <FileAudio size={15} />} {jobActive ? "Transcription in progress" : "Transcribe audio"}</button>}
    </div>}
    {job && <div className="transcription-job"><span className={`status-badge status-${job.state}`}><span />{job.state}</span><strong>{job.stage}</strong><span>{job.progress}%</span>{job.error && <em>{job.error}</em>}</div>}
    {message && <p className="transcription-message">{message}</p>}
    {transcript && <div className="transcript-viewer"><div className="transcript-toolbar"><div><strong>{transcript.metadata.language || "Unknown language"}</strong><span>{transcript.metadata.segment_count} segments · {transcript.metadata.word_count} words · {transcript.metadata.model_name} · {transcript.metadata.device}/{transcript.metadata.compute_type} · language confidence {(transcript.content.language_probability * 100).toFixed(0)}%</span><small>{transcript.metadata.elapsed_seconds.toFixed(1)}s · {transcript.metadata.real_time_factor.toFixed(2)}× real-time · RAM peak Δ {transcript.metadata.ram_delta_bytes ? formatBytes(transcript.metadata.ram_delta_bytes) : "unavailable"} · VRAM Δ {transcript.metadata.vram_delta_bytes === null ? "unavailable" : formatBytes(transcript.metadata.vram_delta_bytes)}</small></div><label className="search-box"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search transcript" /></label></div>{transcript.content.language_probability < 0.5 && <p className="transcription-note">Language detection is uncertain. Select the spoken language above and transcribe again to improve recognition.</p>}<audio ref={audioRef} controls preload="metadata" src={api.audioUrl(project.id)} /><div className="transcript-segments">{visible.map((segment, index) => {
          const originalIndex = transcript.content.segments.indexOf(segment);
          return editingIndex === originalIndex ? (
            <div className="st-segment-edit" key={`edit-${segment.start}-${index}`}>
              <time>{formatTimestamp(segment.start)}</time>
              <textarea value={editingText} rows={2} onChange={(event) => setEditingText(event.target.value)} />
              <div className="st-segment-edit-actions">
                <button className="secondary-button sm" disabled={busy} onClick={() => { setEditingIndex(null); setEditingText(""); }}>Cancel</button>
                <button className="primary-button sm" disabled={busy || !editingText.trim()} onClick={() => void saveEdit()}>
                  {busy ? <LoaderCircle size={13} className="spin" /> : <Save size={13} />} Save
                </button>
              </div>
            </div>
          ) : (
            <div className="st-segment-row" key={`${segment.start}-${index}`}>
              <button onClick={() => { if (audioRef.current) { audioRef.current.currentTime = segment.start; void audioRef.current.play(); } }}>
                <time>{formatTimestamp(segment.start)}</time>
                <span>{segment.text}</span>
                <Play size={13} />
              </button>
              <button className="st-segment-edit-btn" title="Fix text (captions and SRT stay in sync)" onClick={() => beginEdit(originalIndex, segment.text)}>
                <PencilLine size={12} />
              </button>
            </div>
          );
        })}</div></div>}
  </section>;
}

function formatTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60); const remainder = Math.floor(seconds % 60);
  return `${minutes}:${remainder.toString().padStart(2, "0")}`;
}

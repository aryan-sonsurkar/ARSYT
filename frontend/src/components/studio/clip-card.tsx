"use client";

import { useState } from "react";
import {
  Captions, Check, ChevronsLeft, ChevronsRight, Copy, Download, Film, Flame, LoaderCircle, Package,
  PencilLine, Play, Sparkles, Trash2, UploadCloud, WandSparkles, Youtube,
} from "lucide-react";
import { api, AppSettings, Clip, formatBytes, formatTimestamp, PublishRequest, relativeTime, SocialVariant } from "@/lib/api";
import { Busy, EmptyState, Field, InlineNote, Modal, StatusBadge, Toggle } from "@/components/studio/ui";

type MusicFile = { name: string; size_bytes: number; path: string };

type Props = {
  clip: Clip;
  settings: AppSettings | null;
  musicFiles?: MusicFile[];
  showProject?: boolean;
  onChanged: () => void;
  onToast: (message: string) => void;
};

type Dialog = "" | "preview" | "render" | "metadata" | "social" | "publish";

export function ClipCard({ clip, settings, musicFiles = [], showProject = false, onChanged, onToast }: Props) {
  const [dialog, setDialog] = useState<Dialog>("");
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState(clip.title);
  const [social, setSocial] = useState<SocialVariant[]>([]);
  const [socialSource, setSocialSource] = useState("");
  const [copied, setCopied] = useState("");

  const [preset, setPreset] = useState(clip.preset || settings?.values.render_preset || "podcast");
  const [style, setStyle] = useState(clip.caption_style || settings?.values.caption_style || "kinetic");
  const [music, setMusic] = useState(clip.music_path || "");
  const [volume, setVolume] = useState(clip.music_volume ?? settings?.values.background_music_volume ?? 0.18);
  const [fps, setFps] = useState(30);
  const [platform, setPlatform] = useState<"youtube" | "reels">("youtube");
  const [smart, setSmart] = useState(settings?.values.smart_reframe ?? true);
  const [ducking, setDucking] = useState(true);
  const [trimSilence, setTrimSilence] = useState(false);
  const [metadataModel, setMetadataModel] = useState(settings?.values.metadata_model ?? "");
  const [pubTitle, setPubTitle] = useState("");
  const [pubDescription, setPubDescription] = useState("");
  const [pubTags, setPubTags] = useState("");
  const [pubPrivacy, setPubPrivacy] = useState<PublishRequest["privacy"]>("public");
  const [pubCategory, setPubCategory] = useState<PublishRequest["category"]>("entertainment");
  const [pubConfirmText, setPubConfirmText] = useState("");
  const [pubAuthed, setPubAuthed] = useState<boolean | null>(null);
  const [pubClientConfigured, setPubClientConfigured] = useState<boolean | null>(null);
  const [pubStatusChecking, setPubStatusChecking] = useState(false);
  const [error, setError] = useState("");

  const rendered = clip.state === "rendered" && clip.has_output;
  const active = clip.state === "queued" || clip.state === "rendering";

  function openDialog(next: Dialog) {
    if (next === "render") {
      setPreset(clip.preset || settings?.values.render_preset || "podcast");
      setStyle(clip.caption_style || settings?.values.caption_style || "kinetic");
      setPlatform((clip.caption_options as { platform?: string } | null)?.platform === "reels" ? "reels" : "youtube");
      setMusic(clip.music_path || "");
      setVolume(clip.music_volume ?? settings?.values.background_music_volume ?? 0.18);
      setSmart(settings?.values.smart_reframe ?? true);
      setTrimSilence(false);
      setMetadataModel(settings?.values.metadata_model ?? "");
      setError("");
    }
    if (next === "metadata") setTitle(clip.title);
    if (next === "publish") {
      setPubTitle(clip.title);
      setPubDescription(clip.metadata?.description ?? "");
      setPubTags((clip.metadata?.hashtags ?? []).join(" "));
      setPubPrivacy("public");
      setPubCategory("entertainment");
      setPubConfirmText("");
      setPubAuthed(null);
      setPubClientConfigured(null);
      setPubStatusChecking(true);
      void api.publishingStatus()
        .then((status) => { setPubAuthed(status.authorized); setPubClientConfigured(status.client_configured); })
        .catch(() => { setPubAuthed(false); setPubClientConfigured(false); })
        .finally(() => setPubStatusChecking(false));
    }
    if (next === "social") {
      setCopied("");
      void api.clipSocial(clip.id)
        .then((result) => {
          setSocial(result.variants ?? []);
          setSocialSource(result.generated_by ?? "");
        })
        .catch(() => setSocial([]));
    }
    setDialog(next);
  }

  async function render() {
    setBusy(true); setError("");
    try {
      await api.renderClip(clip.id, {
        preset, caption_style: style, caption_options: { ...(clip.caption_options ?? {}), platform },
        music_path: music, music_volume: volume, fps, ducking, metadata_model: metadataModel, smart_reframe: smart,
        trim_silence: trimSilence,
      });
      onToast("Render queued. Progress is live in the queue.");
      setDialog("");
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not queue the render.");
    } finally { setBusy(false); }
  }

  async function saveTitle() {
    setBusy(true); setError("");
    try {
      await api.updateClip(clip.id, { title });
      onToast("Clip title updated.");
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the title.");
    } finally { setBusy(false); }
  }

  async function nudge(edge: "start" | "end", delta: number) {
    setBusy(true); setError("");
    try {
      const start = edge === "start" ? Math.max(0, clip.start_time + delta) : clip.start_time;
      const end = edge === "end" ? clip.end_time + delta : clip.end_time;
      if (end - start < 3) {
        onToast("A clip must stay at least three seconds long.");
        return;
      }
      await api.updateClip(clip.id, { start_time: Number(start.toFixed(2)), end_time: Number(end.toFixed(2)) });
      onToast(`Clip ${edge} moved ${delta > 0 ? "+" : ""}${delta}s.`);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not move the clip boundary.");
    } finally { setBusy(false); }
  }

  async function regenerateMetadata() {
    setBusy(true); setError("");
    try {
      await api.regenerateMetadata(clip.id);
      onToast("Metadata regenerated from the transcript with local Ollama.");
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Metadata generation failed.");
    } finally { setBusy(false); }
  }

  async function exportClip() {
    setBusy(true); setError("");
    try {
      const result = await api.exportClip(clip.id);
      onToast(result.directory ? `Exported to ${result.directory}` : "Clip exported.");
    } catch (cause) {
      onToast(cause instanceof Error ? cause.message : "Export failed.");
    } finally { setBusy(false); }
  }

  async function generateSocial() {
    setBusy(true); setError("");
    try {
      const result = await api.generateClipSocial(clip.id);
      setSocial(result.variants ?? []);
      setSocialSource(result.generated_by ?? "");
      onToast("New viral captions written with the local model.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not write the captions.");
    } finally { setBusy(false); }
  }

  async function saveSocial() {
    setBusy(true); setError("");
    try {
      await api.saveClipSocial(clip.id, social);
      onToast("Caption edits saved.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save the captions.");
    } finally { setBusy(false); }
  }

  async function copyVariant(variant: SocialVariant) {
    const text = [variant.hook, variant.caption, variant.hashtags.join(" "), variant.cta]
      .filter(Boolean)
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(variant.style);
      window.setTimeout(() => setCopied(""), 1600);
    } catch {
      onToast("Copy failed. Select the text and copy it manually.");
    }
  }

  function updateVariant(style: string, patch: Partial<SocialVariant>) {
    setSocial((current) => current.map((item) => (item.style === style ? { ...item, ...patch } : item)));
  }

  async function publish() {
    if (pubConfirmText !== "PUBLISH") {
      setError("Type PUBLISH exactly to confirm the upload to your channel.");
      return;
    }
    setBusy(true); setError("");
    try {
      await api.publishClip(clip.id, {
        title: pubTitle,
        description: pubDescription,
        tags: pubTags.split(/[\s,]+/).filter(Boolean),
        privacy: pubPrivacy,
        category: pubCategory,
      }, true);
      onToast("Upload queued to your YouTube channel. Progress is live in the queue.");
      setDialog("");
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not queue the upload.");
    } finally { setBusy(false); }
  }

  async function connectYouTube() {
    setBusy(true); setError("");
    try {
      const result = await api.publishingAuthUrl();
      window.open(result.authorization_url, "_blank", "noopener");
      onToast("Google consent opened in a new tab. Finish it, then reopen this dialog.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start the OAuth flow.");
    } finally { setBusy(false); }
  }

  async function disconnectYouTube() {
    setBusy(true);
    try {
      await api.publishingDisconnect();
      setPubAuthed(false);
      onToast("YouTube account disconnected.");
    } catch {
      onToast("Disconnect failed.");
    } finally { setBusy(false); }
  }

  async function remove() {
    if (!window.confirm(`Delete “${clip.title}” and its rendered files? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await api.deleteClip(clip.id);
      onToast("Clip deleted.");
      onChanged();
    } catch (cause) {
      onToast(cause instanceof Error ? cause.message : "Could not delete the clip.");
    } finally { setBusy(false); }
  }

  return (
    <>
      <article className={`st-clip-card ${rendered ? "is-rendered" : ""}`}>
        <button className="st-clip-thumb" onClick={() => openDialog("preview")} disabled={!rendered} aria-label={rendered ? `Preview ${clip.title}` : "Render to preview"}>
          {rendered && clip.thumbnail_path ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={api.clipThumbnailUrl(clip.id)} alt="" loading="lazy" />
          ) : (
            <span className="st-clip-placeholder"><Film size={26} /></span>
          )}
          {rendered && <span className="st-play"><Play size={18} fill="currentColor" /></span>}
          <span className="st-clip-duration">{formatTimestamp(clip.duration || clip.end_time - clip.start_time)}</span>
        </button>

        <div className="st-clip-body">
          <div className="st-clip-top">
            <StatusBadge state={clip.state} />
            {clip.overall_score ? <span className="st-score-chip">score {Math.round(clip.overall_score)}</span> : null}
            {clip.metadata?.youtube_url && (
              <a className="st-published-chip" href={clip.metadata.youtube_url} target="_blank" rel="noopener noreferrer" title={clip.metadata.youtube_url}>
                <Youtube size={11} /> Published
              </a>
            )}
            {active && <LoaderCircle size={13} className="spin" />}
          </div>
          <h3>{clip.title}</h3>
          {showProject && clip.project_name && <span className="st-clip-project">{clip.project_name}</span>}
          <div className="st-clip-meta">
            <span>{formatTimestamp(clip.start_time)} → {formatTimestamp(clip.end_time)}</span>
            <span>{clip.caption_style || "default captions"}</span>
            {rendered && <span>{formatBytes(clip.file_size_bytes)}</span>}
            <span>{relativeTime(clip.created_at)}</span>
          </div>
          {clip.error && <InlineNote tone="error">{clip.error}</InlineNote>}

          <div className="st-clip-actions">
            {rendered ? (
              <button className="st-icon-action" title="Preview" onClick={() => openDialog("preview")}><Play size={14} /></button>
            ) : null}
            {rendered ? (
              <button className="st-icon-action" title="Re-render clip" aria-label="Re-render clip" disabled={busy || active} onClick={() => openDialog("render")}>
                <WandSparkles size={14} />
              </button>
            ) : (
              <button className="secondary-button sm" disabled={busy || active} onClick={() => openDialog("render")}>
                <WandSparkles size={14} /> Render
              </button>
            )}
            <button className="st-icon-action" title="Edit metadata" onClick={() => openDialog("metadata")}><Sparkles size={14} /></button>
            {rendered && <button className="st-icon-action" title="Viral captions" aria-label="Viral captions" onClick={() => openDialog("social")}><Flame size={14} /></button>}
            {rendered && <a className="st-icon-action" title="Download MP4" href={api.clipDownloadUrl(clip.id)}><Download size={14} /></a>}
            {rendered && <a className="st-icon-action" title="Download SRT" href={api.clipCaptionsUrl(clip.id)}><Captions size={14} /></a>}
            {rendered && <button className="st-icon-action" title="Export package" disabled={busy} onClick={() => void exportClip()}><Package size={14} /></button>}
            {rendered && <button className="st-icon-action" title="Publish to YouTube" disabled={busy} onClick={() => openDialog("publish")}><UploadCloud size={14} /></button>}
            <button className="st-icon-action danger" title="Delete clip" disabled={busy} onClick={() => void remove()}><Trash2 size={14} /></button>
          </div>
        </div>
      </article>

      <Modal open={dialog === "preview"} onClose={() => setDialog("")} title={clip.title} subtitle={`${formatTimestamp(clip.start_time)} → ${formatTimestamp(clip.end_time)} · ${clip.preset} · ${clip.caption_style}`} width={760}>
        {rendered ? (
          <div className="st-preview">
            <video src={api.clipVideoUrl(clip.id)} poster={clip.thumbnail_path ? api.clipThumbnailUrl(clip.id) : undefined} controls playsInline />
            <div className="st-preview-meta">
              <span>{formatBytes(clip.file_size_bytes)}</span>
              <span>{clip.duration.toFixed(1)}s</span>
              <span>1080 × 1920</span>
              <span>{clip.metadata?.category || "uncategorised"}</span>
            </div>
            <div className="st-modal-actions">
              <a className="secondary-button" href={api.clipDownloadUrl(clip.id)}><Download size={14} /> Download MP4</a>
              <a className="secondary-button" href={api.clipCaptionsUrl(clip.id)}><Captions size={14} /> Download SRT</a>
              <button className="primary-button" onClick={() => openDialog("metadata")}><PencilLine size={14} /> Metadata</button>
            </div>
          </div>
        ) : (
          <EmptyState icon={<Film size={20} />} title="Render this clip first">The vertical video and thumbnail are created by the local FFmpeg pipeline.</EmptyState>
        )}
      </Modal>

      <Modal open={dialog === "render"} onClose={() => setDialog("")} title={rendered ? "Re-render clip" : "Render clip"} subtitle="FFmpeg renders one 1080 × 1920 MP4 with burned-in captions and a saved SRT." width={620}>
        <div className="st-form">
          <div className="st-form-grid">
            <Field label="Visual preset">
              <select value={preset} onChange={(event) => setPreset(event.target.value)}>
                {(settings?.presets ?? [{ id: "podcast", label: "Podcast" }]).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </Field>
            <Field label="Caption style">
              <select value={style} onChange={(event) => setStyle(event.target.value)}>
                {(settings?.caption_styles ?? [{ id: "kinetic", label: "Kinetic" }]).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </Field>
            <Field label="Platform safe area" hint="Reels UI covers more of the lower frame, so captions sit higher.">
              <select value={platform} onChange={(event) => setPlatform(event.target.value as "youtube" | "reels")}>
                <option value="youtube">YouTube Shorts</option>
                <option value="reels">Instagram Reels</option>
              </select>
            </Field>
            <Field label="Frame rate">
              <select value={fps} onChange={(event) => setFps(Number(event.target.value))}>
                <option value={24}>24 fps</option><option value={30}>30 fps</option><option value={60}>60 fps</option>
              </select>
            </Field>
            <Field label="Metadata model">
              <select value={metadataModel} onChange={(event) => setMetadataModel(event.target.value)}>
                {(settings?.ollama.models ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </Field>
          </div>

          <Field label="Background music" hint={musicFiles.length ? "Music must already be stored in this project's local music folder." : "Upload music in the project overview first."}>
            <select value={music} onChange={(event) => setMusic(event.target.value)}>
              <option value="">No background music</option>
              {musicFiles.map((item) => <option key={item.path} value={item.path}>{item.name}</option>)}
            </select>
          </Field>

          <Field label={`Music volume · ${Math.round(volume * 100)}%`}>
            <input type="range" min={0} max={1} step={0.01} value={volume} onChange={(event) => setVolume(Number(event.target.value))} />
          </Field>

          <div className="st-toggle-row">
            <Toggle checked={smart} onChange={setSmart} label="Smart reframe" hint="Face-aware crop when OpenCV can detect faces; otherwise a safe centre crop." />
            <Toggle checked={ducking} onChange={setDucking} label="Duck music under speech" hint="Uses FFmpeg sidechain compression." />
            <Toggle checked={trimSilence} onChange={setTrimSilence} label="Trim dead air" hint="Cuts silences longer than 1.2 s down to a natural pause and keeps captions in sync." />
          </div>

          {error && <InlineNote tone="error">{error}</InlineNote>}
          <div className="st-modal-actions">
            <button className="secondary-button" onClick={() => setDialog("")} disabled={busy}>Cancel</button>
            <button className="primary-button" onClick={() => void render()} disabled={busy}>
              {busy ? <Busy label="Queueing…" /> : <><WandSparkles size={15} /> {rendered ? "Re-render" : "Render clip"}</>}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={dialog === "metadata"} onClose={() => setDialog("")} title="Clip metadata" subtitle="Grounded in the transcript by local Ollama. Edit anything before export." width={620}>
        <div className="st-form">
          <Field label="Clip title">
            <input className="form-input" value={title} maxLength={140} onChange={(event) => setTitle(event.target.value)} />
          </Field>

          <Field label="Fine trim" hint="Nudge the clip boundaries in half-second or 5-second steps. Requires a re-render to appear in the video.">
            <div className="st-trim-row">
              <div className="st-trim-edge">
                <span className="st-trim-label">Start {formatTimestamp(clip.start_time)}</span>
                <div className="st-inline-actions">
                  <button className="secondary-button sm" disabled={busy || clip.start_time <= 0} onClick={() => void nudge("start", -5)} title="Start −5s"><ChevronsLeft size={13} /> 5s</button>
                  <button className="secondary-button sm" disabled={busy || clip.start_time <= 0} onClick={() => void nudge("start", -0.5)} title="Start −0.5s"><ChevronsLeft size={13} /> .5s</button>
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("start", 0.5)}>.5s <ChevronsRight size={13} /></button>
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("start", 5)}>5s <ChevronsRight size={13} /></button>
                </div>
              </div>
              <div className="st-trim-edge">
                <span className="st-trim-label">End {formatTimestamp(clip.end_time)}</span>
                <div className="st-inline-actions">
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("end", -5)}><ChevronsLeft size={13} /> 5s</button>
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("end", -0.5)}><ChevronsLeft size={13} /> .5s</button>
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("end", 0.5)}>.5s <ChevronsRight size={13} /></button>
                  <button className="secondary-button sm" disabled={busy} onClick={() => void nudge("end", 5)}>5s <ChevronsRight size={13} /></button>
                </div>
              </div>
            </div>
          </Field>

          {(clip.metadata?.title_variants ?? []).filter((variant) => variant && variant !== clip.metadata?.title).length > 0 && (
            <Field label="Title variants (A/B)" hint="How each option reads in the Shorts feed. Click a card to apply it, then Save title.">
              <div className="st-feed-grid">
                {[
                  clip.metadata?.title ?? "",
                  ...(clip.metadata?.title_variants ?? []).filter((variant) => variant && variant !== clip.metadata?.title),
                ].map((variant, index) => (
                  <button key={`${variant}-${index}`} className={`st-feed-mock ${title === variant ? "current" : ""}`} onClick={() => { setTitle(variant); onToast("Title applied — press Save title to keep it."); }}>
                    <span className="st-feed-screen">
                      {rendered && clip.thumbnail_path
                        // eslint-disable-next-line @next/next/no-img-element
                        ? <img src={api.clipThumbnailUrl(clip.id)} alt="" />
                        : <span className="st-feed-fallback"><Film size={20} /></span>}
                      <span className="st-feed-title">{variant}</span>
                    </span>
                    <span className="st-feed-label">{title === variant ? "Current title" : `Variant ${index}`}</span>
                  </button>
                ))}
              </div>
            </Field>
          )}
          <Field label="Description">
            <textarea className="form-input" rows={4} readOnly value={clip.metadata?.description ?? ""} />
          </Field>
          <Field label="Hashtags">
            <input className="form-input" readOnly value={(clip.metadata?.hashtags ?? []).join(" ")} />
          </Field>
          {clip.metadata?.thumbnail_text && <Field label="Thumbnail text"><input className="form-input" readOnly value={clip.metadata.thumbnail_text} /></Field>}
          {error && <InlineNote tone="error">{error}</InlineNote>}
          <div className="st-modal-actions">
            <button className="secondary-button" onClick={() => void regenerateMetadata()} disabled={busy}><Sparkles size={14} /> Regenerate</button>
            <button className="primary-button" onClick={() => void saveTitle()} disabled={busy}>Save title</button>
          </div>
        </div>
      </Modal>
      <Modal open={dialog === "social"} onClose={() => setDialog("")} title="Viral captions" subtitle="Trending caption variants written locally from this clip's own transcript. Nothing is published." width={720}>
        {social.length === 0 ? (
          <EmptyState icon={<Flame size={20} />} title="No captions yet">Write trending caption variants for this clip with the local Ollama model.</EmptyState>
        ) : (
          <div className="st-social-list">
            {social.map((variant) => (
              <section className="st-social-card" key={variant.style}>
                <header>
                  <span className="st-social-label">{variant.label || variant.style}</span>
                  <button className="secondary-button sm" onClick={() => void copyVariant(variant)}>
                    {copied === variant.style ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy</>}
                  </button>
                </header>
                <Field label="Hook">
                  <input className="form-input" value={variant.hook} onChange={(event) => updateVariant(variant.style, { hook: event.target.value })} />
                </Field>
                <Field label="Caption">
                  <textarea className="form-input" rows={3} value={variant.caption} onChange={(event) => updateVariant(variant.style, { caption: event.target.value })} />
                </Field>
                <Field label="Hashtags">
                  <input className="form-input" value={variant.hashtags.join(" ")} onChange={(event) => updateVariant(variant.style, { hashtags: event.target.value.split(/\s+/).filter(Boolean) })} />
                </Field>
                <Field label="Call to action">
                  <input className="form-input" value={variant.cta} onChange={(event) => updateVariant(variant.style, { cta: event.target.value })} />
                </Field>
              </section>
            ))}
          </div>
        )}
        {error && <InlineNote tone="error">{error}</InlineNote>}
        <div className="st-modal-actions">
          <span className="st-social-source">{socialSource ? `written by ${socialSource}` : ""}</span>
          <button className="secondary-button" onClick={() => void saveSocial()} disabled={busy || social.length === 0}>Save edits</button>
          <button className="primary-button" onClick={() => void generateSocial()} disabled={busy}>
            {busy ? <Busy label="Writing…" /> : <><Flame size={15} /> {social.length ? "Rewrite" : "Write captions"}</>}
          </button>
        </div>
      </Modal>

      <Modal open={dialog === "publish"} onClose={() => setDialog("")} title="Publish to YouTube" subtitle="Uploads go to your own channel via the official Data API. Nothing is uploaded until you type PUBLISH." width={620}>
        <div className="st-form">
          {pubStatusChecking ? (
            <InlineNote><Busy label="Checking YouTube connection…" /></InlineNote>
          ) : pubAuthed ? (
            <InlineNote tone="ok">YouTube account connected. Uploads use your own OAuth credentials stored locally.</InlineNote>
          ) : pubClientConfigured ? (
            <InlineNote tone="error">Not authorized yet. Connect your own YouTube account first.</InlineNote>
          ) : (
            <InlineNote tone="error">No OAuth client yet. Save your Google Cloud client_secret.json to the path below (local data folder, publishing subfolder), then connect.</InlineNote>
          )}
          {!pubAuthed && (
            <div className="st-modal-actions">
              <Field label="Client secret path" hint="Create it in Google Cloud Console with the youtube.upload scope as an installed/desktop app.">
                <input className="form-input" readOnly value={pubClientConfigured ? "client_secret.json found ✓" : "clipfree_data/publishing/client_secret.json"} />
              </Field>
              <button className="primary-button" onClick={() => void connectYouTube()} disabled={busy || !pubClientConfigured}>
                <UploadCloud size={15} /> Connect YouTube account
              </button>
              {pubAuthed === false && pubClientConfigured && (
                <button className="secondary-button" onClick={() => void disconnectYouTube()} disabled={busy}>Clear saved token</button>
              )}
            </div>
          )}
          {pubAuthed && (
            <>
              <Field label="Video title" hint="YouTube Shorts titles are capped at 100 characters.">
                <input className="form-input" value={pubTitle} maxLength={100} onChange={(event) => setPubTitle(event.target.value)} />
              </Field>
              <Field label="Description">
                <textarea className="form-input" rows={3} value={pubDescription} onChange={(event) => setPubDescription(event.target.value)} />
              </Field>
              <Field label="Tags" hint="Space or comma separated.">
                <input className="form-input" value={pubTags} onChange={(event) => setPubTags(event.target.value)} />
              </Field>
              <div className="st-form-grid two">
                <Field label="Visibility">
                  <select value={pubPrivacy} onChange={(event) => setPubPrivacy(event.target.value as PublishRequest["privacy"])}>
                    <option value="public">Public</option>
                    <option value="unlisted">Unlisted</option>
                    <option value="private">Private</option>
                  </select>
                </Field>
                <Field label="Category">
                  <select value={pubCategory} onChange={(event) => setPubCategory(event.target.value as PublishRequest["category"])}>
                    <option value="entertainment">Entertainment</option>
                    <option value="people">People &amp; Blogs</option>
                    <option value="gaming">Gaming</option>
                    <option value="education">Education</option>
                  </select>
                </Field>
              </div>
              {clip.metadata?.youtube_url && <InlineNote tone="ok">Already published: {clip.metadata.youtube_url}</InlineNote>}
              <Field label="Type PUBLISH to confirm the upload">
                <input className="form-input" value={pubConfirmText} placeholder="PUBLISH" onChange={(event) => setPubConfirmText(event.target.value)} />
              </Field>
              {error && <InlineNote tone="error">{error}</InlineNote>}
              <div className="st-modal-actions">
                <button className="secondary-button" onClick={() => setDialog("")} disabled={busy}>Cancel</button>
                <button className="primary-button" onClick={() => void publish()} disabled={busy || pubConfirmText !== "PUBLISH"}>
                  {busy ? <Busy label="Queueing…" /> : <><UploadCloud size={15} /> Upload as Short</>}
                </button>
              </div>
            </>
          )}
          {error && !pubAuthed && <InlineNote tone="error">{error}</InlineNote>}
        </div>
      </Modal>
    </>
  );
}

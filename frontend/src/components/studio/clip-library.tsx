"use client";

import { useEffect, useMemo, useState } from "react";
import { Download, Film, LoaderCircle, Package, Search, WandSparkles } from "lucide-react";
import { api, AppSettings, Clip } from "@/lib/api";
import { ClipCard } from "@/components/studio/clip-card";
import { EmptyState, InlineNote } from "@/components/studio/ui";

export function ClipLibrary({ onOpenProject, onToast }: { onOpenProject: (projectId: string) => void; onToast: (message: string) => void }) {
  const [clips, setClips] = useState<Clip[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load(quiet = false) {
    try {
      const [items, config] = await Promise.all([api.clips(), api.settings()]);
      setClips(items);
      setSettings(config);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load the clip library.");
    } finally { if (!quiet) setLoading(false); }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (!clips.some((clip) => clip.state === "queued" || clip.state === "rendering")) return;
    const timer = window.setInterval(() => void load(true), 3500);
    return () => window.clearInterval(timer);
  }, [clips]);

  const visible = useMemo(() => clips.filter((clip) => {
    const matchesSearch = `${clip.title} ${clip.project_name ?? ""}`.toLowerCase().includes(search.toLowerCase());
    const matchesState = filter === "all"
      || (filter === "drafts" && clip.state === "draft")
      || (filter === "rendered" && clip.state === "rendered")
      || (filter === "failed" && clip.state === "failed");
    return matchesSearch && matchesState;
  }), [clips, search, filter]);

  const renderedIds = visible.filter((clip) => clip.state === "rendered" && clip.has_output).map((clip) => clip.id);
  const selectedIds = [...selected].filter((id) => clips.some((clip) => clip.id === id));
  const allVisibleSelected = visible.length > 0 && visible.every((clip) => selected.has(clip.id));

  function toggle(clipId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(clipId)) next.delete(clipId); else next.add(clipId);
      return next;
    });
  }

  function toggleAll() {
    setSelected((current) => {
      if (allVisibleSelected) {
        const next = new Set(current);
        for (const clip of visible) next.delete(clip.id);
        return next;
      }
      return new Set([...current, ...visible.map((clip) => clip.id)]);
    });
  }

  async function batchRender() {
    if (!selectedIds.length || !settings) return;
    setBusy(true); setError("");
    try {
      const result = await api.batchRender(selectedIds, {
        preset: settings.values.render_preset, caption_style: settings.values.caption_style,
        caption_options: {}, music_path: "", music_volume: settings.values.background_music_volume,
        fps: 30, ducking: true, metadata_model: settings.values.metadata_model,
        smart_reframe: settings.values.smart_reframe,
      });
      onToast(`${result.jobs.length} render job${result.jobs.length === 1 ? "" : "s"} queued${result.errors.length ? `, ${result.errors.length} skipped` : ""}.`);
      setSelected(new Set());
      await load(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Batch render failed.");
    } finally { setBusy(false); }
  }

  async function batchExport() {
    if (!selectedIds.length) return;
    setBusy(true); setError("");
    try {
      const result = await api.batchExport(selectedIds);
      onToast(`${result.exports.length} package${result.exports.length === 1 ? "" : "s"} exported to the local export folder.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Batch export failed.");
    } finally { setBusy(false); }
  }

  async function batchDownload() {
    if (!selectedIds.length) return;
    setBusy(true); setError("");
    try {
      await api.downloadBatch(selectedIds);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Batch download failed.");
    } finally { setBusy(false); }
  }

  return (
    <div className="st-stack">
      <div className="st-section-head">
        <div>
          <h1>Clip library<span className="heading-period">.</span></h1>
          <p>{clips.length} clip{clips.length === 1 ? "" : "s"} across every project. Rendered MP4s, captions, thumbnails and metadata stay local.</p>
        </div>
        <div className="st-library-count"><Film size={16} /><strong>{renderedIds.length}</strong><span>rendered</span></div>
      </div>

      <div className="st-toolbar">
        <label className="search-box"><Search size={14} /><input placeholder="Search clips or projects" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <div className="st-tabs">
          {[["all", "All"], ["drafts", "Drafts"], ["rendered", "Rendered"], ["failed", "Failed"]].map(([value, label]) => (
            <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>
          ))}
        </div>
        <div className="st-toolbar-actions">
          <button className="secondary-button sm" onClick={toggleAll} disabled={!visible.length}>{allVisibleSelected ? "Clear" : "Select all"}</button>
          <button className="secondary-button sm" disabled={!selectedIds.length || busy} onClick={() => void batchRender()}><WandSparkles size={14} /> Render</button>
          <button className="secondary-button sm" disabled={!selectedIds.length || busy} onClick={() => void batchExport()}><Package size={14} /> Export</button>
          <button className="secondary-button sm" disabled={!selectedIds.length || busy} onClick={() => void batchDownload()}><Download size={14} /> ZIP</button>
        </div>
      </div>

      {selectedIds.length > 0 && <InlineNote tone="info">{selectedIds.length} selected. Batch render uses your saved Settings defaults.</InlineNote>}
      {error && <InlineNote tone="error">{error}</InlineNote>}

      {loading ? (
        <div className="st-loading"><LoaderCircle size={18} className="spin" /> Reading local clips…</div>
      ) : visible.length ? (
        <div className="st-clip-grid">
          {visible.map((clip) => (
            <div key={clip.id} className={`st-selectable ${selected.has(clip.id) ? "selected" : ""}`}>
              <label className="st-selectbox">
                <input type="checkbox" checked={selected.has(clip.id)} onChange={() => toggle(clip.id)} />
                <span />
              </label>
              <button className="st-project-link" onClick={() => onOpenProject(clip.project_id)}>{clip.project_name ?? "Open project"}</button>
              <ClipCard clip={clip} settings={settings} showProject onChanged={() => void load(true)} onToast={onToast} />
            </div>
          ))}
        </div>
      ) : (
        <EmptyState icon={<Film size={20} />} title={clips.length ? "No clips match this filter" : "No clips yet"}>
          {clips.length ? "Try another search or filter." : "Approve highlight candidates in a project to create draft clips, then render them here."}
        </EmptyState>
      )}
    </div>
  );
}

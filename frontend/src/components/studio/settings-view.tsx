"use client";

import { useEffect, useState } from "react";
import {
  Check, Cpu, Database, Download, FolderOpen, HardDrive, LoaderCircle,
  Save, ShieldCheck, SlidersHorizontal, Sparkles, WandSparkles,
} from "lucide-react";
import { api, AppSettings, formatBytes, Health } from "@/lib/api";
import { Busy, Field, InlineNote, Toggle } from "@/components/studio/ui";

type NumberKey = "clip_count" | "min_clip_seconds" | "max_clip_seconds";

export function SettingsView({ onToast }: { onToast: (message: string) => void }) {
  const [config, setConfig] = useState<AppSettings | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [whisper, setWhisper] = useState<Array<{ name: string; size: string; memory: string; installed: boolean; description: string }>>([]);
  const [exportsList, setExportsList] = useState<Array<{ name: string; relative_path: string; size_bytes: number; modified_at: number }>>([]);
  const [form, setForm] = useState<AppSettings["values"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    try {
      const [settings, status, models, exports] = await Promise.all([
        api.settings(), api.health(), api.transcriptionModels(), api.exports(),
      ]);
      setConfig(settings);
      setHealth(status);
      setWhisper(models.models);
      setExportsList(exports.files);
      setForm((current) => current ?? settings.values);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not read local settings.");
    }
  }

  useEffect(() => { void load(); }, []);

  function update<K extends keyof AppSettings["values"]>(key: K, value: AppSettings["values"][K]) {
    setForm((current) => current ? { ...current, [key]: value } : current);
    setSaved(false);
  }

  function number(key: NumberKey, value: string) {
    update(key, Number(value) as never);
  }

  async function save() {
    if (!form) return;
    setBusy(true); setError("");
    try {
      const updated = await api.updateSettings({
        highlight_model: form.highlight_model,
        metadata_model: form.metadata_model,
        clip_count: form.clip_count,
        min_clip_seconds: form.min_clip_seconds,
        max_clip_seconds: form.max_clip_seconds,
        render_preset: form.render_preset,
        caption_style: form.caption_style,
        background_music_volume: form.background_music_volume,
        smart_reframe: form.smart_reframe,
        export_dir: form.export_dir,
      });
      setConfig(updated);
      setForm(updated.values);
      setSaved(true);
      onToast("Settings saved. New jobs use these defaults.");
      window.setTimeout(() => setSaved(false), 3000);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save settings.");
    } finally { setBusy(false); }
  }

  if (!config || !form) {
    return <div className="st-loading">{error ? error : <><LoaderCircle size={18} className="spin" /> Reading local settings…</>}</div>;
  }

  const models = config.ollama.models.length ? config.ollama.models : [];
  const usedPercent = health?.storage.total_bytes ? Math.min(100, (health.storage.used_bytes / health.storage.total_bytes) * 100) : 0;

  return (
    <div className="st-stack">
      <div className="st-section-head">
        <div>
          <h1>Settings<span className="heading-period">.</span></h1>
          <p>Local models, export defaults and the resource profile for this laptop.</p>
        </div>
        <button className="primary-button" onClick={() => void save()} disabled={busy}>
          {busy ? <Busy label="Saving…" /> : saved ? <><Check size={15} /> Saved</> : <><Save size={15} /> Save settings</>}
        </button>
      </div>

      {error && <InlineNote tone="error">{error}</InlineNote>}
      {!config.ollama.available && <InlineNote tone="warn">Ollama is offline. Start it at 127.0.0.1:11434 before running highlights or metadata.</InlineNote>}

      <div className="st-settings-grid">
        <section className="st-panel">
          <div className="st-panel-head"><div><h2><Sparkles size={15} /> Local AI</h2><p>Only models already installed in Ollama are listed. ClipFree never downloads models on its own.</p></div></div>
          <div className="st-form">
            <Field label="Highlight + selection model" hint="Runs one bounded JSON request per transcript chunk.">
              <select value={form.highlight_model} onChange={(event) => update("highlight_model", event.target.value)}>
                {models.map((model) => <option key={model} value={model}>{model}</option>)}
                {!models.length && <option value={form.highlight_model}>{form.highlight_model}</option>}
              </select>
            </Field>
            <Field label="Metadata model">
              <select value={form.metadata_model} onChange={(event) => update("metadata_model", event.target.value)}>
                {models.map((model) => <option key={model} value={model}>{model}</option>)}
                {!models.length && <option value={form.metadata_model}>{form.metadata_model}</option>}
              </select>
            </Field>
            <div className="st-model-list">
              {config.ollama.models.map((model) => (
                <span key={model} className={`st-model-chip ${model === form.highlight_model ? "active" : ""}`}><span />{model}</span>
              ))}
              {!config.ollama.models.length && <span className="st-muted">No Ollama models detected.</span>}
            </div>
            <InlineNote tone="info">
              <span><strong>Context 8192 · temperature 0.2.</strong> Heavy work is serialized: transcription, highlights and rendering do not run at the same time by default.</span>
            </InlineNote>
          </div>
        </section>

        <section className="st-panel">
          <div className="st-panel-head"><div><h2><SlidersHorizontal size={15} /> Clip defaults</h2><p>Applied to new highlight runs and render jobs.</p></div></div>
          <div className="st-form">
            <div className="st-form-grid two">
              <Field label="Clips per run"><input className="form-input" type="number" min={1} max={20} value={form.clip_count} onChange={(event) => number("clip_count", event.target.value)} /></Field>
              <Field label="Minimum clip seconds"><input className="form-input" type="number" min={8} max={120} value={form.min_clip_seconds} onChange={(event) => number("min_clip_seconds", event.target.value)} /></Field>
              <Field label="Maximum clip seconds"><input className="form-input" type="number" min={12} max={180} value={form.max_clip_seconds} onChange={(event) => number("max_clip_seconds", event.target.value)} /></Field>
              <Field label="Music volume"><input className="form-input" type="number" min={0} max={1} step={0.01} value={form.background_music_volume} onChange={(event) => update("background_music_volume", Number(event.target.value))} /></Field>
            </div>
            <div className="st-form-grid two">
              <Field label="Render preset">
                <select value={form.render_preset} onChange={(event) => update("render_preset", event.target.value)}>
                  {config.presets.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
              </Field>
              <Field label="Caption style">
                <select value={form.caption_style} onChange={(event) => update("caption_style", event.target.value)}>
                  {config.caption_styles.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
              </Field>
            </div>
            <Toggle checked={form.smart_reframe} onChange={(value) => update("smart_reframe", value)} label="Smart reframe by default" hint="Falls back to a safe centre crop when face detection is unavailable." />
            <InlineNote><span><strong>Render concurrency:</strong> {form.render_concurrency} (serialized). This protects a 6 GB VRAM / 16 GB RAM laptop.</span></InlineNote>
          </div>
        </section>

        <section className="st-panel">
          <div className="st-panel-head"><div><h2><Cpu size={15} /> Whisper engine</h2><p>faster-whisper runs on CPU with int8 by default.</p></div></div>
          <div className="st-form">
            <InlineNote tone="info">Default model: <strong>{form.default_whisper_model}</strong>. Model downloads require explicit confirmation in a project's transcription panel.</InlineNote>
            <div className="st-whisper-list">
              {whisper.map((model) => (
                <div className="st-whisper-row" key={model.name}>
                  <div><strong>{model.name}</strong><span>{model.memory} · {model.size}</span></div>
                  <span className={`st-installed ${model.installed ? "yes" : "no"}`}><span />{model.installed ? "installed" : "not installed"}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="st-panel">
          <div className="st-panel-head"><div><h2><HardDrive size={15} /> Storage &amp; exports</h2><p>Everything is written to local folders only.</p></div></div>
          <div className="st-form">
            <Field label="Export directory" hint={`Resolved: ${config.export_dir_resolved}`}>
              <input className="form-input" value={form.export_dir} onChange={(event) => update("export_dir", event.target.value)} />
            </Field>
            <div className="st-storage">
              <div className="st-storage-top"><span>Disk used</span><strong>{health ? `${formatBytes(health.storage.used_bytes)} / ${formatBytes(health.storage.total_bytes)}` : "unavailable"}</strong></div>
              <div className="st-storage-track"><span style={{ width: `${usedPercent}%` }} /></div>
              <div className="st-storage-foot"><span><Database size={12} /> {health?.database ? "SQLite database connected" : "database unavailable"}</span><span>{health ? `${formatBytes(health.storage.free_bytes)} free` : ""}</span></div>
            </div>
            <div className="st-export-list">
              <div className="st-export-head"><strong><FolderOpen size={13} /> Recent exports</strong><button className="text-button" onClick={() => void load()}>Refresh</button></div>
              {exportsList.slice(0, 6).map((file) => (
                <a key={file.relative_path} href={api.exportDownloadUrl(file.relative_path)} className="st-export-row">
                  <span>{file.name}</span><span>{formatBytes(file.size_bytes)}</span><Download size={13} />
                </a>
              ))}
              {!exportsList.length && <span className="st-muted">No exported packages yet.</span>}
            </div>
          </div>
        </section>
      </div>

      <div className="st-compliance">
        <ShieldCheck size={18} />
        <div>
          <strong>Local-first by design</strong>
          <span>No paid APIs, no automatic social publishing, and no DRM, auth or paywall bypassing. Use only media you own or are authorized to process.</span>
        </div>
        <WandSparkles size={18} />
      </div>
    </div>
  );
}

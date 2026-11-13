"use client";

import { motion } from "framer-motion";
import {
  Activity, ArrowRight, AudioLines, Clapperboard, Cpu, Film, FolderKanban,
  HardDrive, LoaderCircle, Plus, Sparkles, Video, WandSparkles,
} from "lucide-react";
import { api, Dashboard, formatBytes, formatTimestamp, Health, Project, relativeTime } from "@/lib/api";
import { JobList } from "@/components/studio/job-list";
import { EmptyState, StatusBadge } from "@/components/studio/ui";

type Props = {
  data: Dashboard;
  health: Health | null;
  loading: boolean;
  error: string;
  onOpenProject: (projectId: string) => void;
  onNewProject: () => void;
  onNavigate: (view: "projects" | "clips" | "queue" | "settings") => void;
  onToast: (message: string) => void;
  onChanged: () => void;
};

export function OverviewView({ data, health, loading, error, onOpenProject, onNewProject, onNavigate, onToast, onChanged }: Props) {
  const metrics = [
    { label: "Total projects", value: data.stats.projects, icon: FolderKanban, note: "Created on this device" },
    { label: "Videos processed", value: data.stats.videos_processed, icon: Video, note: "Imported source videos" },
    { label: "Clips rendered", value: data.stats.clips_generated, icon: Clapperboard, note: "Real vertical renders" },
    { label: "Active jobs", value: data.stats.active_jobs, icon: Activity, note: data.stats.active_jobs ? "In your local queue" : "Nothing running" },
  ];

  return (
    <div className="st-stack">
      <div className="welcome-row">
        <div>
          <div className="eyebrow"><span className="eyebrow-line" /> YOUR LOCAL PRODUCTION STUDIO</div>
          <h1>Good to see you<span className="heading-period">.</span></h1>
          <p className="welcome-subtitle">Turn long conversations into scroll-stopping short videos — entirely on this machine.</p>
        </div>
        <button className="primary-button" onClick={onNewProject}><Plus size={17} /> New project <span className="button-shortcut">↗</span></button>
      </div>

      {error && (
        <div className="error-banner"><span><Activity size={17} /><span><strong>Can’t reach the ClipFree backend.</strong> {error} Start the local API, then retry.</span></span></div>
      )}

      <section className="metric-grid" aria-label="Studio activity">
        {metrics.map(({ label, value, icon: Icon, note }, index) => (
          <motion.article className="metric-card" key={label} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * 0.06 }}>
            <div className="metric-top"><span className="metric-label">{label}</span><span className="metric-icon"><Icon size={17} strokeWidth={1.8} /></span></div>
            <div className="metric-value">{loading ? <span className="skeleton skeleton-value" /> : value}</div>
            <div className="metric-note"><span className="note-indicator" />{note}</div>
          </motion.article>
        ))}
      </section>

      <div className="st-overview-grid">
        <section className="st-panel">
          <div className="st-panel-head">
            <div><h2>Recent projects</h2><p>Pick up where you left off.</p></div>
            <button className="secondary-button sm" onClick={() => onNavigate("projects")}>View all <ArrowRight size={13} /></button>
          </div>
          {loading && !data.projects.length ? (
            <div className="st-loading"><LoaderCircle size={17} className="spin" /> Loading local projects…</div>
          ) : data.projects.length ? (
            <div className="st-project-list">
              {data.projects.slice(0, 5).map((project, index) => (
                <ProjectRow key={project.id} project={project} index={index} onOpen={() => onOpenProject(project.id)} />
              ))}
            </div>
          ) : (
            <EmptyState icon={<AudioLines size={20} />} title="Your next great clip starts here">
              Create a project with a podcast recording you own, then transcribe, detect highlights and render vertical clips.
              <span className="st-empty-actions"><button className="primary-button sm" onClick={onNewProject}><Plus size={14} /> Create project</button></span>
            </EmptyState>
          )}
        </section>

        <div className="st-side-stack">
          <section className="st-panel">
            <div className="st-panel-head"><div><h2>Studio status</h2><p>Services running on this device</p></div><span className={`status-orb ${health?.ollama.available ? "on" : ""}`}><span /></span></div>
            <div className="service-list">
              <div className="service-row"><div className="service-symbol"><WandSparkles size={15} /></div><span>Ollama inference</span><strong className={health?.ollama.available ? "service-up" : "service-down"}><i />{health?.ollama.available ? "Ready" : "Offline"}</strong></div>
              <div className="service-row"><div className="service-symbol"><HardDrive size={15} /></div><span>Local database</span><strong className={!error ? "service-up" : "service-down"}><i />{!error ? "Connected" : "Unavailable"}</strong></div>
              <div className="service-row"><div className="service-symbol"><Cpu size={15} /></div><span>Processing engine</span><strong className="service-up"><i />FFmpeg · serialized</strong></div>
            </div>
            <div className="st-storage-mini">
              <div><span>Project storage</span><strong>{formatBytes(data.storage_bytes)}</strong></div>
              <div className="st-storage-track"><span style={{ width: health?.storage.total_bytes ? `${Math.min(100, (health.storage.used_bytes / health.storage.total_bytes) * 100)}%` : "0%" }} /></div>
              <span>{health ? `${formatBytes(health.storage.free_bytes)} free on this drive` : "Storage unavailable"}</span>
            </div>
          </section>

          <section className="st-panel">
            <div className="st-panel-head"><div><h2>Latest clips</h2><p>Rendered on this device</p></div><button className="text-button" onClick={() => onNavigate("clips")}>Library</button></div>
            {data.clips.length ? (
              <div className="st-mini-clips">
                {data.clips.slice(0, 4).map((clip) => (
                  <button key={clip.id} onClick={() => onOpenProject(clip.project_id)} className="st-mini-clip">
                    <span className="st-mini-thumb">
                      {clip.has_output && clip.thumbnail_path
                        // eslint-disable-next-line @next/next/no-img-element
                        ? <img src={api.clipThumbnailUrl(clip.id)} alt="" loading="lazy" />
                        : <Film size={15} />}
                    </span>
                    <span><strong>{clip.title}</strong><em>{formatTimestamp(clip.duration)} · {clip.state}</em></span>
                  </button>
                ))}
              </div>
            ) : <EmptyState icon={<Clapperboard size={18} />} title="No rendered clips yet">Approve highlights in a project to create your first clip.</EmptyState>}
          </section>
        </div>
      </div>

      <section className="st-panel">
        <div className="st-panel-head">
          <div><h2><Activity size={15} /> Processing activity</h2><p>Live updates from the local worker</p></div>
          <div className="st-panel-actions"><span className={`st-live ${data.stats.active_jobs ? "on" : ""}`}><span /> {data.stats.active_jobs ? "LIVE" : "IDLE"}</span><button className="secondary-button sm" onClick={() => onNavigate("queue")}>Open queue</button></div>
        </div>
        <JobList jobs={data.jobs} limit={4} onChanged={onChanged} onToast={onToast} emptyLabel="Your processing history will show up here." />
      </section>

      <div className="st-tip">
        <span><Sparkles size={15} /></span>
        <p><strong>Local AI, real constraints.</strong> Whisper <code>small</code> and Ollama <code>gemma3:4b</code> run one job at a time to stay inside a 6 GB VRAM / 16 GB RAM laptop.</p>
      </div>
    </div>
  );
}

function ProjectRow({ project, index, onOpen }: { project: Project; index: number; onOpen: () => void }) {
  return (
    <button className="st-project-row" onClick={onOpen}>
      <span className={`st-project-thumb thumb-${index % 4}`}><AudioLines size={16} /></span>
      <span className="st-project-copy"><strong>{project.name}</strong><em>{project.source_filename || project.source_url || "No source added yet"}</em></span>
      <StatusBadge state={project.status} />
      <span className="st-project-time">{relativeTime(project.updated_at)}</span>
      <ArrowRight size={15} />
    </button>
  );
}

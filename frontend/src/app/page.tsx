"use client";

import { useCallback, useEffect, useRef, useState } from "react";import { AnimatePresence, motion } from "framer-motion";
import {
  Activity, Bell, Check, Clapperboard, Film, FolderKanban, HardDrive, LayoutDashboard,
  Menu, Moon, Plus, Settings2, Sun, UploadCloud, X,
} from "lucide-react";
import { api, Dashboard, Health } from "@/lib/api";
import { NewProjectModal } from "@/components/studio/new-project-modal";
import { OverviewView } from "@/components/studio/overview";
import { ProjectsView } from "@/components/studio/projects-view";
import { ProjectDetailView } from "@/components/studio/project-detail";
import { ClipLibrary } from "@/components/studio/clip-library";
import { QueueView } from "@/components/studio/job-list";
import { SettingsView } from "@/components/studio/settings-view";
import { formatBytes, relativeTime } from "@/lib/api";

type View = "overview" | "projects" | "clips" | "queue" | "settings";
type Notification = { id: number; message: string; at: number };

const initialDashboard: Dashboard = {
  stats: { projects: 0, videos_processed: 0, clips_generated: 0, active_jobs: 0, completed_jobs: 0, failed_jobs: 0, candidates: 0 },
  projects: [], jobs: [], clips: [], storage_bytes: 0, export_dir: "",
};

const navigation: Array<{ id: View; label: string; icon: typeof LayoutDashboard }> = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "projects", label: "Projects", icon: FolderKanban },
  { id: "clips", label: "Clip library", icon: Film },
  { id: "queue", label: "Processing queue", icon: Activity },
  { id: "settings", label: "Settings", icon: Settings2 },
];

function parseHash(): { view: View; projectId: string | null } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  if (raw.startsWith("projects/")) {
    const id = raw.slice("projects/".length);
    return { view: "projects", projectId: id || null };
  }
  if (raw === "projects" || raw === "clips" || raw === "queue" || raw === "settings") {
    return { view: raw, projectId: null };
  }
  return { view: "overview", projectId: null };
}

function hashFor(view: View, projectId: string | null): string {
  if (view === "overview") return "#/";
  if (view === "projects") return projectId ? `#/projects/${projectId}` : "#/projects";
  return `#/${view}`;
}

export default function Home() {
  const [view, setView] = useState<View>("overview");
  const [projectId, setProjectId] = useState<string | null>(null);
  const [data, setData] = useState<Dashboard>(initialDashboard);
  const [health, setHealth] = useState<Health | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mobileMenu, setMobileMenu] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [toast, setToast] = useState("");
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [bellOpen, setBellOpen] = useState(false);
  const [unseen, setUnseen] = useState(0);
  const seenAt = useRef(0);
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    const stored = window.localStorage.getItem("clipfree-theme");
    const next = stored === "light" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
  }, []);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("clipfree-theme", next);
  }

  const refresh = useCallback(async (quiet = false) => {
    try {
      const [dashboard, status] = await Promise.all([api.dashboard(), api.health()]);
      setData(dashboard);
      setHealth(status);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The local API is unavailable.");
    } finally { if (!quiet) setLoading(false); }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh(true);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const sync = () => {
      const parsed = parseHash();
      setView(parsed.view);
      setProjectId(parsed.projectId);
    };
    sync();
    window.addEventListener("popstate", sync);
    window.addEventListener("hashchange", sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener("hashchange", sync);
    };
  }, []);

  useEffect(() => {
    const hash = hashFor(view, projectId);
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  }, [view, projectId]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 4200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function navigate(next: View) {
    setView(next);
    setProjectId(null);
    setMobileMenu(false);
  }

  function openProject(id: string) {
    setProjectId(id);
    setView("projects");
    setMobileMenu(false);
  }

  const notify = useCallback((message: string) => {
    setToast(message);
    setNotifications((current) => [...current.slice(-29), { id: Date.now() + Math.random(), message, at: Date.now() }]);
    setUnseen((current) => current + 1);
  }, []);

  function openBell() {
    setBellOpen((open) => !open);
    if (!bellOpen) { seenAt.current = Date.now(); setUnseen(0); }
  }

  // ---- Drag & drop video anywhere -> new project with the file as source ----
  const [dragging, setDragging] = useState(false);
  const [dropBusy, setDropBusy] = useState("");
  const dragDepth = useRef(0);

  const isVideoFile = (file: File) =>
    file.type.startsWith("video/") || /\.(mp4|m4v|mov|mkv|webm|avi)$/i.test(file.name);

  async function handleDrop(file: File) {
    if (!isVideoFile(file)) { notify("Drop a video file (MP4, MOV, MKV, WebM or AVI) to start a project."); return; }
    setDropBusy("Creating the local project…");
    try {
      const name = file.name.replace(/\.[^.]+$/, "").slice(0, 120) || "Dropped video";
      const project = await api.createProject(name, "");
      setDropBusy("Storing and probing the video locally…");
      const job = await api.createJob(project.id);
      await api.uploadSource(project.id, job.id, file);
      notify(`Project "${name}" created and the video is ingested locally.`);
      openProject(project.id);
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "Could not import the dropped video.");
    } finally { setDropBusy(""); }
  }

  useEffect(() => {
    const onDragEnter = (event: DragEvent) => {
      if (!event.dataTransfer) return;
      event.preventDefault();
      dragDepth.current += 1;
      if ([...event.dataTransfer.items].some((item) => item.kind === "file")) setDragging(true);
    };
    const onDragOver = (event: DragEvent) => { event.preventDefault(); };
    const onDragLeave = (event: DragEvent) => {
      event.preventDefault();
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragging(false);
    };
    const onDrop = (event: DragEvent) => {
      event.preventDefault();
      dragDepth.current = 0;
      setDragging(false);
      const file = event.dataTransfer?.files?.[0];
      if (file && !dropBusy) void handleDrop(file);
    };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [dropBusy, notify]);
  // --------------------------------------------------------------------------

  const activeProject = view === "projects" && projectId;
  const pageLabel = activeProject ? "Project" : navigation.find((item) => item.id === view)?.label ?? "Overview";

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileMenu ? "sidebar-open" : ""}`}>
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); navigate("overview"); }}>
          <span className="brand-mark"><Clapperboard size={19} strokeWidth={2.4} /></span>
          <span className="brand-name">clip<span>free</span></span><span className="local-tag">LOCAL</span>
        </a>

        <button className="st-sidebar-new" onClick={() => { setNewProjectOpen(true); setMobileMenu(false); }}>
          <Plus size={15} /> New project
        </button>

        <div className="nav-heading">WORKSPACE</div>
        <nav className="main-nav">
          {navigation.map(({ id, label, icon: Icon }) => (
            <button key={id} className={`nav-item ${view === id && !activeProject ? "active" : ""}`} onClick={() => navigate(id)}>
              <Icon size={17} strokeWidth={1.8} /><span>{label}</span>
              {id === "queue" && data.stats.active_jobs > 0 && <span className="nav-count">{data.stats.active_jobs}</span>}
            </button>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <div className="storage-label"><span>Local storage</span><strong>{formatBytes(data.storage_bytes)}</strong></div>
          <div className="storage-track"><span style={{ width: health?.storage.total_bytes ? `${Math.min(100, (health.storage.used_bytes / health.storage.total_bytes) * 100)}%` : "0%" }} /></div>
          <span className="storage-note">Project files stay on this device</span>
          <div className="profile-row"><div className="profile-avatar">AS</div><div className="profile-copy"><strong>Local user</strong><span>Studio owner</span></div><HardDrive size={16} /></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <button className="icon-button mobile-only" aria-label="Open navigation" onClick={() => setMobileMenu(!mobileMenu)}><Menu size={19} /></button>
          <div className="breadcrumb">Studio <span>/</span> <strong>{pageLabel}</strong></div>
          <div className="topbar-right">
            <div className={`connection-pill ${health?.ollama.available ? "connected" : ""}`}><span className="connection-dot" />{health?.ollama.available ? "Ollama connected" : "Ollama offline"}</div>
            <span className="st-local-pill">127.0.0.1</span>
            <button className="icon-button" aria-label="Toggle light or dark theme" title={theme === "dark" ? "Switch to light" : "Switch to dark"} onClick={toggleTheme}>
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <div className="st-bell-wrap">
              <button className="icon-button st-bell" aria-label="Notifications" onClick={openBell}>
                <Bell size={17} />
                {unseen > 0 && <span className="st-bell-count">{unseen > 9 ? "9+" : unseen}</span>}
              </button>
              <AnimatePresence>
                {bellOpen && (
                  <motion.div className="st-bell-panel" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }}>
                    <div className="st-bell-head"><strong>Notifications</strong><button className="text-button" onClick={() => setNotifications([])}>Clear</button></div>
                    <div className="st-bell-list">
                      {notifications.length ? [...notifications].reverse().map((item) => (
                        <div className="st-bell-item" key={item.id}>
                          <span className="st-bell-dot" />
                          <div><p>{item.message}</p><small>{relativeTime(new Date(item.at).toISOString())}</small></div>
                        </div>
                      )) : <div className="st-bell-empty">Nothing yet. Activity messages collect here.</div>}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
            <div className="top-avatar">AS</div>
          </div>
        </header>

        <div className="page-content">
          {view === "overview" && (
            <OverviewView
              data={data} health={health} loading={loading} error={error}
              onOpenProject={openProject} onNewProject={() => setNewProjectOpen(true)}
              onNavigate={navigate} onToast={notify} onChanged={() => void refresh(true)}
            />
          )}
          {view === "projects" && (activeProject ? (
            <ProjectDetailView
              key={projectId}
              projectId={projectId}
              onBack={() => { setProjectId(null); void refresh(true); }}
              onChanged={() => void refresh(true)}
              onToast={notify}
            />
          ) : (
            <ProjectsView projects={data.projects} onOpenProject={openProject} onNewProject={() => setNewProjectOpen(true)} />
          ))}
          {view === "clips" && <ClipLibrary onOpenProject={openProject} onToast={notify} />}
          {view === "queue" && <QueueView onToast={notify} />}
          {view === "settings" && <SettingsView onToast={notify} />}

          <footer className="page-footer"><span>ClipFree <span className="footer-dot">·</span> Local first, always.</span><span><span className="footer-lock">◆</span> Your files never leave this device</span></footer>
        </div>
      </main>

      <NewProjectModal
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        onCreated={(id) => { openProject(id); void refresh(true); }}
        onToast={notify}
      />

      <AnimatePresence>
        {(dragging || dropBusy) && (
          <motion.div className="st-drop-overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="st-drop-overlay-card">
              <UploadCloud size={34} />
              <strong>{dropBusy || "Drop the video to create a project"}</strong>
              <span>{dropBusy ? "Importing locally — you will land in the project when it finishes." : "MP4, MOV, MKV, WebM or AVI · stays on this device"}</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {toast && (
          <motion.div className="toast" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}>
            <Check size={16} />{toast}<button aria-label="Dismiss" onClick={() => setToast("")}><X size={15} /></button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

"use client";

import { useMemo, useState } from "react";
import { ArrowRight, AudioLines, Film, FolderKanban, Plus, Search } from "lucide-react";
import { formatBytes, Project, relativeTime } from "@/lib/api";
import { EmptyState, StatusBadge } from "@/components/studio/ui";

export function ProjectsView({
  projects, onOpenProject, onNewProject,
}: { projects: Project[]; onOpenProject: (projectId: string) => void; onNewProject: () => void }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");

  const visible = useMemo(() => projects.filter((project) => {
    const matchesSearch = `${project.name} ${project.source_filename ?? ""} ${project.source_url}`.toLowerCase().includes(search.toLowerCase());
    const matchesStatus = filter === "all"
      || (filter === "ready" && project.status === "source_ready")
      || (filter === "empty" && project.status === "created")
      || (filter === "failed" && project.status === "failed");
    return matchesSearch && matchesStatus;
  }), [projects, search, filter]);

  return (
    <div className="st-stack">
      <div className="st-section-head">
        <div>
          <h1>Projects<span className="heading-period">.</span></h1>
          <p>{projects.length} local project{projects.length === 1 ? "" : "s"}. Each one keeps its source, transcript, clips and exports in its own folder.</p>
        </div>
        <button className="primary-button" onClick={onNewProject}><Plus size={15} /> New project</button>
      </div>

      <div className="st-toolbar">
        <label className="search-box"><Search size={14} /><input placeholder="Search projects" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <div className="st-tabs">
          {[["all", "All"], ["ready", "Source ready"], ["empty", "Empty"], ["failed", "Failed"]].map(([value, label]) => (
            <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>
          ))}
        </div>
      </div>

      {visible.length ? (
        <div className="st-project-grid">
          {visible.map((project, index) => (
            <button key={project.id} className="st-project-card" onClick={() => onOpenProject(project.id)}>
              <div className={`st-project-cover thumb-${index % 4}`}>
                <AudioLines size={26} />
                <span className="st-project-format">{project.source_filename?.split(".").pop()?.toUpperCase() || "PROJECT"}</span>
              </div>
              <div className="st-project-card-body">
                <div className="st-project-card-top"><StatusBadge state={project.status} /><span>{relativeTime(project.updated_at)}</span></div>
                <h3>{project.name}</h3>
                <p>{project.source_filename || project.source_url || "No source media yet"}</p>
                <div className="st-project-card-foot">
                  <span><FolderKanban size={12} /> {project.source_size_bytes ? formatBytes(project.source_size_bytes) : "empty"}</span>
                  <span className="st-open-project">Open <ArrowRight size={13} /></span>
                </div>
              </div>
            </button>
          ))}
        </div>
      ) : (
        <EmptyState icon={<Film size={20} />} title={projects.length ? "No projects match this filter" : "No projects yet"}>
          {projects.length ? "Try another search or filter." : "Create your first project from a local video you own."}
          {!projects.length && <span className="st-empty-actions"><button className="primary-button sm" onClick={onNewProject}><Plus size={14} /> Create project</button></span>}
        </EmptyState>
      )}
    </div>
  );
}

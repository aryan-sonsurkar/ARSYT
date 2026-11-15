export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8026";

export type Project = {
  id: string; name: string; source_url: string; source_path: string;
  source_title: string; source_filename?: string; source_size_bytes?: number;
  metadata_json?: string; status: string; created_at: string; updated_at: string;
};

export type Job = {
  id: string; project_id: string; project_name?: string; kind?: string; state: string;
  stage: string; progress: number; error: string; created_at: string;
  started_at?: string | null; completed_at?: string | null; retry_count?: number;
};

export type WhisperModel = {
  name: "tiny" | "base" | "small" | "medium"; size: string; memory: string; description: string;
  installed: boolean; path: string; recommended_device: "cpu" | "cuda"; recommended_compute_type: string;
};

export type TranscriptSegment = {
  start: number; end: number; text: string; avg_logprob: number; no_speech_prob: number;
  words: Array<{ start: number; end: number; word: string; probability: number }>;
};

export type Transcript = {
  metadata: {
    model_name: string; language: string; language_probability?: number; device: string; compute_type: string;
    segment_count: number; word_count: number; elapsed_seconds: number; real_time_factor: number;
    ram_delta_bytes: number; vram_delta_bytes: number | null;
  };
  content: { language: string; language_probability: number; duration_seconds: number; segments: TranscriptSegment[] };
};

export type Candidate = {
  id: string; project_id: string; start_time: number; end_time: number;
  title: string; hook: string; reason: string; category: string;
  scores: Record<string, number>; overall_score: number; excerpt: string;
  state: "candidate" | "approved" | "rejected"; selected: boolean;
  created_at: string; updated_at: string;
};

export type ClipMetadata = {
  title?: string; description?: string; hashtags?: string[]; category?: string;
  thumbnail_text?: string; generated_by?: string;
};

export type SocialVariant = {
  style: string; label: string; hook: string; caption: string;
  hashtags: string[]; cta: string;
};

export type SocialCaptions = {
  clip_id?: string; variants: SocialVariant[];
  generated_by?: string; model?: string; updated_at?: string;
};

export type Clip = {
  id: string; project_id: string; candidate_id?: string | null; project_name?: string;
  title: string; start_time: number; end_time: number; state: string;
  preset: string; caption_style: string; caption_options: Record<string, unknown>;
  music_path: string; music_volume: number; output_path: string;
  thumbnail_path: string; srt_path: string; duration: number; file_size_bytes: number;
  metadata: ClipMetadata; error: string; category?: string; overall_score?: number;
  has_output: boolean; created_at: string; updated_at: string;
};

export type Preset = {
  id: string; label: string; description: string; zoom: number;
  color_filter: string; caption_style: string; fps: number;
};

export type CaptionStyle = { id: string; label: string };

export type AppSettings = {
  values: {
    highlight_model: string; metadata_model: string; clip_count: number;
    min_clip_seconds: number; max_clip_seconds: number; render_preset: string;
    caption_style: string; background_music_volume: number; smart_reframe: boolean;
    export_dir: string; render_concurrency: number; default_whisper_model: string;
  };
  ollama: { available: boolean; models: string[] };
  presets: Preset[];
  caption_styles: CaptionStyle[];
  export_dir_resolved: string;
};

export type RenderOptions = {
  preset: string; caption_style: string; caption_options: Record<string, unknown>;
  music_path: string; music_volume: number; fps: number; ducking: boolean;
  metadata_model?: string; smart_reframe: boolean; trim_silence?: boolean;
};

export type ProjectDetail = Project & {
  jobs: Job[];
  transcript: Transcript["metadata"] | null;
  candidates: Candidate[];
  clips: Clip[];
  music_files: Array<{ name: string; size_bytes: number; path: string }>;
  settings: {
    highlight_model: string; clip_count: number; min_clip_seconds: number;
    max_clip_seconds: number; preset: string; caption_style: string;
    music_volume: number; smart_reframe: boolean; metadata_model: string;
  };
};

export type Dashboard = {
  stats: {
    projects: number; videos_processed: number; clips_generated: number; active_jobs: number;
    completed_jobs: number; failed_jobs: number; candidates: number;
  };
  projects: Project[]; jobs: Job[]; clips: Clip[]; storage_bytes: number; export_dir: string;
};

export type Health = {
  status: string; ollama: { available: boolean; models: string[]; url?: string };
  storage: { total_bytes: number; free_bytes: number; used_bytes: number };
  database: string; data_dir: string; export_dir: string;
};

export type ExportResult = { clip_id?: string; directory?: string; files?: string[]; path?: string; filename?: string; clip_count?: number };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_URL}${path}`, { ...init, cache: "no-store", headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.detail ?? `API request failed (${response.status})`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function downloadPost(path: string, body: unknown, filename: string): Promise<void> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(data.detail ?? `Download failed (${response.status})`, response.status);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export const api = {
  dashboard: () => request<Dashboard>("/api/dashboard"),
  health: () => request<Health>("/api/health"),
  jobs: () => request<Job[]>("/api/jobs"),
  projects: () => request<Project[]>("/api/projects"),
  project: (projectId: string) => request<ProjectDetail>(`/api/projects/${projectId}`),
  createProject: (name: string, source_url: string) => request<Project>("/api/projects", { method: "POST", body: JSON.stringify({ name, source_url }) }),
  deleteProject: (projectId: string) => request<void>(`/api/projects/${projectId}?confirm=true`, { method: "DELETE" }),
  createJob: (projectId: string) => request<Job>(`/api/projects/${projectId}/jobs`, { method: "POST" }),
  uploadSource: (projectId: string, jobId: string, file: File) => {
    const form = new FormData();
    form.set("job_id", jobId);
    form.set("file", file, file.name);
    return request<Job>(`/api/projects/${projectId}/source/upload`, { method: "POST", body: form });
  },
  ingestUrl: (projectId: string, url: string) => request<Job>(`/api/projects/${projectId}/source/url`, {
    method: "POST", body: JSON.stringify({ url, authorized: true }),
  }),
  sourceUrl: (projectId: string) => `${API_URL}/api/projects/${projectId}/source`,
  cancelJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" }),
  retryJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}/retry`, { method: "POST" }),
  job: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),

  transcriptionModels: () => request<{ default_model: WhisperModel["name"]; engine_available: boolean; models: WhisperModel[] }>("/api/transcription/models"),
  downloadWhisperModel: (model: WhisperModel["name"]) => request<{ model: string; installed: boolean; path: string }>(`/api/transcription/models/${model}/download`, { method: "POST", body: JSON.stringify({ confirm_download: true }) }),
  startTranscription: (projectId: string, options: { model: WhisperModel["name"]; language?: string; word_timestamps: boolean; device: "auto" | "cpu" | "cuda" }) => request<Job>(`/api/projects/${projectId}/transcription`, { method: "POST", body: JSON.stringify(options) }),
  transcript: (projectId: string) => request<Transcript>(`/api/projects/${projectId}/transcript`),
  audioUrl: (projectId: string) => `${API_URL}/api/projects/${projectId}/media/audio`,

  startHighlights: (projectId: string, options: { model: string; count: number; min_duration: number; max_duration: number }) => request<Job>(`/api/projects/${projectId}/highlights`, { method: "POST", body: JSON.stringify(options) }),
  startPipeline: (projectId: string, options?: Partial<{
    transcription_model: string; language: string | null; device: "auto" | "cpu" | "cuda";
    highlight_model: string; count: number; min_duration: number; max_duration: number; render: Record<string, unknown>;
  }>) => request<Job>(`/api/projects/${projectId}/pipeline`, { method: "POST", body: JSON.stringify(options ?? {}) }),
  updateCandidate: (candidateId: string, payload: Partial<Pick<Candidate, "title" | "hook" | "reason" | "category" | "start_time" | "end_time">>) => request<Candidate>(`/api/candidates/${candidateId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  candidateAction: (candidateId: string, action: "approve" | "reject" | "reset") => request<Candidate>(`/api/candidates/${candidateId}/${action}`, { method: "POST" }),
  batchApprove: (candidateIds: string[]) => request<{ approved: Candidate[]; errors: Array<{ candidate_id: string; error: string }> }>("/api/candidates/batch/approve", { method: "POST", body: JSON.stringify({ candidate_ids: candidateIds }) }),
  createClip: (projectId: string, payload: { candidate_id?: string; title?: string; start_time?: number; end_time?: number }) => request<Clip>(`/api/projects/${projectId}/clips`, { method: "POST", body: JSON.stringify(payload) }),
  clipsFromApproved: (projectId: string) => request<{ clips: Clip[] }>(`/api/projects/${projectId}/clips/from-approved`, { method: "POST" }),

  clips: (projectId?: string) => request<Clip[]>(projectId ? `/api/clips?project_id=${projectId}` : "/api/clips"),
  clip: (clipId: string) => request<Clip>(`/api/clips/${clipId}`),
  updateClip: (clipId: string, payload: Record<string, unknown>) => request<Clip>(`/api/clips/${clipId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteClip: (clipId: string) => request<void>(`/api/clips/${clipId}?confirm=true`, { method: "DELETE" }),
  renderClip: (clipId: string, options: RenderOptions) => request<Job>(`/api/clips/${clipId}/render`, { method: "POST", body: JSON.stringify(options) }),
  batchRender: (clipIds: string[], options: RenderOptions) => request<{ jobs: Job[]; errors: Array<{ clip_id: string; error: string }> }>("/api/clips/batch/render", { method: "POST", body: JSON.stringify({ clip_ids: clipIds, options }) }),
  clipVideoUrl: (clipId: string) => `${API_URL}/api/clips/${clipId}/video`,
  clipThumbnailUrl: (clipId: string) => `${API_URL}/api/clips/${clipId}/thumbnail`,
  clipDownloadUrl: (clipId: string) => `${API_URL}/api/clips/${clipId}/download`,
  clipCaptionsUrl: (clipId: string) => `${API_URL}/api/clips/${clipId}/captions`,
  clipMetadata: (clipId: string) => request<ClipMetadata>(`/api/clips/${clipId}/metadata`),
  regenerateMetadata: (clipId: string) => request<ClipMetadata>(`/api/clips/${clipId}/metadata`, { method: "POST" }),
  clipSocial: (clipId: string) => request<SocialCaptions>(`/api/clips/${clipId}/social`),
  generateClipSocial: (clipId: string, options?: { model?: string; count?: number }) => request<SocialCaptions>(`/api/clips/${clipId}/social`, { method: "POST", body: JSON.stringify(options ?? {}) }),
  saveClipSocial: (clipId: string, variants: SocialVariant[]) => request<SocialCaptions>(`/api/clips/${clipId}/social/save`, { method: "POST", body: JSON.stringify({ variants }) }),
  exportClip: (clipId: string) => request<ExportResult>(`/api/clips/${clipId}/export`, { method: "POST" }),
  batchExport: (clipIds: string[]) => request<{ exports: ExportResult[]; errors: Array<{ clip_id: string; error: string }> }>("/api/clips/batch/export", { method: "POST", body: JSON.stringify({ clip_ids: clipIds }) }),
  downloadBatch: (clipIds: string[], filename = "clipfree-clips.zip") => downloadPost("/api/clips/batch/download", { clip_ids: clipIds }, filename),
  exportProject: (projectId: string) => request<ExportResult>(`/api/projects/${projectId}/export`, { method: "POST" }),
  projectExportUrl: (projectId: string, name: string) => `${API_URL}/api/projects/${projectId}/export/download?name=${encodeURIComponent(name)}`,

  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (values: Partial<AppSettings["values"]>) => request<AppSettings>("/api/settings", { method: "PUT", body: JSON.stringify(values) }),
  exports: () => request<{ export_dir: string; files: Array<{ name: string; relative_path: string; size_bytes: number; modified_at: number }> }>("/api/exports"),
  exportDownloadUrl: (relativePath: string) => `${API_URL}/api/exports/download?path=${encodeURIComponent(relativePath)}`,
  sourceDownloadUrl: (projectId: string) => `${API_URL}/api/projects/${projectId}/source`,

  music: (projectId: string) => request<Array<{ name: string; size_bytes: number; path: string }>>(`/api/projects/${projectId}/music`),
  uploadMusic: (projectId: string, file: File) => {
    const form = new FormData();
    form.set("file", file, file.name);
    return request<{ name: string; path: string; duration_seconds: number }>(`/api/projects/${projectId}/music`, { method: "POST", body: form });
  },
  deleteMusic: (projectId: string, name: string) => request<void>(`/api/projects/${projectId}/music/${encodeURIComponent(name)}`, { method: "DELETE" }),
};

export function formatBytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index >= 2 ? 1 : 0)} ${units[index]}`;
}

export function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function formatTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.floor(seconds % 60);
  return `${minutes}:${remainder.toString().padStart(2, "0")}`;
}

# Known Issues and Phase 3 Limits

- Browser job status uses five-second REST polling. Cancellation is checked by the worker while streaming and extracting, but the UI can take up to one poll interval to display the final state.
- Direct URL import supports public direct media files only. It does not support web pages or platform download flows, and it intentionally refuses URL sources that would need DRM, authentication, paywall, or platform-restriction bypassing.
- The source upload endpoint completes after WAV extraction so a browser request can remain open for large files. The queue still receives durable stage/progress updates; resumable upload is not yet implemented.
- The worker processes one media ingestion pipeline at a time to fit the target laptop. This avoids competing FFmpeg jobs but queues simultaneous imports.
- faster-whisper and the small model are installed locally after explicit user approval. A real 8m49s downloaded video was processed through the full workflow, but transcript quality did not pass acceptance: 74 words across 15 segments, Kannada detection confidence 38%.
- That run used small/int8 CPU in 344.5 seconds (RTF 0.651), with ~1.02 GB peak RAM increase and 0 GB VRAM increase. This source may not contain enough clear speech to benchmark a podcast workflow.
- CUDA runtime lacked `cublas64_12.dll`; automatic device selection now falls back to CPU. GPU inference remains unverified despite CTranslate2 detecting a CUDA device; no extra CUDA libraries were installed.
- The default/base, tiny, and medium Whisper models are not installed; only the explicitly selected small model was downloaded.
- The configured base default has not been benchmarked on this machine; select the production default after a real-media speed/resource comparison.
- Language detection confidence is shown in the viewer. For confidence below 50%, the UI recommends selecting the spoken language and transcribing again.
- VRAM telemetry uses Windows NVML when present; other environments without a supported GPU monitoring source show it as unavailable rather than a measured zero.
- No transcript editing, visual crop analysis, AI clip selection, rendered captions, music mix, final rendering, or clip export exists yet.
- Dashboard live updates use periodic REST polling. SSE/WebSocket streaming requires the worker lifecycle.
- Ollama connection status is checked live. Hardware telemetry and per-project disk usage are not yet shown.
- Empty ClipFree settings are expected until settings persistence is implemented. Existing CodeShortsBot settings remain unchanged.
- `clipfree_data` can be moved with `CLIPFREE_DATA_DIR`; the target directory must be writable and have adequate local free space.
- A Google Fonts import is currently used for the visual design; system sans-serif fallbacks are configured.

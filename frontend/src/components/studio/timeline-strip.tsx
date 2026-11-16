"use client";

import { useState } from "react";
import { Film, Sparkles } from "lucide-react";
import { Candidate, Clip, formatTimestamp } from "@/lib/api";

type Props = {
  candidates: Candidate[];
  clips: Clip[];
  duration: number;
  onOpenCandidate?: (candidateId: string) => void;
  onOpenClip?: (clipId: string) => void;
};

type Block = {
  key: string;
  lane: 0 | 1;
  start: number;
  end: number;
  label: string;
  tooltip: string;
  className: string;
  intensity?: number;
};

  function colorFor(score: number): number {
    // 0 (muted) → 100 (bright) intensity for the inline style.
    return Math.max(18, Math.min(100, Math.round(score)));
  }

export function TimelineStrip({ candidates, clips, duration, onOpenCandidate, onOpenClip }: Props) {
  const [hover, setHover] = useState<Block | null>(null);
  const total = Math.max(duration, 60);
  const blocks: Block[] = [];

  for (const candidate of candidates) {
    blocks.push({
      key: `c-${candidate.id}`,
      lane: 0,
      start: candidate.start_time,
      end: candidate.end_time,
      label: candidate.title,
      tooltip: `${candidate.title} · ${formatTimestamp(candidate.start_time)}–${formatTimestamp(candidate.end_time)} · score ${Math.round(candidate.overall_score)}`,
      className: `st-tl-candidate ${candidate.state === "approved" ? "approved" : candidate.state === "rejected" ? "rejected" : ""}`,
      intensity: colorFor(candidate.overall_score),
    });
  }
  for (const clip of clips) {
    blocks.push({
      key: `k-${clip.id}`,
      lane: 1,
      start: clip.start_time,
      end: clip.end_time,
      label: clip.title,
      tooltip: `${clip.title} · ${formatTimestamp(clip.start_time)}–${formatTimestamp(clip.end_time)} · ${clip.state}`,
      className: `st-tl-clip ${clip.state}`,
    });
  }

  const ticks: Array<{ left: number; label: string }> = [];
  const step = total > 3600 ? 600 : total > 1200 ? 300 : total > 600 ? 120 : 60;
  for (let second = 0; second <= total; second += step) {
    ticks.push({ left: (second / total) * 100, label: formatTimestamp(second) });
  }

  function jump(block: Block) {
    const element = block.lane === 0
      ? document.getElementById(`candidate-${block.key.slice(2)}`)
      : document.getElementById(`clip-${block.key.slice(2)}`);
    if (element) element.scrollIntoView({ behavior: "smooth", block: "center" });
    (block.lane === 0 ? onOpenCandidate?.(block.key.slice(2)) : onOpenClip?.(block.key.slice(2)));
  }

  if (!blocks.length) return null;

  return (
    <section className="st-panel st-timeline-panel" aria-label="Project timeline">
      <div className="st-panel-head">
        <div><h2><Film size={14} /> Timeline</h2><p>Highlight moments (top) and clips (bottom) laid out across the source. Click to jump.</p></div>
        {hover && <span className="st-tl-hover">{hover.tooltip}</span>}
      </div>
      <div className="st-timeline">
        {[0, 1].map((lane) => (
          <div className="st-tl-lane" key={lane}>
            <span className="st-tl-lane-label">{lane === 0 ? <Sparkles size={11} /> : <Film size={11} />}</span>
            <div className="st-tl-track">
              {blocks.filter((block) => block.lane === lane).map((block) => {
                const left = Math.max(0, Math.min(100, (block.start / total) * 100));
                const width = Math.max(0.7, Math.min(100 - left, ((block.end - block.start) / total) * 100));
                return (
                  <button
                    key={block.key}
                    className={`${block.className} ${hover?.key === block.key ? "hover" : ""}`}
                    style={{ left: `${left}%`, width: `${width}%`, ...(block.intensity !== undefined ? { opacity: 0.35 + (block.intensity / 100) * 0.65 } : {}) }}
                    title={block.tooltip}
                    onMouseEnter={() => setHover(block)}
                    onMouseLeave={() => setHover(null)}
                    onClick={() => jump(block)}
                  >
                    <span>{block.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
        <div className="st-tl-scale">
          <span />
          {ticks.map((tick) => (
            <span key={tick.left} style={{ left: `${tick.left}%` }}>{tick.label}</span>
          ))}
        </div>
      </div>
    </section>
  );
}

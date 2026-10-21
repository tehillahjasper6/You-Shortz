import { Player, Thumbnail } from "@remotion/player";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildClipProps,
  checkHighlight,
  type BrollPlanItem,
  type FaceTrack,
  type Highlight,
  type Highlights,
  type Ingest,
} from "../../remotion/clip-props";
import { FPS, HEIGHT, WIDTH, type ClipWord } from "../../remotion/schema";
import { ViralClip } from "../../remotion/ViralClip";
import { formatTime, snapEnd, snapStart, viewWindow } from "./timeline";

type Job = { running: boolean; ranks: number[]; log: string[]; exitCode: number | null; startedAt: string };
type RenderedClip = { rank: number; file: string; hook_variants?: { file: string; label: string }[] };
type State = {
  workspace: string;
  hasSource: boolean;
  ingest: Ingest;
  highlights: Highlights | null;
  duration: number;
  words: ClipWord[];
  segments: { text: string; start: number; end: number }[];
  faceTracks: Record<string, FaceTrack>;
  broll: Record<string, { start: number; end: number; items: BrollPlanItem[] }>;
  rendered: RenderedClip[];
  job: Job | null;
};

const api = async <T,>(url: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
  return data as T;
};

const propsFor = (state: State, clip: Highlight) =>
  buildClipProps({
    clip,
    words: state.words,
    ingest: state.ingest,
    track: state.faceTracks[clip.rank],
    brollItems:
      state.broll[clip.rank] && Math.abs(state.broll[clip.rank].start - clip.start) < 1e-6 && Math.abs(state.broll[clip.rank].end - clip.end) < 1e-6
        ? state.broll[clip.rank].items
        : [],
    reframe: "auto",
  });

export const App: React.FC = () => {
  const [state, setState] = useState<State | null>(null);
  const [clips, setClips] = useState<Highlight[]>([]);
  const [selected, setSelected] = useState(1);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [options, setOptions] = useState({ variants: false, safeZones: false, noBroll: false });

  const load = useCallback(async () => {
    const s = await api<State>("/api/state");
    setState(s);
    setClips(s.highlights?.clips ?? []);
    setJob(s.job);
    setSelected((cur) => (s.highlights?.clips.some((c) => c.rank === cur) ? cur : s.highlights?.clips[0]?.rank ?? 1));
  }, []);

  useEffect(() => {
    load().catch((e) => setError(String(e)));
  }, [load]);

  // Poll while a render runs; refresh renders when it finishes.
  useEffect(() => {
    if (!job?.running) return;
    const id = setInterval(async () => {
      const { job: next } = await api<{ job: Job | null }>("/api/render");
      setJob(next);
      if (next && !next.running) load();
    }, 2000);
    return () => clearInterval(id);
  }, [job?.running, load]);

  const saved = state?.highlights?.clips ?? [];
  const dirty = JSON.stringify(saved) !== JSON.stringify(clips);
  const clip = clips.find((c) => c.rank === selected);

  const update = (rank: number, patch: Partial<Highlight>) =>
    setClips((cs) => cs.map((c) => (c.rank === rank ? { ...c, ...patch } : c)));

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const s = await api<State>("/api/highlights", { method: "PUT", body: JSON.stringify({ clips }) });
      setState(s);
      setClips(s.highlights?.clips ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const render = async () => {
    setError(null);
    try {
      if (dirty) await save();
      const { job: next } = await api<{ job: Job }>("/api/render", {
        method: "POST",
        body: JSON.stringify({ ranks: [...checked].sort((a, b) => a - b), options }),
      });
      setJob(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (error && !state) return <div className="empty">Could not load the workspace: {error}</div>;
  if (!state) return <div className="empty">Loading…</div>;
  if (!state.highlights || !state.hasSource)
    return (
      <div className="empty">
        Nothing to review in <code>{state.workspace}</code> yet. Run the pipeline first:
        <pre>npx tsx scripts/run.ts --url "…" --no-render</pre>
      </div>
    );

  return (
    <div className="app">
      <header>
        <h1>Clip review</h1>
        <span className="muted">
          {state.highlights.title ?? "Untitled"} · workspace <code>{state.workspace}</code>
        </span>
        <div className="spacer" />
        {dirty ? <span className="badge">Unsaved changes</span> : null}
        <button disabled={!dirty || saving} onClick={save}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="primary" disabled={!checked.size || Boolean(job?.running)} onClick={render}>
          Render selected ({checked.size})
        </button>
      </header>
      {error ? <div className="error">{error}</div> : null}

      <main>
        <section className="storyboard">
          {clips.map((c) => (
            <StoryCard
              key={c.rank}
              clip={c}
              state={state}
              active={c.rank === selected}
              checked={checked.has(c.rank)}
              edited={JSON.stringify(c) !== JSON.stringify(saved.find((s) => s.rank === c.rank))}
              onSelect={() => setSelected(c.rank)}
              onCheck={(on) =>
                setChecked((s) => {
                  const next = new Set(s);
                  if (on) next.add(c.rank);
                  else next.delete(c.rank);
                  return next;
                })
              }
            />
          ))}
          <div className="options">
            <label>
              <input type="checkbox" checked={options.variants} onChange={(e) => setOptions({ ...options, variants: e.target.checked })} />
              A/B hook variants
            </label>
            <label>
              <input type="checkbox" checked={options.noBroll} onChange={(e) => setOptions({ ...options, noBroll: e.target.checked })} />
              No B-roll
            </label>
            <label>
              <input type="checkbox" checked={options.safeZones} onChange={(e) => setOptions({ ...options, safeZones: e.target.checked })} />
              Safe-zone guides (dev)
            </label>
          </div>
          <RenderStatus job={job} rendered={state.rendered} />
        </section>

        {clip ? <Editor key={clip.rank} clip={clip} state={state} onChange={(patch) => update(clip.rank, patch)} /> : null}
      </main>
    </div>
  );
};

const StoryCard: React.FC<{
  clip: Highlight;
  state: State;
  active: boolean;
  checked: boolean;
  edited: boolean;
  onSelect: () => void;
  onCheck: (on: boolean) => void;
}> = ({ clip, state, active, checked, edited, onSelect, onCheck }) => {
  const { props } = useMemo(() => propsFor(state, clip), [state, clip]);
  const { warnings, errors } = checkHighlight(clip, state.duration);
  return (
    <div className={`card ${active ? "active" : ""}`} onClick={onSelect}>
      <Thumbnail
        component={ViralClip}
        inputProps={props}
        compositionWidth={WIDTH}
        compositionHeight={HEIGHT}
        durationInFrames={Math.max(1, props.clipDurationInFrames)}
        fps={FPS}
        frameToDisplay={Math.min(45, Math.max(0, props.clipDurationInFrames - 1))}
        style={{ width: 72, height: 128, borderRadius: 6, overflow: "hidden", flex: "none" }}
      />
      <div className="card-body">
        <div className="card-top">
          <strong>#{clip.rank}</strong>
          <span className="score">{clip.viral_score}</span>
          {edited ? <span className="badge">edited</span> : null}
          <input
            type="checkbox"
            title="Include in render"
            checked={checked}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => onCheck(e.target.checked)}
          />
        </div>
        <div className="hook">{clip.hook_text}</div>
        <div className="muted">
          {formatTime(clip.start)} → {formatTime(clip.end)} · {(clip.end - clip.start).toFixed(1)}s
        </div>
        {[...errors, ...warnings].map((w) => (
          <div key={w} className={errors.includes(w) ? "warn err" : "warn"}>
            {w}
          </div>
        ))}
      </div>
    </div>
  );
};

const Editor: React.FC<{ clip: Highlight; state: State; onChange: (patch: Partial<Highlight>) => void }> = ({ clip, state, onChange }) => {
  const [snap, setSnap] = useState(true);
  const { props, framing } = useMemo(() => propsFor(state, clip), [state, clip]);
  const platform = clip.platform ?? {};
  const setPlatform = (patch: Partial<NonNullable<Highlight["platform"]>>) => onChange({ platform: { ...platform, ...patch } });
  const title = platform.youtube_title ?? "";

  return (
    <section className="editor">
      <div className="preview">
        <Player
          component={ViralClip}
          inputProps={props}
          compositionWidth={WIDTH}
          compositionHeight={HEIGHT}
          durationInFrames={Math.max(1, props.clipDurationInFrames)}
          fps={FPS}
          controls
          loop
          style={{ width: 300, height: 533, borderRadius: 12, overflow: "hidden", background: "#000" }}
        />
        <div className="muted small">
          Framing: {framing.reframe} ({framing.reason}) · B-roll: {props.broll.length}
        </div>
      </div>

      <div className="fields">
        <h2>
          Clip #{clip.rank} <span className="muted">score {clip.viral_score}</span>
        </h2>
        <p className="muted small">{clip.reasoning}</p>

        <Trimmer clip={clip} state={state} snap={snap} onChange={onChange} />
        <div className="row">
          <label>
            Start (s)
            <input type="number" step={0.01} value={clip.start} onChange={(e) => onChange({ start: Number(e.target.value) })} />
          </label>
          <label>
            End (s)
            <input type="number" step={0.01} value={clip.end} onChange={(e) => onChange({ end: Number(e.target.value) })} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={snap} onChange={(e) => setSnap(e.target.checked)} /> Snap handles to words
          </label>
        </div>

        <label>
          Hook text <span className="muted small">({clip.hook_text.trim().split(/\s+/).filter(Boolean).length}/10 words)</span>
          <input value={clip.hook_text} onChange={(e) => onChange({ hook_text: e.target.value })} />
        </label>
        {clip.hook_variants?.length ? (
          <div className="chips">
            {clip.hook_variants.map((v) => (
              <button key={v.text} className="chip" onClick={() => onChange({ hook_text: v.text })} title={`predicted CTR ${v.predicted_ctr}`}>
                {v.text}
              </button>
            ))}
          </div>
        ) : null}

        <label>
          YouTube Shorts title <span className={`small ${title.length > 100 ? "err" : "muted"}`}>({title.length}/100)</span>
          <input value={title} onChange={(e) => setPlatform({ youtube_title: e.target.value })} />
        </label>
        <label>
          TikTok description
          <textarea rows={3} value={platform.tiktok_description ?? ""} onChange={(e) => setPlatform({ tiktok_description: e.target.value })} />
        </label>
        <label>
          Instagram Reels caption
          <textarea rows={4} value={platform.instagram_caption ?? ""} onChange={(e) => setPlatform({ instagram_caption: e.target.value })} />
        </label>
      </div>
    </section>
  );
};

/** The source around the clip, with the transcript underneath and draggable start/end handles. */
const Trimmer: React.FC<{ clip: Highlight; state: State; snap: boolean; onChange: (patch: Partial<Highlight>) => void }> = ({
  clip,
  state,
  snap,
  onChange,
}) => {
  const ref = useRef<HTMLDivElement>(null);
  // Freeze the visible window while dragging so the scale doesn't shift under the pointer.
  const [frozen, setFrozen] = useState<[number, number] | null>(null);
  const [v0, v1] = frozen ?? viewWindow(clip.start, clip.end, state.duration);
  const x = (t: number) => `${((t - v0) / (v1 - v0)) * 100}%`;
  const words = state.words.filter((w) => w.end > v0 && w.start < v1);

  const drag = (edge: "start" | "end") => (e: React.PointerEvent) => {
    e.preventDefault();
    const box = ref.current!.getBoundingClientRect();
    const win: [number, number] = [v0, v1];
    setFrozen(win);
    const move = (ev: PointerEvent) => {
      const t = win[0] + ((ev.clientX - box.left) / box.width) * (win[1] - win[0]);
      const clamped = Math.min(Math.max(t, 0), state.duration);
      if (edge === "start") {
        const s = snap ? snapStart(clamped, state.words) : +clamped.toFixed(3);
        if (s < clip.end - 1) onChange({ start: s });
      } else {
        const en = snap ? snapEnd(clamped, state.words, state.duration) : +clamped.toFixed(3);
        if (en > clip.start + 1) onChange({ end: en });
      }
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setFrozen(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <div className="trimmer">
      <div className="track" ref={ref}>
        {state.segments
          .filter((s) => s.end > v0 && s.start < v1)
          .map((s) => (
            <div key={s.start} className="segment" style={{ left: x(Math.max(s.start, v0)), width: `calc(${x(Math.min(s.end, v1))} - ${x(Math.max(s.start, v0))})` }} />
          ))}
        <div className="selection" style={{ left: x(clip.start), width: `calc(${x(clip.end)} - ${x(clip.start)})` }} />
        <div className="handle" style={{ left: x(clip.start) }} onPointerDown={drag("start")} title="Drag to change the start" />
        <div className="handle" style={{ left: x(clip.end) }} onPointerDown={drag("end")} title="Drag to change the end" />
      </div>
      <div className="axis muted small">
        <span>{formatTime(v0)}</span>
        <span>{(clip.end - clip.start).toFixed(2)}s selected</span>
        <span>{formatTime(v1)}</span>
      </div>
      <div className="words">
        {words.map((w, i) => {
          const inside = w.start >= clip.start && w.end <= clip.end;
          return (
            <span
              key={`${w.start}-${i}`}
              className={inside ? "word in" : "word"}
              title={`${w.start.toFixed(2)}s — click: start here · shift-click: end here`}
              onClick={(e) =>
                e.shiftKey
                  ? onChange({ end: snapEnd(w.end, state.words, state.duration) })
                  : onChange({ start: snapStart(w.start, state.words) })
              }
            >
              {w.word}
            </span>
          );
        })}
      </div>
    </div>
  );
};

const RenderStatus: React.FC<{ job: Job | null; rendered: RenderedClip[] }> = ({ job, rendered }) => (
  <div className="status">
    <h3>Renders</h3>
    {job ? (
      <div className="muted small">
        {job.running ? `Rendering #${job.ranks.join(", #")}…` : job.exitCode === 0 ? "Last render finished." : `Last render failed (exit ${job.exitCode}).`}
      </div>
    ) : null}
    {job?.log.length ? <pre className="log">{job.log.slice(-12).join("\n")}</pre> : null}
    <ul>
      {rendered.flatMap((r) =>
        (r.hook_variants?.length ? r.hook_variants : [{ file: r.file, label: "A" }]).map((v) => (
          <li key={v.file}>
            <a href={`/clips/${encodeURIComponent(v.file)}`} target="_blank" rel="noreferrer">
              {v.file}
            </a>
          </li>
        )),
      )}
    </ul>
  </div>
);

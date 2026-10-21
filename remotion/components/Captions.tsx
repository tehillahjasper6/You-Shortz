import { createTikTokStyleCaptions, type Caption, type TikTokPage } from "@remotion/captions";
import { useMemo } from "react";
import { AbsoluteFill, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { fontFamily } from "../fonts";
import type { ClipWord } from "../schema";

// Words spoken within this window share a page (2-4 words at normal pace).
const COMBINE_WITHIN_MS = 900;
// Keep the last page up briefly after its final word so it can be read.
const LINGER_MS = 400;

// The bottom ~440px is covered by the platforms' description/handle UI and the
// right ~170px by the like/comment/share rail, so captions sit above and left of them.
const SAFE_BOTTOM_PX = 470;
const SAFE_LEFT_PX = 70;
const SAFE_RIGHT_PX = 170;

const CaptionPage: React.FC<{ page: TikTokPage; accentColor: string }> = ({ page, accentColor }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const timeMs = page.startMs + (frame / fps) * 1000;
  const enter = spring({ frame, fps, config: { damping: 14, stiffness: 220 }, durationInFrames: 8 });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: SAFE_BOTTOM_PX,
        paddingLeft: SAFE_LEFT_PX,
        paddingRight: SAFE_RIGHT_PX,
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          columnGap: 22,
          rowGap: 4,
          fontFamily,
          fontWeight: 900,
          fontSize: 86,
          lineHeight: 1.1,
          textTransform: "uppercase",
          textAlign: "center",
          scale: String(interpolate(enter, [0, 1], [0.86, 1])),
          opacity: enter,
        }}
      >
        {page.tokens.map((token, i) => {
          const active = token.fromMs <= timeMs && timeMs < token.toMs;
          const sinceStart = Math.max(0, frame - ((token.fromMs - page.startMs) / 1000) * fps);
          const pop = active ? spring({ frame: sinceStart, fps, config: { damping: 10, stiffness: 300 }, durationInFrames: 6 }) : 0;
          return (
            <span
              key={`${token.fromMs}-${i}`}
              style={{
                color: active ? accentColor : "#FFFFFF",
                WebkitTextStroke: "14px #000",
                paintOrder: "stroke fill",
                textShadow: "0 8px 24px rgba(0,0,0,0.55)",
                display: "inline-block",
                scale: String(1 + 0.12 * pop),
              }}
            >
              {token.text.trim()}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/**
 * Word-by-word karaoke captions. `words` are clip-relative seconds (the
 * render script has already subtracted trimBefore / fps from the source
 * timestamps), so page timing lines up with frame 0 of the clip.
 */
export const Captions: React.FC<{ words: ClipWord[]; accentColor: string }> = ({ words, accentColor }) => {
  const { fps } = useVideoConfig();

  const pages = useMemo(() => {
    const captions: Caption[] = words
      .filter((w) => w.end > 0)
      .map((w, i) => ({
        text: i === 0 ? w.word : ` ${w.word}`,
        startMs: Math.max(0, w.start * 1000),
        endMs: w.end * 1000,
        timestampMs: ((Math.max(0, w.start) + w.end) / 2) * 1000,
        confidence: null,
      }));
    return createTikTokStyleCaptions({ captions, combineTokensWithinMilliseconds: COMBINE_WITHIN_MS }).pages;
  }, [words]);

  return (
    <AbsoluteFill>
      {pages.map((page, index) => {
        const next = pages[index + 1];
        const startFrame = Math.round((page.startMs / 1000) * fps);
        const pageEndMs = page.startMs + page.durationMs + LINGER_MS;
        const endMs = next ? Math.min(next.startMs, pageEndMs) : pageEndMs;
        const durationInFrames = Math.round((endMs / 1000) * fps) - startFrame;
        if (durationInFrames <= 0) return null;
        return (
          <Sequence key={index} from={startFrame} durationInFrames={durationInFrames} layout="none">
            <CaptionPage page={page} accentColor={accentColor} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

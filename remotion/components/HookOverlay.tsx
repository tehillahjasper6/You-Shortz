import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { fontFamily } from "../fonts";

// Below the "Following | For You" tabs and search icon at the top of the feed.
const SAFE_TOP_PX = 290;
const EXIT_FRAMES = 8;

/** The Phase 2 hook_text, punched in with spring() over the top third for the opening seconds. */
export const HookOverlay: React.FC<{ text: string; durationInSeconds: number; accentColor: string }> = ({
  text,
  durationInSeconds,
  accentColor,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const visibleFrames = Math.round(durationInSeconds * fps);
  if (frame >= visibleFrames) return null;

  const punch = spring({ frame, fps, config: { damping: 11, stiffness: 180, mass: 0.7 } });
  const exit = interpolate(frame, [visibleFrames - EXIT_FRAMES, visibleFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ alignItems: "center", paddingTop: SAFE_TOP_PX, paddingLeft: 70, paddingRight: 70 }}>
      <div
        style={{
          fontFamily,
          fontWeight: 900,
          fontSize: 78,
          lineHeight: 1.08,
          textTransform: "uppercase",
          textAlign: "center",
          color: "#111",
          backgroundColor: "#FFFFFF",
          padding: "26px 40px 30px",
          borderRadius: 28,
          boxShadow: `0 14px 0 ${accentColor}, 0 24px 60px rgba(0,0,0,0.45)`,
          maxWidth: 900,
          scale: String(interpolate(punch, [0, 1], [1.7, 1]) * interpolate(exit, [0, 1], [0.9, 1])),
          rotate: `${interpolate(punch, [0, 1], [-6, -2])}deg`,
          opacity: Math.min(interpolate(frame, [0, 3], [0, 1], { extrapolateRight: "clamp" }), exit),
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

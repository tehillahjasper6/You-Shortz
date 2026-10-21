import { Video } from "@remotion/media";
import { AbsoluteFill, Freeze, interpolate, staticFile, useVideoConfig } from "remotion";
import { Captions } from "./components/Captions";
import { HookOverlay } from "./components/HookOverlay";
import { ProgressBar } from "./components/ProgressBar";
import { SafeZoneOverlay } from "./components/SafeZoneOverlay";
import type { ViralClipProps } from "./schema";

// Hold the last frame briefly so the clip doesn't cut out abruptly when it loops.
const HOLD_FRAMES = 4;
const AUDIO_FADE_OUT_SECONDS = 0.5;
const AUDIO_FADE_IN_FRAMES = 2;

/**
 * Vertical reframe of the source: a blurred, zoomed copy fills the 9:16
 * frame and the sharp source sits on top. Both layers are non-destructively
 * trimmed with `trimBefore`, so public/source.mp4 is never cut.
 */
const Reframed: React.FC<Pick<ViralClipProps, "src" | "trimBefore" | "clipDurationInFrames" | "fit">> = ({
  src,
  trimBefore,
  clipDurationInFrames,
  fit,
}) => {
  const { fps } = useVideoConfig();
  const source = staticFile(src);
  const fadeOutStart = clipDurationInFrames - HOLD_FRAMES - AUDIO_FADE_OUT_SECONDS * fps;

  return (
    <AbsoluteFill>
      <AbsoluteFill style={{ scale: "1.25", filter: "blur(48px) brightness(0.55) saturate(1.2)" }}>
        <Video
          src={source}
          trimBefore={trimBefore}
          durationInFrames={clipDurationInFrames}
          muted
          objectFit="cover"
          style={{ width: "100%", height: "100%" }}
        />
      </AbsoluteFill>
      <AbsoluteFill>
        <Video
          src={source}
          trimBefore={trimBefore}
          durationInFrames={clipDurationInFrames}
          objectFit={fit}
          style={{ width: "100%", height: "100%" }}
          volume={(f) =>
            interpolate(
              f,
              [0, AUDIO_FADE_IN_FRAMES, fadeOutStart, clipDurationInFrames - HOLD_FRAMES],
              [0, 1, 1, 0],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
            )
          }
        />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export const ViralClip: React.FC<ViralClipProps> = ({
  src,
  trimBefore,
  clipDurationInFrames,
  words,
  hookText,
  hookDurationInSeconds,
  accentColor,
  fit,
  showSafeZones,
}) => {
  const lastPlayedFrame = Math.max(0, clipDurationInFrames - HOLD_FRAMES - 1);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Freeze frame={lastPlayedFrame} active={(f) => f > lastPlayedFrame}>
        <Reframed src={src} trimBefore={trimBefore} clipDurationInFrames={clipDurationInFrames} fit={fit} />
      </Freeze>
      <Captions words={words} accentColor={accentColor} />
      {hookText.trim() ? (
        <HookOverlay text={hookText} durationInSeconds={hookDurationInSeconds} accentColor={accentColor} />
      ) : null}
      <ProgressBar durationInFrames={clipDurationInFrames} color={accentColor} />
      {showSafeZones ? <SafeZoneOverlay /> : null}
    </AbsoluteFill>
  );
};

import { Video } from "@remotion/media";
import { AbsoluteFill, Freeze, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { cameraX, cropLeft } from "./camera";
import { BRoll } from "./components/BRoll";
import { Captions } from "./components/Captions";
import { HookOverlay } from "./components/HookOverlay";
import { ProgressBar } from "./components/ProgressBar";
import { SafeZoneOverlay } from "./components/SafeZoneOverlay";
import type { ViralClipProps } from "./schema";

// Hold the last frame briefly so the clip doesn't cut out abruptly when it loops.
const HOLD_FRAMES = 4;
const AUDIO_FADE_OUT_SECONDS = 0.5;
const AUDIO_FADE_IN_FRAMES = 2;

type ReframeProps = Pick<
  ViralClipProps,
  "src" | "trimBefore" | "clipDurationInFrames" | "fit" | "reframe" | "sourceWidth" | "sourceHeight" | "cameraPath"
>;

/**
 * Vertical reframe of the source, non-destructively trimmed with `trimBefore`
 * so public/source.mp4 is never cut.
 *
 * - "track": the source fills the frame height and a 9:16 window slides
 *   along the face-tracking camera path.
 * - "fit": a blurred, zoomed copy fills the frame behind the whole source.
 */
const Reframed: React.FC<ReframeProps> = ({
  src,
  trimBefore,
  clipDurationInFrames,
  fit,
  reframe,
  sourceWidth,
  sourceHeight,
  cameraPath,
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const source = staticFile(src);
  const fadeOutStart = clipDurationInFrames - HOLD_FRAMES - AUDIO_FADE_OUT_SECONDS * fps;
  const volume = (f: number) =>
    interpolate(f, [0, AUDIO_FADE_IN_FRAMES, fadeOutStart, clipDurationInFrames - HOLD_FRAMES], [0, 1, 1, 0], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });

  if (reframe === "track" && cameraPath.length && sourceWidth && sourceHeight) {
    const left = cropLeft(cameraX(cameraPath, frame / fps), sourceWidth, sourceHeight, width, height);
    return (
      <AbsoluteFill style={{ overflow: "hidden" }}>
        <Video
          src={source}
          trimBefore={trimBefore}
          durationInFrames={clipDurationInFrames}
          objectFit="fill"
          volume={volume}
          style={{ position: "absolute", top: 0, left, height, width: (sourceWidth * height) / sourceHeight }}
        />
      </AbsoluteFill>
    );
  }

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
          volume={volume}
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
  reframe,
  fit,
  sourceWidth,
  sourceHeight,
  cameraPath,
  broll,
  showSafeZones,
}) => {
  const lastPlayedFrame = Math.max(0, clipDurationInFrames - HOLD_FRAMES - 1);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Freeze frame={lastPlayedFrame} active={(f) => f > lastPlayedFrame}>
        <Reframed
          src={src}
          trimBefore={trimBefore}
          clipDurationInFrames={clipDurationInFrames}
          fit={fit}
          reframe={reframe}
          sourceWidth={sourceWidth}
          sourceHeight={sourceHeight}
          cameraPath={cameraPath}
        />
        <BRoll items={broll} />
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

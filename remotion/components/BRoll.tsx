import { Video } from "@remotion/media";
import { AbsoluteFill, Img, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import type { BrollItem } from "../schema";

const DISSOLVE_FRAMES = 6;

const Cutaway: React.FC<{ item: BrollItem; durationInFrames: number }> = ({ item, durationInFrames }) => {
  const frame = useCurrentFrame();
  const fade = Math.min(DISSOLVE_FRAMES, Math.floor(durationInFrames / 2));
  const opacity = interpolate(frame, [0, fade, durationInFrames - fade, durationInFrames], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ opacity, backgroundColor: "#000" }}>
      {item.type === "video" ? (
        <Video src={staticFile(item.src)} muted objectFit="cover" style={{ width: "100%", height: "100%" }} />
      ) : (
        <Img
          src={staticFile(item.src)}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
            // Slow push-in so a still doesn't read as a freeze.
            scale: String(interpolate(frame, [0, durationInFrames], [1, 1.08])),
          }}
        />
      )}
    </AbsoluteFill>
  );
};

/** Stock-footage cutaways with a cross-dissolve; the clip's own audio keeps playing underneath. */
export const BRoll: React.FC<{ items: BrollItem[] }> = ({ items }) => {
  const { fps } = useVideoConfig();
  return (
    <>
      {items.map((item, i) => {
        const from = Math.round(item.from * fps);
        const durationInFrames = Math.round(item.duration * fps);
        if (durationInFrames <= 0) return null;
        return (
          <Sequence key={`${item.src}-${i}`} from={from} durationInFrames={durationInFrames} layout="none">
            <Cutaway item={item} durationInFrames={durationInFrames} />
          </Sequence>
        );
      })}
    </>
  );
};

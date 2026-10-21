import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";

const HEIGHT_PX = 12;

/**
 * A thin bar along the top edge that fills over the clip. Top rather than
 * bottom, because every platform draws its own scrubber along the bottom.
 */
export const ProgressBar: React.FC<{ durationInFrames: number; color: string }> = ({ durationInFrames, color }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: HEIGHT_PX, backgroundColor: "rgba(255,255,255,0.22)" }}>
        <div
          style={{
            height: "100%",
            width: `${interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 100], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            })}%`,
            backgroundColor: color,
            boxShadow: `0 0 16px ${color}`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

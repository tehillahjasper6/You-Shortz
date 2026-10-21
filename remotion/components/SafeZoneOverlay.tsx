import { AbsoluteFill } from "remotion";

type Zone = { label: string; top: number; left: number; width: number; height: number };

// Approximate union of TikTok, Instagram Reels and YouTube Shorts UI on a 1080x1920 frame.
const ZONES: Zone[] = [
  { label: "Top tabs / search", top: 0, left: 0, width: 1080, height: 260 },
  { label: "Like · comment · share rail", top: 820, left: 920, width: 160, height: 820 },
  { label: "Handle · description · audio", top: 1460, left: 0, width: 920, height: 460 },
];

/** Dev-only guide (render with --safe-zones) showing where platform UI will cover the clip. */
export const SafeZoneOverlay: React.FC = () => {
  return (
    <AbsoluteFill>
      {ZONES.map((z) => (
        <div
          key={z.label}
          style={{
            position: "absolute",
            top: z.top,
            left: z.left,
            width: z.width,
            height: z.height,
            backgroundColor: "rgba(255, 40, 80, 0.28)",
            border: "4px dashed rgba(255, 40, 80, 0.9)",
            color: "#fff",
            fontFamily: "sans-serif",
            fontSize: 30,
            fontWeight: 700,
            padding: 16,
            boxSizing: "border-box",
          }}
        >
          {z.label}
        </div>
      ))}
    </AbsoluteFill>
  );
};

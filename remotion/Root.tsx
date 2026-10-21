import { Composition, type CalculateMetadataFunction } from "remotion";
import { ViralClip } from "./ViralClip";
import { FPS, HEIGHT, WIDTH, defaultViralClipProps, viralClipSchema, type ViralClipProps } from "./schema";

const calculateMetadata: CalculateMetadataFunction<ViralClipProps> = ({ props }) => ({
  durationInFrames: props.clipDurationInFrames,
  defaultOutName: "viral-clip",
});

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="ViralClip"
      component={ViralClip}
      durationInFrames={defaultViralClipProps.clipDurationInFrames}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      schema={viralClipSchema}
      defaultProps={defaultViralClipProps}
      calculateMetadata={calculateMetadata}
    />
  );
};

// Settings for the Remotion CLI (`npm run studio`). Programmatic renders in
// scripts/04_render.ts pass their own options to renderMedia().
import { Config } from "@remotion/cli/config";

Config.setEntryPoint("./remotion/index.ts");
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setPixelFormat("yuv420p");
Config.setColorSpace("bt709");
Config.setCodec("h264");

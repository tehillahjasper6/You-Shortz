import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

// Montserrat Black/ExtraBold: the heavy, high-contrast look of native Shorts captions.
// Bundled in public/fonts (SIL OFL 1.1) so renders work offline; loadFont()
// holds the render until the face is ready.
export const fontFamily = "Montserrat";

loadFont({
  family: fontFamily,
  url: staticFile("fonts/Montserrat-Latin-Variable.woff2"),
  weight: "100 900",
});

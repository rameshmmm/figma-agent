async function main() {
    const frames = await getAllFrames();
    console.log("Frames found:", frames);
    figma.closePlugin(`Found ${frames.length} frame(s).`);
  }
  
  main();
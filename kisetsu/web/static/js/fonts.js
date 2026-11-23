// Inter is one variable font: the first text drawn at a size and weight not drawn before makes the browser build
// that instance, several milliseconds each, and a page that brings new ones (a show's page has its title, bold
// counts and the mono release names) paid for them all in the frame it opened in. They are built here while the
// browser is idle, so the views open without it.
const FACES = [
  ['Inter', 13, [450, 500, 600, 700]],
  ['Inter', 14, [400, 450, 500, 600, 700]],
  ['Inter', 15, [450, 500, 600]],
  ['Inter', 17, [600]],
  ['Inter', 20, [600]],
  ['Inter', 24, [600]],
  ['Inter', 32, [600]],
  ['IBM Plex Mono', 12.5, [450, 500]],
];
const SAMPLE = 'Ep 12 Aired 0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ [](),.-:/~';

const idle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 2000 }) : setTimeout(() => fn({ timeRemaining: () => 8 }), 50));

export function warmFonts() {
  const fonts = FACES.flatMap(([family, size, weights]) => weights.map(weight => `${weight} ${size}px "${family}"`));
  // Measured on a canvas, which builds the same instances without laying out the page.
  const ctx = document.createElement('canvas').getContext('2d');
  Promise.all(fonts.map(font => document.fonts.load(font).catch(() => {}))).then(() => {
    const step = (deadline) => {
      while (fonts.length && deadline.timeRemaining() > 2) {
        ctx.font = fonts.shift();
        ctx.measureText(SAMPLE);
      }
      if (fonts.length) idle(step);
    };
    idle(step);
  });
}

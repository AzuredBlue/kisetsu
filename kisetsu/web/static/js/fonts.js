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
  const box = document.createElement('div');
  box.setAttribute('aria-hidden', 'true');
  box.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none;white-space:nowrap';
  // The files are fetched first (the mono one is not used until a page needs it), then laid out one at a time.
  Promise.all(fonts.map(font => document.fonts.load(font).catch(() => {}))).then(() => {
    document.body.appendChild(box);
    const step = (deadline) => {
      while (fonts.length && deadline.timeRemaining() > 4) {
        const span = document.createElement('span');
        span.style.font = fonts.shift();
        span.textContent = SAMPLE;
        box.appendChild(span);
        void span.offsetWidth;
      }
      if (fonts.length) idle(step);
      else box.remove();
    };
    idle(step);
  });
}

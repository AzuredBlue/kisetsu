// AniList serves each cover at two sizes: medium, 230 px wide, and large, 460 px. Other addresses have one size.
const MEDIUM = '/media/anime/cover/medium/';
const LARGE = '/media/anime/cover/large/';

// The medium cover, for posters drawn small.
export const mediumCover = (url) => url.replace(LARGE, MEDIUM);

// Both sizes, for a poster that may be drawn wider than the medium cover: the browser takes the medium one unless
// the poster is wider than it on screen (a high-density screen, or the page zoomed in).
export const coverSet = (url) => (url.includes(LARGE) ? `${mediumCover(url)} 230w, ${url} 460w` : undefined);

// How wide a show page draws its poster (`.show-poster` in views.css).
export const SHOW_POSTER_SIZES = '(min-width: 1024px) 13.5rem, 9rem';

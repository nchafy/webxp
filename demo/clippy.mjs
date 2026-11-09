/* Clippy. One message, once, and then he goes away.
 *
 * He exists because the Read me used to open on every load, which is the wrong way to offer
 * help: it takes the desktop before you have asked for anything. An assistant who offers and
 * accepts no is the period-correct version of the same thought.
 *
 * Deliberately not an app. He is not a window — no title bar, no taskbar button, nothing to
 * minimise — so making him one would mean a window pretending not to be a window. He is host
 * page content, which is also why he lives in `demo/` rather than in `src/`.
 *
 * Deliberately not clever. He says one thing on arrival and has no opinion about anything you
 * do afterwards. The real Clippy's failing was not his voice, it was that he kept going.
 */

const SVG = 'http://www.w3.org/2000/svg';

//: His one line, in his own register: the observation, then the offer. Every other string in
//: this project is terse on purpose; this one is in character on purpose, which is the only
//: reason it is a question and not a label.
const GREETING = "It looks like you're looking at a desktop. Would you like help with that?";

//: Where his dismissal is remembered. Without it the header's claim that he appears once is
//: false: he came back on every load and had to be declined again, which is the behaviour
//: taking the read-me off autostart was meant to remove.
const SEEN_KEY = 'webxp.clippy.seen';

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

/**
 * The paperclip.
 *
 * Three strokes and two eyes. The bends are what make a paperclip read as Clippy rather than
 * as stationery, so they are drawn large and the wire is thick; at a faithful gauge he looks
 * like a staple.
 */
function clip() {
  const face = svg('svg', {
    viewBox: '0 0 56 84',
    width: 56,
    height: 84,
    class: 'clippy-body',
    'aria-hidden': 'true',
    focusable: 'false',
  });

  // The wire, drawn twice: a dark pass for the shadowed underside, a lighter pass on top of
  // it, which is the whole of the metal shading.
  const wire = 'M20 34 V62 a10 10 0 0 0 20 0 V20 a9 9 0 0 0 -18 0 V56';
  face.append(svg('path', {
    d: wire,
    fill: 'none',
    stroke: '#6f7b86',
    'stroke-width': 7,
    'stroke-linecap': 'round',
  }));
  face.append(svg('path', {
    d: wire,
    fill: 'none',
    stroke: '#cdd7e0',
    'stroke-width': 4,
    'stroke-linecap': 'round',
  }));

  // Eyes, in the upper bend where the real one had them.
  for (const cx of [25, 36]) {
    face.append(svg('ellipse', { cx, cy: 30, rx: 5.2, ry: 6, fill: '#fff', stroke: '#2b3a4f' }));
    face.append(svg('circle', { cx: cx + 1, cy: 31, r: 2.4, fill: '#16202e' }));
  }
  // Eyebrows. They are the entire performance.
  face.append(svg('path', {
    d: 'M20.5 21.5c2-2.4 5-3 7.5-1.6M33 19.6c2.6-1.2 5.6-.4 7.4 2',
    fill: 'none',
    stroke: '#2b3a4f',
    'stroke-width': 2,
    'stroke-linecap': 'round',
  }));
  return face;
}

/**
 * Put Clippy on the desktop with his one offer.
 *
 * @param {object} options
 * @param {HTMLElement} options.root        Where he goes. The desktop surface.
 * @param {() => void} [options.onAccept]   Taking the offer. Dismisses him too.
 * @returns {{dismiss: () => void}}
 */
export function mountClippy({ root, onAccept }) {
  try {
    if (localStorage.getItem(SEEN_KEY)) return { dismiss: () => {} };
  } catch {
    /* no storage, so he asks */
  }

  const wrap = document.createElement('div');
  wrap.className = 'clippy';

  const bubble = document.createElement('div');
  bubble.className = 'clippy-bubble card';
  // A live region would announce him over whatever a reader was already doing, which is the
  // behaviour he is famous for and the one thing not worth reproducing. He is a labelled
  // group instead: reachable, not insistent.
  bubble.setAttribute('role', 'group');
  bubble.setAttribute('aria-label', 'Office Assistant');

  const says = document.createElement('p');
  says.className = 'clippy-says';
  says.textContent = GREETING;

  const buttons = document.createElement('div');
  buttons.className = 'clippy-buttons';

  says.id = 'clippy-says';
  const yes = document.createElement('button');
  yes.type = 'button';
  yes.textContent = 'Yes, please';
  // Without this a reader tabbing in hears "Office Assistant, Yes please, button" and never
  // the question being answered.
  yes.setAttribute('aria-describedby', says.id);

  const no = document.createElement('button');
  no.type = 'button';
  no.setAttribute('aria-describedby', 'clippy-says');
  // Period-correct, and the only honest label for the button that makes him leave.
  no.textContent = 'No, just let me work';

  const dismiss = () => {
    wrap.remove();
    // A refusal is the answer to the question, so it is kept. Wrapped because a browser with
    // storage denied throws on write, and an assistant is not worth an exception.
    try {
      localStorage.setItem(SEEN_KEY, '1');
    } catch {
      /* no storage, so he asks again next time */
    }
  };

  yes.addEventListener('click', () => {
    dismiss();
    if (onAccept) onAccept();
  });
  no.addEventListener('click', dismiss);

  buttons.append(yes, no);
  bubble.append(says, buttons);
  wrap.append(bubble, clip());
  root.append(wrap);

  // Focus stays where it was. He is an offer, not a prompt — taking the caret to him would
  // make declining him compulsory.
  return { dismiss };
}
